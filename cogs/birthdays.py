"""
Birthdays Cog for Logiq
Birthday signup and automatic birthday announcements
"""

import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.db_manager import DatabaseManager
from utils.embeds import EmbedColor, EmbedFactory
from utils.permissions import is_admin

logger = logging.getLogger(__name__)

DEFAULT_BIRTHDAY_MESSAGE = "🎉 Happy Birthday {user}! Everyone wish them a great day in **{server}**!"


def is_valid_birthday(month: int, day: int) -> bool:
    """Validate a birthday without requiring a real year."""
    if month < 1 or month > 12:
        return False

    days_in_month = {
        1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
        7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
    }
    return 1 <= day <= days_in_month[month]


def format_birthday(month: int, day: int) -> str:
    """Format a month/day pair nicely."""
    return datetime(2024, month, day).strftime("%B %d")


class SafeTemplateDict(dict):
    """Fallback-safe dict for lightweight template rendering."""

    def __missing__(self, key):
        return "{" + key + "}"


class Birthdays(commands.Cog):
    """Birthday signup and announcement cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get("modules", {}).get("birthdays", {})
        self.check_birthdays_task.start()

    def cog_unload(self):
        """Cleanup on unload"""
        self.check_birthdays_task.cancel()

    def _get_now_for_guild(self, guild_config: dict) -> datetime:
        """Resolve current time using the guild's configured birthday timezone offset."""
        offset = guild_config.get("birthday_timezone_offset", 0)
        tz = timezone(timedelta(hours=offset))
        return datetime.now(tz)

    def _build_birthday_message(self, template: Optional[str], member: discord.Member, month: int, day: int) -> str:
        """Render a birthday announcement template."""
        message_template = template or DEFAULT_BIRTHDAY_MESSAGE
        return message_template.format_map(SafeTemplateDict(
            user=member.mention,
            username=member.name,
            display_name=member.display_name,
            server=member.guild.name,
            birthday=format_birthday(month, day),
            month=format_birthday(month, day)
        ))

    async def _ensure_user(self, user_id: int, guild_id: int) -> dict:
        """Get or create a user document."""
        user_data = await self.db.get_user(user_id, guild_id)
        if not user_data:
            user_data = await self.db.create_user(user_id, guild_id)
        return user_data

    async def _ensure_guild(self, guild_id: int) -> dict:
        """Get or create a guild config document."""
        guild_config = await self.db.get_guild(guild_id)
        if not guild_config:
            guild_config = await self.db.create_guild(guild_id)
        return guild_config

    async def _announce_birthdays_for_guild(self, guild: discord.Guild) -> None:
        """Send birthday announcements for a single guild if needed."""
        guild_config = await self._ensure_guild(guild.id)
        birthday_channel_id = guild_config.get("birthday_channel")
        if not birthday_channel_id:
            return

        channel = guild.get_channel(birthday_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        now = self._get_now_for_guild(guild_config)
        today_key = now.strftime("%Y-%m-%d")
        month = now.month
        day = now.day

        birthdays = await self.db.get_guild_users_with_birthday(guild.id, month, day)

        if month == 2 and day == 28 and not calendar.isleap(now.year):
            leap_birthdays = await self.db.get_guild_users_with_birthday(guild.id, 2, 29)
            birthdays.extend(leap_birthdays)

        for user_data in birthdays:
            if user_data.get("birthday_last_announced") == today_key:
                continue

            member = guild.get_member(user_data["user_id"])
            if member is None:
                continue

            try:
                birthday_month = user_data.get("birthday_month", month)
                birthday_day = user_data.get("birthday_day", day)
                content = self._build_birthday_message(
                    guild_config.get("birthday_message"),
                    member,
                    birthday_month,
                    birthday_day
                )
                embed = EmbedFactory.create(
                    title="🎂 Birthday Time!",
                    description=f"Today is **{member.display_name}**'s birthday!",
                    color=EmbedColor.PREMIUM,
                    thumbnail=member.display_avatar.url,
                    fields=[
                        {"name": "Member", "value": member.mention, "inline": True},
                        {"name": "Birthday", "value": format_birthday(birthday_month, birthday_day), "inline": True},
                    ],
                )
                await channel.send(content=content, embed=embed)
                await self.db.update_user(
                    member.id,
                    guild.id,
                    {"birthday_last_announced": today_key}
                )
            except discord.Forbidden:
                logger.warning("Cannot send birthday message in %s", guild)
                return
            except Exception:
                logger.exception("Failed to announce birthday for %s in %s", member, guild)

    @tasks.loop(minutes=30)
    async def check_birthdays_task(self):
        """Background task to post birthdays each day."""
        for guild in self.bot.guilds:
            try:
                await self._announce_birthdays_for_guild(guild)
            except Exception:
                logger.exception("Birthday checker failed in guild %s", guild)

    @check_birthdays_task.before_loop
    async def before_check_birthdays(self):
        """Wait for bot readiness before starting the birthday checker."""
        await self.bot.wait_until_ready()

    @app_commands.command(name="birthday-set", description="Save your birthday for automatic birthday announcements")
    @app_commands.describe(month="Birth month (1-12)", day="Birth day")
    async def birthday_set(self, interaction: discord.Interaction, month: int, day: int):
        """Register the invoking user's birthday."""
        if not is_valid_birthday(month, day):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Birthday", "Please provide a real calendar date."),
                ephemeral=True
            )
            return

        await self._ensure_user(interaction.user.id, interaction.guild.id)
        await self.db.update_user(
            interaction.user.id,
            interaction.guild.id,
            {
                "birthday_month": month,
                "birthday_day": day,
                "birthday_last_announced": None,
            }
        )

        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Birthday Saved",
                f"Your birthday is now set to **{format_birthday(month, day)}**."
            ),
            ephemeral=True
        )

    @app_commands.command(name="birthday-remove", description="Remove your saved birthday")
    async def birthday_remove(self, interaction: discord.Interaction):
        """Remove the invoking user's stored birthday."""
        user_data = await self.db.get_user(interaction.user.id, interaction.guild.id)
        if (
            not user_data
            or user_data.get("birthday_month") is None
            or user_data.get("birthday_day") is None
        ):
            await interaction.response.send_message(
                embed=EmbedFactory.info("No Birthday Saved", "You do not currently have a birthday saved."),
                ephemeral=True
            )
            return

        await self.db.update_user(
            interaction.user.id,
            interaction.guild.id,
            {
                "birthday_month": None,
                "birthday_day": None,
                "birthday_last_announced": None,
            }
        )

        await interaction.response.send_message(
            embed=EmbedFactory.success("Birthday Removed", "Your birthday has been removed."),
            ephemeral=True
        )

    @app_commands.command(name="birthday-config", description="Configure the birthday announcement channel and settings")
    @app_commands.describe(
        channel="Channel where birthday alerts should be posted",
        timezone_offset="UTC offset used for birthday announcements (-12 to 14)",
        message="Optional birthday message template"
    )
    @is_admin()
    async def birthday_config(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        timezone_offset: Optional[int] = None,
        message: Optional[str] = None
    ):
        """Configure birthday alert settings for the guild."""
        guild_config = await self._ensure_guild(interaction.guild.id)
        update_data = {}

        if channel is not None:
            update_data["birthday_channel"] = channel.id

        if timezone_offset is not None:
            if timezone_offset < -12 or timezone_offset > 14:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Invalid Timezone", "Timezone offset must be between -12 and 14."),
                    ephemeral=True
                )
                return
            update_data["birthday_timezone_offset"] = timezone_offset

        if message is not None:
            update_data["birthday_message"] = message.strip() or DEFAULT_BIRTHDAY_MESSAGE

        if not update_data:
            current_channel = guild_config.get("birthday_channel")
            channel_value = f"<#{current_channel}>" if current_channel else "Not set"
            tz_value = guild_config.get("birthday_timezone_offset", 0)
            message_value = guild_config.get("birthday_message", DEFAULT_BIRTHDAY_MESSAGE)
            embed = EmbedFactory.create(
                title="🎂 Birthday Configuration",
                color=EmbedColor.INFO,
                fields=[
                    {"name": "Birthday Channel", "value": channel_value, "inline": False},
                    {"name": "Timezone Offset", "value": f"UTC{tz_value:+d}", "inline": True},
                    {"name": "Message Template", "value": message_value, "inline": False},
                ],
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await self.db.update_guild(interaction.guild.id, update_data)
        guild_config.update(update_data)

        channel_value = f"<#{guild_config.get('birthday_channel')}>" if guild_config.get("birthday_channel") else "Not set"
        tz_value = guild_config.get("birthday_timezone_offset", 0)
        message_value = guild_config.get("birthday_message", DEFAULT_BIRTHDAY_MESSAGE)

        embed = EmbedFactory.success(
            "Birthday Settings Updated",
            f"**Channel:** {channel_value}\n"
            f"**Timezone:** UTC{tz_value:+d}\n"
            f"**Template:** {message_value}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="birthday-list", description="List saved birthdays in this server")
    @is_admin()
    async def birthday_list(self, interaction: discord.Interaction):
        """List all saved birthdays for this guild."""
        birthdays = await self.db.get_guild_birthdays(interaction.guild.id)
        if not birthdays:
            await interaction.response.send_message(
                embed=EmbedFactory.info("No Birthdays Saved", "No members have saved birthdays yet."),
                ephemeral=True
            )
            return

        lines = []
        for entry in birthdays[:25]:
            member = interaction.guild.get_member(entry["user_id"])
            display = member.mention if member else f"<@{entry['user_id']}>"
            lines.append(f"{display} - {format_birthday(entry['birthday_month'], entry['birthday_day'])}")

        embed = EmbedFactory.create(
            title="🎂 Saved Birthdays",
            description="\n".join(lines),
            color=EmbedColor.INFO,
            footer=f"Showing {min(len(birthdays), 25)} of {len(birthdays)} birthdays"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="birthday-test", description="Send a test birthday alert to the configured birthday channel")
    @is_admin()
    async def birthday_test(self, interaction: discord.Interaction):
        """Send a test birthday alert using the current guild birthday configuration."""
        guild_config = await self._ensure_guild(interaction.guild.id)
        birthday_channel_id = guild_config.get("birthday_channel")
        if not birthday_channel_id:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Birthday Channel Missing", "Set one with `/birthday-config channel:#channel`."),
                ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(birthday_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Birthday Channel Missing", "The configured birthday channel could not be found."),
                ephemeral=True
            )
            return

        try:
            content = self._build_birthday_message(guild_config.get("birthday_message"), interaction.user, 1, 1)
            embed = EmbedFactory.create(
                title="🎂 Birthday Test",
                description=f"This is how birthday alerts will look for {interaction.user.mention}.",
                color=EmbedColor.PREMIUM,
                thumbnail=interaction.user.display_avatar.url,
                fields=[
                    {"name": "Birthday", "value": "January 01", "inline": True},
                    {"name": "Channel", "value": channel.mention, "inline": True},
                ],
            )
            await channel.send(content=content, embed=embed)
            await interaction.response.send_message(
                embed=EmbedFactory.success("Birthday Test Sent", f"Sent a preview to {channel.mention}."),
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Permission Error", "I cannot send messages in the configured birthday channel."),
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Birthdays(bot, bot.db, bot.config))
