"""
World Time Cog for Logiq
Country-based time lookup with multi-timezone support
"""

from __future__ import annotations

import logging
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from utils.embeds import EmbedFactory, EmbedColor

logger = logging.getLogger(__name__)

ISO3166_PATH = Path("/usr/share/zoneinfo/iso3166.tab")
ZONE_TAB_PATH = Path("/usr/share/zoneinfo/zone.tab")
MAX_AUTOCOMPLETE_CHOICES = 25
MAX_TIMEZONE_FIELDS = 25

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
    "u.a.e.": "AE",
    "united arab emirates": "AE",
    "south korea": "KR",
    "north korea": "KP",
    "czech republic": "CZ",
    "ivory coast": "CI",
    "vatican": "VA",
    "russia": "RU",
    "bolivia": "BO",
    "moldova": "MD",
    "laos": "LA",
    "tanzania": "TZ",
    "syria": "SY",
    "venezuela": "VE",
}


def _normalize_lookup(value: str) -> str:
    """Normalize user input/country names for robust lookup."""
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


def _friendly_timezone_name(timezone_name: str) -> str:
    """Convert an IANA timezone into a nicer user-facing label."""
    return timezone_name.replace("_", " / ").replace("/", " - ")


class WorldTime(commands.Cog):
    """Country-based world time utilities."""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.country_names_by_code: dict[str, str] = {}
        self.country_timezones_by_code: dict[str, list[str]] = {}
        self.lookup_to_code: dict[str, str] = {}
        self._load_timezone_tables()

    def _load_timezone_tables(self) -> None:
        """Load country names and timezone mappings from system tzdata tables."""
        if ISO3166_PATH.exists():
            for raw_line in ISO3166_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                country_code, country_name = parts[0], parts[1]
                self.country_names_by_code[country_code] = country_name

        if ZONE_TAB_PATH.exists():
            for raw_line in ZONE_TAB_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                country_codes, _coordinates, timezone_name = parts[:3]
                for country_code in country_codes.split(","):
                    self.country_timezones_by_code.setdefault(country_code, []).append(timezone_name)

        for country_code, country_name in self.country_names_by_code.items():
            self.lookup_to_code[_normalize_lookup(country_name)] = country_code

        for alias, country_code in COUNTRY_ALIASES.items():
            self.lookup_to_code[_normalize_lookup(alias)] = country_code

        logger.info(
            "World time tables loaded (%s countries, %s timezone mappings)",
            len(self.country_names_by_code),
            len(self.country_timezones_by_code),
        )

    def _resolve_country_code(self, query: str) -> tuple[str | None, list[str]]:
        """Resolve a user query to a country code, with suggestions if needed."""
        normalized = _normalize_lookup(query)
        if not normalized:
            return None, []

        exact = self.lookup_to_code.get(normalized)
        if exact:
            return exact, []

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
        """Autocomplete country names and aliases."""
        normalized = _normalize_lookup(current)
        seen_codes = set()
        choices: list[app_commands.Choice[str]] = []

        for country_code, country_name in sorted(self.country_names_by_code.items(), key=lambda item: item[1]):
            if normalized and normalized not in _normalize_lookup(country_name):
                continue
            if country_code in seen_codes:
                continue
            seen_codes.add(country_code)
            choices.append(app_commands.Choice(name=country_name, value=country_name))
            if len(choices) >= MAX_AUTOCOMPLETE_CHOICES:
                return choices

        for alias, country_code in COUNTRY_ALIASES.items():
            if country_code in seen_codes:
                continue
            if normalized and normalized not in _normalize_lookup(alias):
                continue
            country_name = self.country_names_by_code.get(country_code, alias.title())
            seen_codes.add(country_code)
            choices.append(app_commands.Choice(name=f"{country_name} ({alias.upper()})", value=country_name))
            if len(choices) >= MAX_AUTOCOMPLETE_CHOICES:
                break

        return choices

    @app_commands.command(name="time-country", description="Check the current time for a country")
    @app_commands.describe(country="Country name, such as Japan, United States, or Australia")
    @app_commands.autocomplete(country=_country_autocomplete)
    async def time_country(self, interaction: discord.Interaction, country: str):
        """Show the current local time for all timezones in a country."""
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

        now_utc = datetime.utcnow()
        embed = EmbedFactory.create(
            title=f"🕒 Current Time in {country_name}",
            description=(
                "This country has one timezone."
                if len(timezones) == 1
                else f"This country currently spans **{len(timezones)}** timezones."
            ),
            color=EmbedColor.INFO
        )

        for timezone_name in timezones[:MAX_TIMEZONE_FIELDS]:
            local_now = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(timezone_name))
            offset = local_now.utcoffset()
            total_minutes = int(offset.total_seconds() // 60) if offset else 0
            sign = "+" if total_minutes >= 0 else "-"
            abs_minutes = abs(total_minutes)
            offset_label = f"UTC{sign}{abs_minutes // 60:02d}:{abs_minutes % 60:02d}"
            embed.add_field(
                name=_friendly_timezone_name(timezone_name),
                value=(
                    f"**{local_now.strftime('%H:%M:%S')}**\n"
                    f"{local_now.strftime('%A, %d %B %Y')}\n"
                    f"{offset_label} · {local_now.tzname() or timezone_name}"
                ),
                inline=False
            )

        if len(timezones) > MAX_TIMEZONE_FIELDS:
            embed.set_footer(text=f"Showing the first {MAX_TIMEZONE_FIELDS} timezone entries.")

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(WorldTime(bot, bot.db, bot.config))
