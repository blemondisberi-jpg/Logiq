"""
Server Stats Channels Cog for Logiq
Creates auto-updating stat/clock voice channels
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin

logger = logging.getLogger(__name__)

ISO3166_PATH = Path("/usr/share/zoneinfo/iso3166.tab")
ZONE_TAB_PATH = Path("/usr/share/zoneinfo/zone.tab")
SERVER_STATS_KEY = "server_stats_channels"
MAX_COUNTRY_CHOICES = 25
MAX_TIMEZONE_CHOICES = 25

COUNTRY_ALIASES = {
    "usa": "US",
    "us": "US",
    "united states": "US",
    "united states of america": "US",
    "america": "US",
    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "scotland": "GB",
    "wales": "GB",
    "northern ireland": "GB",
    "uae": "AE",
    "united arab emirates": "AE",
}


def _normalize_lookup(value: str) -> str:
    """Normalize a country search term."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    cleaned = []
    last_space = False
    for char in ascii_value.lower():
        if char.isalnum():
            cleaned.append(char)
            last_space = False
        elif not last_space:
            cleaned.append(" ")
            last_space = True
    return "".join(cleaned).strip()


def _friendly_timezone_label(timezone_name: str) -> str:
    """Friendly label for a timezone entry."""
    tail = timezone_name.split("/")[-1].replace("_", " ")
    return tail if tail else timezone_name


def _next_refresh_time() -> datetime:
    """Return the next minute boundary in UTC for the updater loop."""
    now = discord.utils.utcnow()
    return (now + timedelta(minutes=1)).replace(second=0, microsecond=0)


class ServerStatsChannels(commands.Cog):
    """Auto-updating stat channels including a country clock."""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.country_names_by_code: dict[str, str] = {}
        self.country_timezones_by_code: dict[str, list[str]] = {}
        self.lookup_to_code: dict[str, str] = {}
        self._load_timezone_tables()
        self.update_task = self.bot.loop.create_task(self._update_loop())

    def cog_unload(self):
        """Cleanup background task on unload."""
        self.update_task.cancel()

    def _load_timezone_tables(self) -> None:
        """Load country and timezone tables from system tzdata."""
        if ISO3166_PATH.exists():
            for raw_line in ISO3166_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                code, name = parts[0], parts[1]
                self.country_names_by_code[code] = name

        if ZONE_TAB_PATH.exists():
            for raw_line in ZONE_TAB_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                country_codes, _coords, timezone_name = parts[:3]
                for code in country_codes.split(","):
                    self.country_timezones_by_code.setdefault(code, []).append(timezone_name)

        for code, name in self.country_names_by_code.items():
            self.lookup_to_code[_normalize_lookup(name)] = code
        for alias, code in COUNTRY_ALIASES.items():
            self.lookup_to_code[_normalize_lookup(alias)] = code

        logger.info(
            "Server stats timezone tables loaded (%s countries, %s timezone mappings)",
            len(self.country_names_by_code),
            len(self.country_timezones_by_code),
        )

    def _resolve_country_code(self, query: str) -> tuple[str | None, list[str]]:
        """Resolve a country query to an ISO code."""
        normalized = _normalize_lookup(query)
        if not normalized:
            return None, []

        country_code = self.lookup_to_code.get(normalized)
        if country_code:
            return country_code, []

        suggestions = []
        for country_name in self.country_names_by_code.values():
            normalized_name = _normalize_lookup(country_name)
            if normalized in normalized_name or normalized_name.startswith(normalized):
                suggestions.append(country_name)
            if len(suggestions) >= 5:
                break
        return None, suggestions

    async def _country_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete country names."""
        normalized = _normalize_lookup(current)
        choices = []
        for code, name in sorted(self.country_names_by_code.items(), key=lambda item: item[1]):
            if normalized and normalized not in _normalize_lookup(name):
                continue
            choices.append(app_commands.Choice(name=name, value=name))
            if len(choices) >= MAX_COUNTRY_CHOICES:
                break
        return choices

    async def _timezone_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete timezone names, narrowed by selected country when available."""
        normalized = _normalize_lookup(current)
        selected_country = getattr(interaction.namespace, "country", None)
        filtered_timezones: list[str] = []
        if selected_country:
            country_code, _ = self._resolve_country_code(selected_country)
            if country_code:
                filtered_timezones = self.country_timezones_by_code.get(country_code, [])

        timezone_groups = [filtered_timezones] if filtered_timezones else self.country_timezones_by_code.values()
        seen = set()
        choices = []
        for timezones in timezone_groups:
            for timezone_name in timezones:
                if timezone_name in seen:
                    continue
                seen.add(timezone_name)
                label = _friendly_timezone_label(timezone_name)
                if normalized and normalized not in _normalize_lookup(label) and normalized not in _normalize_lookup(timezone_name):
                    continue
                choices.append(app_commands.Choice(name=f"{label} ({timezone_name})", value=timezone_name))
                if len(choices) >= MAX_TIMEZONE_CHOICES:
                    return choices
        return choices

    def _format_channel_names(
        self,
        guild: discord.Guild,
        *,
        timezone_name: str,
        clock_label: str
    ) -> dict[str, str]:
        """Build the desired stat channel names."""
        now = datetime.now(ZoneInfo(timezone_name))
        total_members = guild.member_count or len(guild.members)
        bots = sum(1 for member in guild.members if member.bot)
        humans = total_members - bots

        return {
            "clock": f"🕒 {clock_label}: {now.strftime('%H:%M')}",
            "total": f"🔒 Total Members: {total_members}",
            "humans": f"🔒 People: {humans}",
            "bots": f"🔒 Robots: {bots}",
        }

    async def _update_config(self, guild_id: int, payload: dict) -> None:
        """Persist server stats config into the guild document."""
        guild_config = await self.db.get_guild(guild_id)
        if not guild_config:
            await self.db.create_guild(guild_id)
        await self.db.update_guild(guild_id, {SERVER_STATS_KEY: payload})

    async def _get_config(self, guild_id: int) -> dict | None:
        """Load server stats config from guild document."""
        guild_config = await self.db.get_guild(guild_id)
        if not guild_config:
            return None
        return guild_config.get(SERVER_STATS_KEY)

    async def _create_or_sync_channels(
        self,
        guild: discord.Guild,
        *,
        category_name: str,
        clock_label: str,
        timezone_name: str,
        existing_config: dict | None = None
    ) -> dict:
        """Create or sync the stat voice channels."""
        me = guild.me or guild.get_member(self.bot.user.id if self.bot.user else 0)
        if me is None:
            raise RuntimeError("Bot member could not be resolved for this guild.")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(connect=False, speak=False, send_messages=False),
            me: discord.PermissionOverwrite(connect=True, manage_channels=True, view_channel=True),
        }

        category = None
        if existing_config:
            category = guild.get_channel(existing_config.get("category_id"))
        if category is None:
            category = await guild.create_category(category_name, overwrites=overwrites, reason="Server stats channel setup")
        elif category.name != category_name:
            await category.edit(name=category_name, overwrites=overwrites, reason="Updating server stats category")

        desired_names = self._format_channel_names(guild, timezone_name=timezone_name, clock_label=clock_label)
        channels = {}

        existing_ids = existing_config.get("channel_ids", {}) if existing_config else {}
        for key, desired_name in desired_names.items():
            channel = guild.get_channel(existing_ids.get(key))
            if channel is None:
                channel = await category.create_voice_channel(
                    desired_name,
                    overwrites=overwrites,
                    user_limit=0,
                    reason="Server stats channel setup"
                )
            else:
                edits = {}
                if channel.name != desired_name:
                    edits["name"] = desired_name
                if channel.category_id != category.id:
                    edits["category"] = category
                if edits:
                    edits["reason"] = "Refreshing server stats channels"
                    await channel.edit(**edits)
            channels[key] = channel.id

        return {
            "enabled": True,
            "category_id": category.id,
            "channel_ids": channels,
            "country_code": existing_config.get("country_code") if existing_config else None,
            "country_name": existing_config.get("country_name") if existing_config else None,
            "timezone_name": timezone_name,
            "clock_label": clock_label,
            "category_name": category_name,
        }

    async def _sync_one_guild(self, guild: discord.Guild, config: dict) -> None:
        """Update one guild's stat channels to current values."""
        if not config.get("enabled"):
            return

        channel_ids = config.get("channel_ids", {})
        timezone_name = config.get("timezone_name")
        clock_label = config.get("clock_label", "Time")
        if not timezone_name:
            return

        desired_names = self._format_channel_names(guild, timezone_name=timezone_name, clock_label=clock_label)
        for key, desired_name in desired_names.items():
            channel = guild.get_channel(channel_ids.get(key))
            if channel is None:
                continue
            if channel.name != desired_name:
                await channel.edit(name=desired_name, reason="Refreshing server stats channels")

    async def _update_loop(self) -> None:
        """Background loop to refresh all configured stat channels."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                guild_configs = await self.db.db.guilds.find({f"{SERVER_STATS_KEY}.enabled": True}).to_list(length=1000)
                for guild_config in guild_configs:
                    guild = self.bot.get_guild(guild_config["guild_id"])
                    if guild is None:
                        continue
                    stats_config = guild_config.get(SERVER_STATS_KEY)
                    if not stats_config:
                        continue
                    try:
                        await self._sync_one_guild(guild, stats_config)
                    except Exception as error:
                        logger.error("Failed to update server stats channels for %s: %s", guild.id, error, exc_info=True)
                await discord.utils.sleep_until(_next_refresh_time())
            except Exception as error:
                logger.error("Server stats update loop failed: %s", error, exc_info=True)
                await discord.utils.sleep_until(_next_refresh_time())

    @app_commands.command(name="serverstats-channels-setup", description="Create auto-updating server stat channels (Admin)")
    @app_commands.describe(
        country="Country whose time should be displayed",
        timezone_name="Optional IANA timezone if the country has multiple timezones",
        category_name="Optional category name for the stat channels",
        clock_label="Optional label shown next to the time"
    )
    @app_commands.autocomplete(country=_country_autocomplete, timezone_name=_timezone_autocomplete)
    @app_commands.guild_only()
    @is_admin()
    async def serverstats_channels_setup(
        self,
        interaction: discord.Interaction,
        country: str,
        timezone_name: str | None = None,
        category_name: str | None = None,
        clock_label: str | None = None
    ):
        """Create the stat channels shown in the member list."""
        country_code, suggestions = self._resolve_country_code(country)
        if not country_code:
            suggestion_text = f"\n\nDid you mean: {', '.join(suggestions)}" if suggestions else ""
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Country Not Found",
                    f"I couldn't match `{country}` to a supported country.{suggestion_text}"
                ),
                ephemeral=True
            )
            return

        country_name = self.country_names_by_code.get(country_code, country)
        timezones = self.country_timezones_by_code.get(country_code, [])
        if not timezones:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "No Timezones Found",
                    f"I found `{country_name}` but there were no timezone entries available for it."
                ),
                ephemeral=True
            )
            return

        if timezone_name:
            if timezone_name not in timezones:
                await interaction.response.send_message(
                    embed=EmbedFactory.error(
                        "Timezone Mismatch",
                        f"`{timezone_name}` is not one of the known timezones for `{country_name}`."
                    ),
                    ephemeral=True
                )
                return
            selected_timezone = timezone_name
        elif len(timezones) == 1:
            selected_timezone = timezones[0]
        else:
            timezone_list = ", ".join(_friendly_timezone_label(item) for item in timezones[:8])
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Multiple Timezones",
                    f"`{country_name}` has multiple timezones. Re-run this command with `timezone_name`.\n\nExamples: {timezone_list}"
                ),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if not interaction.guild.me.guild_permissions.manage_channels:
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "Missing Bot Permission",
                    "I need the **Manage Channels** permission to create and update the stat channels."
                ),
                ephemeral=True
            )
            return

        saved_config = await self._get_config(interaction.guild.id)
        stats_config = await self._create_or_sync_channels(
            interaction.guild,
            category_name=category_name or "📊 SERVER STATS 📊",
            clock_label=clock_label or _friendly_timezone_label(selected_timezone),
            timezone_name=selected_timezone,
            existing_config={
                **(saved_config or {}),
                "country_code": country_code,
                "country_name": country_name,
            }
        )
        stats_config["country_code"] = country_code
        stats_config["country_name"] = country_name
        await self._update_config(interaction.guild.id, stats_config)

        embed = EmbedFactory.success(
            "Server Stats Channels Ready",
            (
                f"**Country:** {country_name}\n"
                f"**Timezone:** {selected_timezone}\n"
                f"**Category:** <#{stats_config['category_id']}>\n\n"
                "The clock and member counters will refresh automatically."
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="serverstats-channels-refresh", description="Refresh the server stat channels now (Admin)")
    @app_commands.guild_only()
    @is_admin()
    async def serverstats_channels_refresh(self, interaction: discord.Interaction):
        """Refresh the stat channels immediately."""
        stats_config = await self._get_config(interaction.guild.id)
        if not stats_config or not stats_config.get("enabled"):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Configured", "Server stats channels are not configured in this server."),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self._sync_one_guild(interaction.guild, stats_config)
        await interaction.followup.send(
            embed=EmbedFactory.success("Refreshed", "Server stats channels were updated successfully."),
            ephemeral=True
        )

    @app_commands.command(name="serverstats-channels-remove", description="Disable the server stat channels (Admin)")
    @app_commands.describe(delete_channels="Delete the created category and channels too")
    @app_commands.guild_only()
    @is_admin()
    async def serverstats_channels_remove(self, interaction: discord.Interaction, delete_channels: bool = False):
        """Disable or remove the server stats channels feature."""
        stats_config = await self._get_config(interaction.guild.id)
        if not stats_config:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Not Configured", "There is no server stats channel setup to remove."),
                ephemeral=True
            )
            return

        if delete_channels:
            category = interaction.guild.get_channel(stats_config.get("category_id"))
            if category:
                for channel in list(category.channels):
                    try:
                        await channel.delete(reason="Removing server stats channels")
                    except discord.HTTPException:
                        logger.warning("Failed to delete server stats channel %s", channel.id)
                try:
                    await category.delete(reason="Removing server stats category")
                except discord.HTTPException:
                    logger.warning("Failed to delete server stats category %s", category.id)

        await self._update_config(interaction.guild.id, {"enabled": False})
        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Server Stats Disabled",
                "The auto-updating server stats channels have been disabled."
                + (" The created channels were also deleted." if delete_channels else "")
            ),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(ServerStatsChannels(bot, bot.db, bot.config))
