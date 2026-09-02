"""
Verification Cog for Logiq
Handles user verification with multiple methods
"""

import base64
import discord
from discord import app_commands
from discord.ext import commands, tasks
from pathlib import Path
from datetime import timedelta
from urllib.parse import urlparse
import random
import re
import string
from typing import Optional
import logging
from io import BytesIO
import os

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pymongo import ReturnDocument

from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)
DEFAULT_WELCOME_CARD_MESSAGE = "Hey {user}, Welcome to **{server}**!"
DEFAULT_WELCOME_CARD_TITLE = "Welcome {display_name}"
DEFAULT_WELCOME_CARD_SUBTITLE = "Member #{member_count}"
DEFAULT_WELCOME_CARD_ACCENT = "#F5B8C7"
DEFAULT_WELCOME_CARD_TEXT = "#F1C1CC"
DEFAULT_WELCOME_CARD_SIZE = (1200, 520)
DEFAULT_WELCOME_CARD_TITLE_SIZE = 74
DEFAULT_WELCOME_CARD_SUBTITLE_SIZE = 44
WELCOME_CARD_BACKGROUND_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_VERIFICATION_MODE = "button"
CAPTCHA_CODE_LENGTH = 6
WELCOME_MEMBER_POSITION_COLLECTION = "welcome_member_positions"
PLATFORM_IDENTITY_RETENTION = timedelta(days=7)
VERIFICATION_PLATFORM_CHOICES = ("twitch", "youtube", "kick")
VERIFICATION_PLATFORM_LABELS = {
    "twitch": "Twitch",
    "youtube": "YouTube",
    "kick": "Kick",
}
VERIFICATION_PLATFORM_EMOJIS = {
    "twitch": "🟣",
    "youtube": "🔴",
    "kick": "🟢",
}
FONT_SEARCH_DIRS = [
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
    "/usr/share/fonts/truetype/liberation2",
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/opentype/noto",
    "/usr/local/share/fonts",
    "/System/Library/Fonts/Supplemental",
    "/System/Library/Fonts",
    "/Library/Fonts"
]
BUNDLED_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
WELCOME_CARD_FONT_PRESETS = {
    "noto_sans": {
        "label": "Noto Sans",
        "regular": "NotoSans-Regular.ttf",
        "bold": "NotoSans-Bold.ttf"
    },
    "poppins": {
        "label": "Poppins",
        "regular": "Poppins-Regular.ttf",
        "bold": "Poppins-Bold.ttf"
    },
    "playfair": {
        "label": "Playfair Display",
        "regular": "PlayfairDisplay-wght.ttf",
        "bold": "PlayfairDisplay-wght.ttf"
    },
    "fredoka": {
        "label": "Fredoka",
        "regular": "Fredoka-wdth-wght.ttf",
        "bold": "Fredoka-wdth-wght.ttf"
    },
    "nunito": {
        "label": "Nunito",
        "regular": "Nunito-wght.ttf",
        "bold": "Nunito-wght.ttf"
    }
}
WELCOME_CARD_FONT_CHOICES = [
    app_commands.Choice(name=font_data["label"], value=font_key)
    for font_key, font_data in WELCOME_CARD_FONT_PRESETS.items()
]

logger = logging.getLogger(__name__)


class VerificationButton(discord.ui.View):
    """Button-based verification view"""

    def __init__(self, cog: 'Verification'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green, custom_id="verify_button", emoji="✅")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle verification button click"""
        await self.cog.verify_user(interaction, source="verification")


class RulesAcceptView(discord.ui.View):
    """Persistent rules acceptance verification button."""

    def __init__(self, cog: 'Verification', button_label: str = "Accept"):
        super().__init__(timeout=None)
        button = discord.ui.Button(
            label=button_label,
            style=discord.ButtonStyle.green,
            custom_id="rules_accept_button",
            emoji="✅"
        )
        button.callback = self.accept_button
        self.cog = cog
        self.add_item(button)

    async def accept_button(self, interaction: discord.Interaction):
        """Handle rules acceptance button click."""
        await self.cog.handle_rules_accept(interaction)


class CaptchaModal(discord.ui.Modal, title="Verification Captcha"):
    """Captcha verification modal"""

    def __init__(
        self,
        correct_code: str,
        cog: 'Verification',
        guild_id: Optional[int] = None,
        source: str = "verification"
    ):
        super().__init__()
        self.correct_code = correct_code
        self.cog = cog
        self.guild_id = guild_id
        self.source = source

    captcha_code = discord.ui.TextInput(
        label="Enter the code shown",
        placeholder="Enter captcha code",
        required=True,
        max_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        """Handle captcha submission"""
        if self.captcha_code.value.upper() == self.correct_code:
            await self.cog.verify_user(interaction, source=self.source, guild_id=self.guild_id)
        else:
            await self.cog._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Failed", "Incorrect captcha code. Please try again."),
                ephemeral=True
            )


class CaptchaEntryView(discord.ui.View):
    """View that lets a specific user open the captcha modal."""

    def __init__(
        self,
        cog: 'Verification',
        *,
        user_id: int,
        guild_id: int,
        correct_code: str,
        source: str = "verification"
    ):
        super().__init__(timeout=1800)
        self.cog = cog
        self.user_id = user_id
        self.guild_id = guild_id
        self.correct_code = correct_code
        self.source = source

    @discord.ui.button(label="Enter Code", style=discord.ButtonStyle.success, emoji="✅")
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Open the code entry modal for the intended user."""
        if interaction.user.id != self.user_id:
            await self.cog._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Not For You", "This verification prompt belongs to another member."),
                ephemeral=True
            )
            return

        modal = CaptchaModal(
            self.correct_code,
            self.cog,
            guild_id=self.guild_id,
            source=self.source
        )
        await interaction.response.send_modal(modal)


class PlatformLinkModal(discord.ui.Modal):
    """Modal for submitting a streaming platform username or profile URL."""

    def __init__(self, cog: 'Verification', *, platform: str, guild_id: int, user_id: int):
        self.cog = cog
        self.platform = platform
        self.guild_id = guild_id
        self.user_id = user_id
        platform_label = VERIFICATION_PLATFORM_LABELS[platform]
        super().__init__(title=f"Link {platform_label} Profile")
        self.profile_input = discord.ui.TextInput(
            label=f"{platform_label} username, handle, or URL",
            placeholder=self._placeholder_for(platform),
            required=True,
            max_length=200
        )
        self.add_item(self.profile_input)

    def _placeholder_for(self, platform: str) -> str:
        if platform == "twitch":
            return "e.g. blamevita or https://twitch.tv/blamevita"
        if platform == "youtube":
            return "e.g. @GoogleDevelopers or https://youtube.com/@GoogleDevelopers"
        return "e.g. xqc or https://kick.com/xqc"

    async def on_submit(self, interaction: discord.Interaction):
        """Validate and complete the selected platform link."""
        if interaction.user.id != self.user_id:
            await self.cog._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Not For You", "This platform-link prompt belongs to another member."),
                ephemeral=True
            )
            return

        await self.cog.complete_platform_link(
            interaction,
            platform=self.platform,
            raw_value=str(self.profile_input.value),
            guild_id=self.guild_id
        )


class PlatformLinkView(discord.ui.View):
    """Prompt a member to choose which streaming platform they use."""

    def __init__(self, cog: 'Verification', *, guild_id: int, user_id: int):
        super().__init__(timeout=1800)
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Restrict the prompt to the intended member."""
        if interaction.user.id != self.user_id:
            await self.cog._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Not For You", "This platform-link prompt belongs to another member."),
                ephemeral=True
            )
            return False
        return True

    async def _open_platform_modal(self, interaction: discord.Interaction, platform: str) -> None:
        modal = PlatformLinkModal(
            self.cog,
            platform=platform,
            guild_id=self.guild_id,
            user_id=self.user_id
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Twitch", style=discord.ButtonStyle.secondary, emoji="🟣")
    async def twitch_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_platform_modal(interaction, "twitch")

    @discord.ui.button(label="YouTube", style=discord.ButtonStyle.secondary, emoji="🔴")
    async def youtube_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_platform_modal(interaction, "youtube")

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.secondary, emoji="🟢")
    async def kick_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open_platform_modal(interaction, "kick")


class Verification(commands.Cog):
    """Verification system cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('verification', {})
        self.session = None
        self.bot.add_view(VerificationButton(self))
        self.bot.add_view(RulesAcceptView(self))
        self.cleanup_platform_identity_cache.start()

    def cog_unload(self):
        """Cleanup resources on cog unload."""
        self.cleanup_platform_identity_cache.cancel()
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())

    async def get_session(self):
        """Get or create an aiohttp session."""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def _send_interaction_embed(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        ephemeral: bool = True
    ) -> None:
        """Respond to an interaction safely in guilds and DMs."""
        response_kwargs = {"embed": embed}
        if interaction.guild is not None:
            response_kwargs["ephemeral"] = ephemeral

        if interaction.response.is_done():
            await interaction.followup.send(**response_kwargs)
        else:
            await interaction.response.send_message(**response_kwargs)

    async def _defer_interaction_if_needed(
        self,
        interaction: discord.Interaction,
        *,
        ephemeral: bool = True,
        thinking: bool = True
    ) -> None:
        """Acknowledge slow verification interactions before Discord's timeout."""
        if interaction.response.is_done():
            return

        try:
            if interaction.guild is None:
                await interaction.response.defer(thinking=thinking)
            else:
                await interaction.response.defer(ephemeral=ephemeral, thinking=thinking)
        except discord.HTTPException as error:
            logger.warning("Failed to defer verification interaction %s: %s", interaction.id, error)

    def _get_verification_mode(self, guild_config: dict) -> str:
        """Get the current verification mode used after a member accepts rules."""
        mode = (
            guild_config.get("rules_verification_mode")
            or guild_config.get("verification_type")
            or DEFAULT_VERIFICATION_MODE
        )
        mode = str(mode).lower()
        return mode if mode in {"button", "captcha"} else DEFAULT_VERIFICATION_MODE

    def _generate_captcha_code(self) -> str:
        """Generate a short verification code."""
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=CAPTCHA_CODE_LENGTH))

    def _render_text_template(self, template: Optional[str], context: dict, fallback: str) -> str:
        """Render a text template safely using known placeholders."""
        source = template or fallback
        try:
            return source.format(**context)
        except KeyError as error:
            missing = error.args[0]
            logger.warning("Welcome template missing placeholder %s, falling back to default", missing)
            return fallback.format(**context)

    def _get_rules_button_label(self, guild_config: dict) -> str:
        """Get the configured rules acceptance button label."""
        return guild_config.get("rules_button_label") or "Accept"

    def _build_verification_context(self, member: discord.Member) -> dict:
        """Build a shared placeholder context for verification messages."""
        return {
            "user": member.mention,
            "username": member.name,
            "display_name": member.display_name,
            "server": member.guild.name
        }

    def _get_verification_signpost_message(self, guild_config: dict, member: discord.Member) -> Optional[str]:
        """Render the optional post-verification signpost message."""
        template = str(guild_config.get("verification_signpost_message") or "").strip()
        if not template:
            return None

        context = self._build_verification_context(member)
        try:
            return template.format(**context)
        except KeyError as error:
            logger.warning("Verification signpost template missing placeholder %s; using raw template.", error.args[0])
            return template

    def get_rules_accept_view(self, guild_config: dict) -> RulesAcceptView:
        """Create a rules acceptance view using the saved button label."""
        return RulesAcceptView(self, button_label=self._get_rules_button_label(guild_config))

    def _platform_link_enabled(self, guild_config: Optional[dict]) -> bool:
        """Whether the optional platform-link verification stage is enabled."""
        if not guild_config:
            return False
        return bool(guild_config.get("platform_link_enabled", False))

    def _get_platform_role_id(self, guild_config: dict, platform: str) -> Optional[int]:
        """Get the configured role ID for a platform."""
        role_map = guild_config.get("platform_link_roles", {}) or {}
        role_id = role_map.get(platform)
        return int(role_id) if role_id else None

    def _get_youtube_api_key(self) -> Optional[str]:
        """Load the YouTube Data API key."""
        return os.getenv("YOUTUBE_API_KEY") or self.config.get("api_keys", {}).get("youtube")

    def _get_kick_credentials(self) -> tuple[Optional[str], Optional[str]]:
        """Load Kick API credentials from environment or config."""
        client_id = os.getenv("KICK_CLIENT_ID") or self.config.get("api_keys", {}).get("kick_client_id")
        client_secret = os.getenv("KICK_CLIENT_SECRET") or self.config.get("api_keys", {}).get("kick_client_secret")
        return client_id, client_secret

    async def _get_kick_access_token(self) -> tuple[Optional[str], Optional[str]]:
        """Get or refresh the Kick app access token."""
        token = getattr(self, "_kick_access_token", None)
        expires_at = getattr(self, "_kick_token_expires_at", None)
        if token and expires_at and discord.utils.utcnow() < expires_at:
            return token, None

        client_id, client_secret = self._get_kick_credentials()
        if not client_id or not client_secret:
            return None, "KICK_CLIENT_ID or KICK_CLIENT_SECRET is missing."

        session = await self.get_session()
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials"
        }

        try:
            async with session.post("https://id.kick.com/oauth/token", data=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to get Kick access token: %s %s", response.status, body)
                    return None, f"Kick token request failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Failed to contact Kick for access token: %s", error, exc_info=True)
            return None, "Could not contact Kick while requesting an access token."

        access_token = data.get("access_token")
        if not access_token:
            logger.error("Kick token response was missing access_token: %s", data)
            return None, "Kick did not return an access token."

        expires_in = max(int(data.get("expires_in", 0)) - 60, 60)
        self._kick_access_token = access_token
        self._kick_token_expires_at = discord.utils.utcnow() + timedelta(seconds=expires_in)
        return access_token, None

    def _normalize_twitch_username(self, value: str) -> str:
        """Normalize a Twitch username or URL to a login name."""
        candidate = value.strip()
        if "twitch.tv" in candidate.lower():
            parsed = urlparse(candidate)
            candidate = parsed.path.strip("/").split("/")[0] if parsed.path else ""
        return candidate.strip().lstrip("@").lower()

    def _parse_youtube_input(self, value: str) -> tuple[str, str]:
        """Normalize YouTube input into a lookup type and value."""
        candidate = value.strip()
        if "youtube.com" in candidate.lower() or "youtu.be" in candidate.lower():
            parsed = urlparse(candidate)
            path = parsed.path.strip("/")
            if path.startswith("@"):
                return "handle", path
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] == "channel":
                return "id", parts[1]
            if len(parts) >= 2 and parts[0] == "user":
                return "username", parts[1]
        if candidate.startswith("@"):
            return "handle", candidate
        if candidate.startswith("UC") and len(candidate) >= 20:
            return "id", candidate
        return "handle", candidate

    def _normalize_kick_slug(self, value: str) -> str:
        """Normalize a Kick username or URL to a channel slug."""
        candidate = value.strip()
        if "kick.com" in candidate.lower():
            parsed = urlparse(candidate)
            candidate = parsed.path.strip("/").split("/")[0] if parsed.path else ""
        return candidate.strip().lstrip("@").lower()

    async def _resolve_twitch_profile(self, raw_value: str) -> tuple[Optional[dict], Optional[str]]:
        """Verify a Twitch profile exists and return normalized data."""
        username = self._normalize_twitch_username(raw_value)
        if not username:
            return None, "Please provide a valid Twitch username or profile URL."

        social_alerts = self.bot.get_cog("SocialAlerts")
        if social_alerts and hasattr(social_alerts, "_fetch_twitch_user"):
            user, error = await social_alerts._fetch_twitch_user(username)  # type: ignore[attr-defined]
        else:
            return None, "The Twitch lookup service is unavailable right now."

        if error or not user:
            return None, error or f"No Twitch channel found for `{username}`."

        login = user.get("login") or username
        return {
            "platform": "twitch",
            "username": login,
            "display_name": user.get("display_name") or login,
            "profile_url": f"https://twitch.tv/{login}",
            "profile_image_url": user.get("profile_image_url")
        }, None

    async def _resolve_youtube_profile(self, raw_value: str) -> tuple[Optional[dict], Optional[str]]:
        """Verify a YouTube profile exists and return normalized data."""
        api_key = self._get_youtube_api_key()
        if not api_key:
            return None, "YOUTUBE_API_KEY is missing."

        lookup_type, lookup_value = self._parse_youtube_input(raw_value)
        if not lookup_value:
            return None, "Please provide a valid YouTube handle, channel ID, or profile URL."

        params = {"part": "snippet", "key": api_key}
        if lookup_type == "handle":
            params["forHandle"] = lookup_value
        elif lookup_type == "username":
            params["forUsername"] = lookup_value
        else:
            params["id"] = lookup_value

        session = await self.get_session()
        try:
            async with session.get("https://www.googleapis.com/youtube/v3/channels", params=params) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to fetch YouTube channel %s: %s %s", lookup_value, response.status, body)
                    return None, f"YouTube channel lookup failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching YouTube channel %s: %s", lookup_value, error, exc_info=True)
            return None, "Could not contact YouTube while looking up the channel."

        items = data.get("items", [])
        if not items and lookup_type == "handle":
            fallback_params = {"part": "snippet", "key": api_key, "forUsername": lookup_value.lstrip("@")}
            try:
                async with session.get("https://www.googleapis.com/youtube/v3/channels", params=fallback_params) as response:
                    if response.status == 200:
                        fallback_data = await response.json()
                        items = fallback_data.get("items", [])
            except aiohttp.ClientError:
                items = []

        if not items:
            return None, (
                "No YouTube channel found for that value. Please use a channel handle like `@YourName`, "
                "a channel ID, or a direct channel URL."
            )

        channel = items[0]
        snippet = channel.get("snippet", {})
        custom_url = snippet.get("customUrl") or lookup_value
        normalized_username = custom_url.lstrip("@")
        profile_url = (
            f"https://youtube.com/{custom_url}"
            if str(custom_url).startswith("@")
            else f"https://youtube.com/channel/{channel.get('id')}"
        )
        return {
            "platform": "youtube",
            "username": normalized_username,
            "display_name": normalized_username or snippet.get("title") or channel.get("id"),
            "profile_url": profile_url,
            "profile_image_url": ((snippet.get("thumbnails") or {}).get("default") or {}).get("url")
        }, None

    async def _resolve_kick_profile(self, raw_value: str) -> tuple[Optional[dict], Optional[str]]:
        """Verify a Kick profile exists and return normalized data."""
        slug = self._normalize_kick_slug(raw_value)
        if not slug:
            return None, "Please provide a valid Kick username or channel URL."

        token, token_error = await self._get_kick_access_token()
        if not token:
            return None, token_error or "Kick credentials are unavailable."

        session = await self.get_session()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with session.get("https://api.kick.com/public/v1/channels", params=[("slug", slug)], headers=headers) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to fetch Kick channel %s: %s %s", slug, response.status, body)
                    return None, f"Kick channel lookup failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching Kick channel %s: %s", slug, error, exc_info=True)
            return None, "Could not contact Kick while looking up the channel."

        channels = data.get("data", [])
        if not channels:
            return None, f"No Kick channel found for `{slug}`."

        channel = channels[0]
        resolved_slug = channel.get("slug") or slug
        return {
            "platform": "kick",
            "username": resolved_slug,
            "display_name": resolved_slug,
            "profile_url": f"https://kick.com/{resolved_slug}",
            "profile_image_url": channel.get("banner_picture")
        }, None

    async def _resolve_platform_profile(self, platform: str, raw_value: str) -> tuple[Optional[dict], Optional[str]]:
        """Verify the selected platform profile exists."""
        if platform == "twitch":
            return await self._resolve_twitch_profile(raw_value)
        if platform == "youtube":
            return await self._resolve_youtube_profile(raw_value)
        if platform == "kick":
            return await self._resolve_kick_profile(raw_value)
        return None, "Unsupported platform."

    async def _ensure_user_record(self, user_id: int, guild_id: int) -> dict:
        """Fetch or create the per-guild user document."""
        user_data = await self.db.get_user(user_id, guild_id)
        if not user_data:
            user_data = await self.db.create_user(user_id, guild_id)
        return user_data

    async def _clear_platform_identity(self, user_id: int, guild_id: int) -> None:
        """Delete the stored platform identity for a single member."""
        await self.db.update_user(
            user_id,
            guild_id,
            {
                "viewer_platform": None,
                "viewer_platform_username": None,
                "viewer_platform_display_name": None,
                "viewer_platform_url": None,
                "viewer_platform_profile_image_url": None,
                "viewer_platform_saved_at": None,
                "viewer_platform_expires_at": None
            }
        )

    async def _save_platform_identity(self, user_id: int, guild_id: int, profile_data: dict) -> None:
        """Persist the member's selected viewing platform identity."""
        await self._ensure_user_record(user_id, guild_id)
        saved_at = discord.utils.utcnow()
        expires_at = saved_at + PLATFORM_IDENTITY_RETENTION
        await self.db.update_user(
            user_id,
            guild_id,
            {
                "viewer_platform": profile_data["platform"],
                "viewer_platform_username": profile_data["username"],
                "viewer_platform_display_name": profile_data["display_name"],
                "viewer_platform_url": profile_data["profile_url"],
                "viewer_platform_profile_image_url": profile_data.get("profile_image_url"),
                "viewer_platform_saved_at": saved_at.timestamp(),
                "viewer_platform_expires_at": expires_at.timestamp()
            }
        )

    async def _get_saved_platform_identity(self, user_data: Optional[dict]) -> Optional[dict]:
        """Return a saved platform identity if present and not expired."""
        if not user_data:
            return None
        expires_at = user_data.get("viewer_platform_expires_at")
        if not expires_at or float(expires_at) <= discord.utils.utcnow().timestamp():
            user_id = user_data.get("user_id")
            guild_id = user_data.get("guild_id")
            if user_id and guild_id and any(
                user_data.get(field)
                for field in (
                    "viewer_platform",
                    "viewer_platform_username",
                    "viewer_platform_display_name",
                    "viewer_platform_url",
                    "viewer_platform_profile_image_url"
                )
            ):
                await self._clear_platform_identity(int(user_id), int(guild_id))
            return None
        platform = user_data.get("viewer_platform")
        username = user_data.get("viewer_platform_username")
        if platform not in VERIFICATION_PLATFORM_CHOICES or not username:
            return None
        return {
            "platform": platform,
            "username": username,
            "display_name": user_data.get("viewer_platform_display_name") or username,
            "profile_url": user_data.get("viewer_platform_url") or "",
            "profile_image_url": user_data.get("viewer_platform_profile_image_url")
        }

    @tasks.loop(hours=24)
    async def cleanup_platform_identity_cache(self) -> None:
        """Delete stored platform identities after the retention window expires."""
        expiry_cutoff = discord.utils.utcnow().timestamp()
        result = await self.db.db.users.update_many(
            {
                "viewer_platform_expires_at": {"$lte": expiry_cutoff},
                "viewer_platform": {"$in": list(VERIFICATION_PLATFORM_CHOICES)}
            },
            {
                "$set": {
                    "viewer_platform": None,
                    "viewer_platform_username": None,
                    "viewer_platform_display_name": None,
                    "viewer_platform_url": None,
                    "viewer_platform_profile_image_url": None,
                    "viewer_platform_saved_at": None,
                    "viewer_platform_expires_at": None
                }
            }
        )
        if result.modified_count:
            logger.info("Cleared %s expired verification platform identities", result.modified_count)

        legacy_result = await self.db.db.users.update_many(
            {
                "viewer_platform": {"$in": list(VERIFICATION_PLATFORM_CHOICES)},
                "$or": [
                    {"viewer_platform_expires_at": {"$exists": False}},
                    {"viewer_platform_expires_at": None}
                ]
            },
            {
                "$set": {
                    "viewer_platform": None,
                    "viewer_platform_username": None,
                    "viewer_platform_display_name": None,
                    "viewer_platform_url": None,
                    "viewer_platform_profile_image_url": None,
                    "viewer_platform_saved_at": None,
                    "viewer_platform_expires_at": None
                }
            }
        )
        if legacy_result.modified_count:
            logger.info("Cleared %s legacy verification platform identities without expiry metadata", legacy_result.modified_count)

    @cleanup_platform_identity_cache.before_loop
    async def before_cleanup_platform_identity_cache(self) -> None:
        """Wait until the bot is ready before clearing stored identities."""
        await self.bot.wait_until_ready()

    async def _update_member_nickname(self, member: discord.Member, nickname: str) -> tuple[bool, Optional[str]]:
        """Attempt to update a member's nickname to match their platform username."""
        nickname = nickname.strip()
        if not nickname:
            return False, "No nickname was provided."

        if not member.guild.me.guild_permissions.manage_nicknames:
            return False, "I don't have the **Manage Nicknames** permission."

        if member.guild.owner_id == member.id:
            return False, "I can't change the server owner's nickname."

        if member.guild.me.top_role <= member.top_role:
            return False, "My top role is not high enough to change that nickname."

        safe_nickname = nickname[:32]
        try:
            await member.edit(nick=safe_nickname, reason="Verification platform identity sync")
            return True, None
        except discord.Forbidden:
            return False, "Discord denied the nickname change because of role hierarchy."
        except discord.HTTPException as error:
            return False, f"Discord rejected the nickname change: {error}"

    async def _start_platform_link_prompt(
        self,
        interaction: discord.Interaction,
        *,
        guild: discord.Guild,
        member: discord.Member,
        guild_config: dict,
        redo: bool = False
    ) -> None:
        """Prompt a member to choose and submit their primary viewing platform."""
        missing_roles = [
            VERIFICATION_PLATFORM_LABELS[platform]
            for platform in VERIFICATION_PLATFORM_CHOICES
            if not self._get_platform_role_id(guild_config, platform)
        ]
        if missing_roles:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error(
                    "Platform Verification Not Ready",
                    "An admin still needs to configure roles for: " + ", ".join(missing_roles)
                ),
                ephemeral=True
            )
            return

        description = (
            "Choose the streaming platform you primarily watch from, then submit your profile username or URL.\n\n"
            "Once it checks out, I'll assign your platform role, sync your nickname, and finish your verification."
        )
        if redo:
            description = (
                "Choose your current main viewing platform and submit your updated profile username or URL.\n\n"
                "I'll swap your platform role and update your server nickname."
            )

        embed = EmbedFactory.create(
            title="🎯 Link Your Viewing Platform",
            description=description,
            color=EmbedColor.PRIMARY
        )
        view = PlatformLinkView(self, guild_id=guild.id, user_id=member.id)
        response_kwargs = {"embed": embed, "view": view}
        if interaction.guild is not None:
            response_kwargs["ephemeral"] = True

        if interaction.response.is_done():
            await interaction.followup.send(**response_kwargs)
        else:
            await interaction.response.send_message(**response_kwargs)

    async def _apply_platform_verification(
        self,
        member: discord.Member,
        guild_config: dict,
        *,
        profile_data: dict
    ) -> tuple[bool, list[str], list[str], Optional[str]]:
        """Apply platform role, remove old platform roles, add verified role, and sync nickname."""
        guild = member.guild
        verified_role_id = guild_config.get("verified_role")
        verified_role = guild.get_role(verified_role_id) if verified_role_id else None
        if verified_role is None:
            return False, [], [], "Verified role is not configured correctly."

        platform = profile_data["platform"]
        target_role_id = self._get_platform_role_id(guild_config, platform)
        target_role = guild.get_role(target_role_id) if target_role_id else None
        if target_role is None:
            return False, [], [], f"The configured {VERIFICATION_PLATFORM_LABELS[platform]} role no longer exists."

        platform_roles = []
        for platform_name in VERIFICATION_PLATFORM_CHOICES:
            role_id = self._get_platform_role_id(guild_config, platform_name)
            role = guild.get_role(role_id) if role_id else None
            if role:
                platform_roles.append(role)

        roles_to_remove = [role for role in platform_roles if role != target_role and role in member.roles]
        roles_to_add = [role for role in {target_role, verified_role} if role and role not in member.roles]

        try:
            if roles_to_remove:
                await member.remove_roles(*roles_to_remove, reason="Verification platform refresh")
            if roles_to_add:
                await member.add_roles(*roles_to_add, reason="Verification platform assignment")
        except discord.Forbidden:
            return False, roles_to_add, roles_to_remove, "I don't have permission to manage one or more required roles."
        except discord.HTTPException as error:
            return False, roles_to_add, roles_to_remove, f"Discord rejected the role update: {error}"

        nickname_updated, nickname_error = await self._update_member_nickname(member, profile_data["display_name"])
        return True, roles_to_add, roles_to_remove, None if nickname_updated else nickname_error

    async def save_rules_panel_config(
        self,
        guild_id: int,
        *,
        channel_id: int,
        message_id: Optional[int] = None,
        panel_message: Optional[str],
        title: str,
        description: str,
        color: str,
        image_url: Optional[str],
        footer: Optional[str],
        button_label: str,
        enabled: bool = True
    ) -> dict:
        """Persist rules-panel verification settings for a guild."""
        guild_config = await self.db.get_guild(guild_id)
        if not guild_config:
            guild_config = await self.db.create_guild(guild_id)

        update_data = {
            "verification_enabled": True,
            "rules_panel_enabled": enabled,
            "rules_channel": channel_id,
            "rules_message_id": message_id,
            "rules_panel_message": panel_message,
            "rules_title": title,
            "rules_description": description,
            "rules_color": color,
            "rules_image_url": image_url,
            "rules_footer": footer,
            "rules_button_label": button_label,
            "verification_method": "rules_panel",
            "rules_verification_mode": self._get_verification_mode(guild_config)
        }
        await self.db.update_guild(guild_id, update_data)
        guild_config.update(update_data)
        return guild_config

    def _parse_hex_color(self, value: Optional[str], fallback: str) -> tuple[int, int, int]:
        """Parse a hex color into an RGB tuple with fallback support."""
        color_value = (value or fallback).strip().lstrip("#")
        if len(color_value) != 6:
            color_value = fallback.lstrip("#")
        try:
            return tuple(int(color_value[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            safe_fallback = fallback.lstrip("#")
            return tuple(int(safe_fallback[i:i + 2], 16) for i in (0, 2, 4))

    def _is_valid_hex_color(self, value: str) -> bool:
        """Check whether a color string is a valid 6-digit hex value."""
        candidate = value.strip().lstrip("#")
        if len(candidate) != 6:
            return False
        try:
            int(candidate, 16)
            return True
        except ValueError:
            return False

    def _clamp_font_size(self, value: Optional[int], fallback: int) -> int:
        """Clamp configurable font sizes to a safe range."""
        if value is None:
            return fallback
        return max(24, min(140, int(value)))

    async def _resolve_verification_context(
        self,
        interaction: discord.Interaction,
        guild_id: Optional[int] = None
    ) -> tuple[Optional[discord.Guild], Optional[discord.Member], Optional[dict]]:
        """Resolve the guild, member, and config for guild or DM verification interactions."""
        guild = interaction.guild
        if guild is None and guild_id is not None:
            guild = self.bot.get_guild(guild_id)

        if guild is None:
            return None, None, None

        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if member is None:
            try:
                member = await guild.fetch_member(interaction.user.id)
            except discord.HTTPException:
                member = None

        guild_config = await self.db.get_guild(guild.id)
        return guild, member, guild_config

    async def _send_rules_panel_captcha(self, interaction: discord.Interaction, guild_config: dict) -> None:
        """Send the captcha challenge privately after a member accepts the rules panel."""
        guild = interaction.guild
        if guild is None:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Error", "This rules acceptance button must be used inside the server."),
                ephemeral=True
            )
            return

        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if member is None:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Error", "I couldn't resolve your membership in this server."),
                ephemeral=True
            )
            return

        code = self._generate_captcha_code()
        rules_channel = guild.get_channel(guild_config.get("rules_channel")) if guild_config.get("rules_channel") else None
        button_label = self._get_rules_button_label(guild_config)

        embed = EmbedFactory.create(
            title=f"🔐 Complete Verification for {guild.name}",
            description=(
                "You've clicked the rules acceptance button.\n\n"
                f"**Step 1:** Read this code: `{code}`\n"
                "**Step 2:** Click the button below\n"
                "**Step 3:** Enter the code to unlock the server"
            ),
            color=EmbedColor.PRIMARY,
            footer=f"Triggered from {rules_channel.name}" if rules_channel else None
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        view = CaptchaEntryView(
            self,
            user_id=member.id,
            guild_id=guild.id,
            correct_code=code,
            source="rules_accept_captcha"
        )

        try:
            await member.send(embed=embed, view=view)
        except discord.Forbidden:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error(
                    "DMs Are Closed",
                    (
                        "I couldn't send you the captcha in DMs. Please enable direct messages from this server, "
                        f"then click **{button_label}** again."
                    )
                ),
                ephemeral=True
            )
            return
        except Exception as error:
            logger.error("Failed to send rules captcha DM to %s in %s: %s", member, guild, error, exc_info=True)
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Error", "I couldn't send your captcha DM. Please try again in a moment."),
                ephemeral=True
            )
            return

        await self._send_interaction_embed(
            interaction,
            embed=EmbedFactory.info(
                "Check Your DMs",
                f"I've sent you a captcha in DMs. Complete it there to unlock **{guild.name}**."
            ),
            ephemeral=True
        )

    async def handle_rules_accept(self, interaction: discord.Interaction) -> None:
        """Route rules acceptance into instant verification or DM captcha."""
        await self._defer_interaction_if_needed(interaction)
        guild, member, guild_config = await self._resolve_verification_context(interaction)
        if guild is None or guild_config is None or member is None:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Error", "I couldn't load this server's verification settings."),
                ephemeral=True
            )
            return

        verified_role_id = guild_config.get("verified_role")
        if not verified_role_id:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Unavailable", "No verified role is configured for this server."),
                ephemeral=True
            )
            return

        verified_role = guild.get_role(verified_role_id)
        if verified_role is None:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Unavailable", "The configured verified role no longer exists."),
                ephemeral=True
            )
            return

        if verified_role in member.roles:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.info("Already Verified", "You already have access to the server."),
                ephemeral=True
            )
            return

        mode = self._get_verification_mode(guild_config)
        if mode == "captcha":
            await self._send_rules_panel_captcha(interaction, guild_config)
            return

        await self.verify_user(interaction, source="rules_accept", guild_id=guild.id)

    def _load_font(
        self,
        size: int,
        *,
        font_key: Optional[str] = None,
        bold: bool = False
    ) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, bool]:
        """Load a bundled scalable font first, then fall back to host fonts if needed."""
        candidates = []
        preset = WELCOME_CARD_FONT_PRESETS[self._get_font_key(font_key)]
        if bold:
            candidates.extend([
                preset["bold"],
                "DejaVuSans-Bold.ttf",
                "LiberationSans-Bold.ttf",
                "Arial Bold.ttf",
                "Arial Bold",
                "ArialBD.ttf",
                "FreeSansBold.ttf",
                "Helvetica.ttc"
            ])
        else:
            candidates.extend([
                preset["regular"],
                "DejaVuSans.ttf",
                "LiberationSans-Regular.ttf",
                "Arial.ttf",
                "Arial",
                "FreeSans.ttf",
                "Helvetica.ttc"
            ])

        if BUNDLED_FONT_DIR.exists():
            for candidate in candidates:
                font_path = BUNDLED_FONT_DIR / candidate
                if not font_path.exists():
                    continue
                try:
                    return ImageFont.truetype(str(font_path), size), False
                except OSError:
                    continue

        for candidate in candidates:
            try:
                return ImageFont.truetype(candidate, size), False
            except OSError:
                continue

        pil_font_dir = Path(ImageFont.__file__).resolve().parent
        for directory in [pil_font_dir, pil_font_dir / "fonts", pil_font_dir.parent / "fonts"]:
            if not directory.exists():
                continue
            for candidate in candidates:
                font_path = directory / candidate
                if not font_path.exists():
                    continue
                try:
                    return ImageFont.truetype(str(font_path), size), False
                except OSError:
                    continue

        for search_dir in FONT_SEARCH_DIRS:
            directory = Path(search_dir)
            if not directory.exists():
                continue
            for candidate in candidates:
                font_path = directory / candidate
                if not font_path.exists():
                    continue
                try:
                    return ImageFont.truetype(str(font_path), size), False
                except OSError:
                    continue

            try:
                for font_path in directory.rglob("*.ttf"):
                    candidate_name = font_path.name.lower()
                    if bold and "bold" not in candidate_name:
                        continue
                    if not bold and "bold" in candidate_name:
                        continue
                    try:
                        return ImageFont.truetype(str(font_path), size), False
                    except OSError:
                        continue
            except OSError:
                continue

        logger.warning(
            "Welcome card font loader could not find a scalable font for size=%s bold=%s; using Pillow default font.",
            size,
            bold
        )
        return ImageFont.load_default(), True

    def _normalize_welcome_subtitle(self, subtitle_text: str, context: Optional[dict] = None) -> str:
        """Ensure bare member counts still render as a user-facing position string."""
        stripped = subtitle_text.strip()
        if stripped.isdigit():
            if context and "member_count" in context:
                return f"Member #{context['member_count']}"
            return f"Member #{stripped}"
        if stripped.startswith("#") and stripped[1:].isdigit():
            if context and "member_count" in context:
                return f"Member #{context['member_count']}"
            return f"Member {stripped}"
        static_member_match = re.fullmatch(r"(?i)member\s*#\s*(\d+)", stripped)
        if static_member_match and context and "member_count" in context:
            return f"Member #{context['member_count']}"
        return subtitle_text

    def _normalize_welcome_title(self, title_text: str, context: dict) -> str:
        """Prevent broken numeric-only titles from rendering on welcome cards."""
        stripped = title_text.strip()
        if stripped.isdigit():
            logger.warning("Welcome card title rendered as numeric-only value %s; falling back to default title.", stripped)
            return self._render_text_template(None, context, DEFAULT_WELCOME_CARD_TITLE)
        return title_text

    def _get_font_key(self, value: Optional[str]) -> str:
        """Resolve a saved font key to a known preset."""
        if value in WELCOME_CARD_FONT_PRESETS:
            return value
        return "noto_sans"

    def _get_font_label(self, value: Optional[str]) -> str:
        """Get the user-facing label for a welcome card font preset."""
        font_key = self._get_font_key(value)
        return WELCOME_CARD_FONT_PRESETS[font_key]["label"]

    async def _get_member_position(self, member: discord.Member) -> int:
        """Calculate a reliable current member position for welcome cards."""
        try:
            member_ids = set()
            async for guild_member in member.guild.fetch_members(limit=None):
                member_ids.add(guild_member.id)

            if member.id not in member_ids:
                member_ids.add(member.id)

            if member_ids:
                return len(member_ids)
        except Exception as error:
            logger.warning("Failed to fetch guild members for welcome card position: %s", error)

        return member.guild.member_count or len(member.guild.members) or 1

    async def _reserve_member_join_position(self, member: discord.Member, guild_config: dict) -> int:
        """Reserve a monotonic join position independent from mutable guild config."""
        baseline = await self._get_member_position(member)
        historical_joins = await self.db.db.analytics.count_documents({
            "guild_id": member.guild.id,
            "type": "member_join"
        })
        baseline = max(baseline, int(historical_joins or 0))
        floor_value = max(baseline - 1, 0)
        collection = self.db.db[WELCOME_MEMBER_POSITION_COLLECTION]

        await collection.update_one(
            {"guild_id": member.guild.id},
            {
                "$setOnInsert": {
                    "guild_id": member.guild.id,
                    "sequence": floor_value
                }
            },
            upsert=True
        )

        await collection.update_one(
            {
                "guild_id": member.guild.id,
                "sequence": {"$lt": floor_value}
            },
            {"$set": {"sequence": floor_value}}
        )

        counter = await collection.find_one_and_update(
            {"guild_id": member.guild.id},
            {"$inc": {"sequence": 1}},
            return_document=ReturnDocument.AFTER
        )
        return int(counter.get("sequence", baseline)) if counter else baseline

    async def _heal_welcome_card_config(self, guild: discord.Guild, guild_config: dict) -> None:
        """Repair previously corrupted numeric-only welcome card title/subtitle config."""
        update_data = {}
        raw_title = str(guild_config.get("welcome_card_title") or "").strip()
        raw_subtitle = str(guild_config.get("welcome_card_subtitle") or "").strip()

        if raw_title.isdigit():
            update_data["welcome_card_title"] = DEFAULT_WELCOME_CARD_TITLE
            guild_config["welcome_card_title"] = DEFAULT_WELCOME_CARD_TITLE

        if (
            raw_subtitle.isdigit()
            or (raw_subtitle.startswith("#") and raw_subtitle[1:].isdigit())
            or re.fullmatch(r"(?i)member\s*#\s*\d+", raw_subtitle)
        ):
            update_data["welcome_card_subtitle"] = DEFAULT_WELCOME_CARD_SUBTITLE
            guild_config["welcome_card_subtitle"] = DEFAULT_WELCOME_CARD_SUBTITLE

        if update_data:
            await self.db.update_guild(guild.id, update_data)
            logger.info("Healed stale welcome card config for guild %s: %s", guild.id, ", ".join(update_data.keys()))

    def _measure_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        *,
        target_size: int,
        bitmap_fallback: bool
    ) -> tuple[int, int]:
        """Measure text width/height, normalizing rendered height to the requested size."""
        bbox = draw.textbbox((0, 0), text, font=font)
        width = max(1, bbox[2] - bbox[0])
        height = max(1, bbox[3] - bbox[1])
        scale = self._get_text_scale(height, target_size, bitmap_fallback)
        return max(1, int(width * scale)), max(1, int(height * scale))

    def _get_text_scale(self, base_height: int, target_size: int, bitmap_fallback: bool) -> float:
        """Determine how much to scale rendered text toward the requested pixel height."""
        if base_height <= 0:
            return 1.0

        scale = target_size / base_height
        if bitmap_fallback:
            return max(scale, 1.0)

        # FreeType fonts can still render much smaller than the configured size on some hosts.
        # Normalize obvious mismatches while avoiding unnecessary resampling for near-matches.
        if 0.9 <= scale <= 1.1:
            return 1.0
        return scale

    def _draw_scaled_text(
        self,
        image: Image.Image,
        *,
        position: tuple[float, float],
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        fill: tuple[int, int, int] | tuple[int, int, int, int],
        target_size: int,
        bitmap_fallback: bool
    ) -> None:
        """Draw text, normalizing it to the requested size when the host font renders unexpectedly."""
        draw = ImageDraw.Draw(image)
        bbox = draw.textbbox((0, 0), text, font=font)
        base_width = max(1, bbox[2] - bbox[0])
        base_height = max(1, bbox[3] - bbox[1])
        scale = self._get_text_scale(base_height, target_size, bitmap_fallback)
        if scale == 1.0:
            draw.text(position, text, font=font, fill=fill)
            return

        mask_width = max(1, int(base_width * scale))
        mask_height = max(1, int(base_height * scale))

        mask = Image.new("L", (base_width + 8, base_height + 8), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.text((4 - bbox[0], 4 - bbox[1]), text, font=font, fill=255)
        resample = Image.Resampling.NEAREST if bitmap_fallback else Image.Resampling.LANCZOS
        resized_mask = mask.resize((mask_width, mask_height), resample)

        color_image = Image.new("RGBA", (mask_width, mask_height), fill)
        image.paste(color_image, (int(position[0]), int(position[1])), resized_mask)

    async def _fetch_url_image(self, url: str) -> Optional[Image.Image]:
        """Fetch an image from a remote URL."""
        session = await self.get_session()
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    logger.warning("Failed to fetch welcome card background %s: HTTP %s", url, response.status)
                    return None
                image_bytes = await response.read()
        except aiohttp.ClientError as error:
            logger.warning("Failed to fetch welcome card background %s: %s", url, error)
            return None

        try:
            return Image.open(BytesIO(image_bytes)).convert("RGBA")
        except OSError:
            logger.warning("Fetched welcome card background is not a valid image: %s", url)
            return None

    def _encode_welcome_background_image(self, image: Image.Image) -> str:
        """Normalize and encode a welcome background image for durable storage."""
        normalized = ImageOps.fit(
            image.convert("RGBA"),
            DEFAULT_WELCOME_CARD_SIZE,
            method=Image.Resampling.LANCZOS
        )
        output = BytesIO()
        normalized.save(output, format="PNG", optimize=True, compress_level=9)
        return base64.b64encode(output.getvalue()).decode("ascii")

    def _decode_welcome_background_image(self, encoded_image: Optional[str]) -> Optional[Image.Image]:
        """Decode a stored welcome background image."""
        if not encoded_image:
            return None

        try:
            image_bytes = base64.b64decode(encoded_image)
            return Image.open(BytesIO(image_bytes)).convert("RGBA")
        except (ValueError, OSError) as error:
            logger.warning("Stored welcome card background could not be decoded: %s", error)
            return None

    async def _store_welcome_background_image(
        self,
        guild_id: int,
        guild_config: dict,
        image_bytes: bytes,
        *,
        source_url: Optional[str]
    ) -> tuple[bool, Optional[str]]:
        """Persist a normalized welcome background image in guild config."""
        if len(image_bytes) > WELCOME_CARD_BACKGROUND_MAX_BYTES:
            return False, "Background image files must be 8 MB or smaller."

        try:
            image = Image.open(BytesIO(image_bytes)).convert("RGBA")
        except OSError:
            return False, "That background image could not be opened. Please use a valid PNG, JPG, or WEBP image."

        encoded_image = self._encode_welcome_background_image(image)
        update_data = {
            "welcome_card_background_image_data": encoded_image,
            "welcome_card_background_url": source_url.strip() if source_url else None
        }
        await self.db.update_guild(guild_id, update_data)
        guild_config.update(update_data)
        return True, None

    async def _cache_welcome_background_from_url(
        self,
        guild_id: int,
        guild_config: dict,
        url: str
    ) -> Optional[Image.Image]:
        """Fetch a remote background image once, then persist a durable copy."""
        background = await self._fetch_url_image(url)
        if background is None:
            return None

        encoded_image = self._encode_welcome_background_image(background)
        update_data = {
            "welcome_card_background_image_data": encoded_image,
            "welcome_card_background_url": url
        }
        await self.db.update_guild(guild_id, update_data)
        guild_config.update(update_data)
        logger.info("Cached welcome card background image for guild %s from %s", guild_id, url)
        return self._decode_welcome_background_image(encoded_image)

    async def _build_welcome_card(
        self,
        member: discord.Member,
        guild_config: dict,
        *,
        member_position: Optional[int] = None
    ) -> Optional[discord.File]:
        """Generate a welcome card image for a joining member."""
        width, height = DEFAULT_WELCOME_CARD_SIZE
        accent = self._parse_hex_color(guild_config.get("welcome_card_accent_color"), DEFAULT_WELCOME_CARD_ACCENT)
        text_color = self._parse_hex_color(guild_config.get("welcome_card_text_color"), DEFAULT_WELCOME_CARD_TEXT)
        background_data = guild_config.get("welcome_card_background_image_data")
        background_url = guild_config.get("welcome_card_background_url")
        background = self._decode_welcome_background_image(background_data)

        if background is None and background_url:
            background = await self._cache_welcome_background_from_url(member.guild.id, guild_config, background_url)

        if background is None:
            background = Image.new("RGBA", (width, height), (24, 24, 31, 255))
        else:
            background = ImageOps.fit(background, (width, height), method=Image.Resampling.LANCZOS)

        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        accent_soft = (*accent, 34)
        accent_mid = (*accent, 70)
        overlay_draw.ellipse((-220, -180, 560, 640), fill=accent_soft)
        overlay_draw.ellipse((680, -240, 1460, 500), fill=(255, 255, 255, 18))
        overlay_draw.ellipse((720, 160, 1420, 840), fill=accent_soft)
        overlay_draw.arc((-180, -140, 680, 760), start=200, end=350, fill=accent_mid, width=40)
        overlay_draw.arc((540, -220, 1340, 580), start=200, end=350, fill=(255, 255, 255, 30), width=28)
        overlay_draw.arc((640, 120, 1500, 980), start=15, end=160, fill=accent_mid, width=42)
        overlay = overlay.filter(ImageFilter.GaussianBlur(4))
        canvas = Image.alpha_composite(background, overlay)

        card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        card_draw = ImageDraw.Draw(card)
        card_draw.rounded_rectangle((36, 36, width - 36, height - 36), radius=36, fill=(15, 16, 24, 235))
        card_draw.rounded_rectangle((36, 36, width - 36, height - 36), radius=36, outline=(*accent, 70), width=3)
        card = card.filter(ImageFilter.GaussianBlur(0))
        canvas = Image.alpha_composite(canvas, card)

        avatar_bytes = await member.display_avatar.replace(size=256).read()
        avatar = Image.open(BytesIO(avatar_bytes)).convert("RGBA").resize((180, 180), Image.Resampling.LANCZOS)
        avatar_mask = Image.new("L", (180, 180), 0)
        ImageDraw.Draw(avatar_mask).ellipse((0, 0, 180, 180), fill=255)
        avatar_circle = ImageOps.fit(avatar, (180, 180), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
        avatar_circle.putalpha(avatar_mask)

        border = Image.new("RGBA", (210, 210), (0, 0, 0, 0))
        ImageDraw.Draw(border).ellipse((0, 0, 210, 210), fill=(*accent, 255))
        border.paste(avatar_circle, (15, 15), avatar_circle)

        avatar_x = (width - 210) // 2
        avatar_y = 96
        canvas.paste(border, (avatar_x, avatar_y), border)

        member_count = member_position if member_position is not None else await self._get_member_position(member)
        context = {
            "user": member.mention,
            "username": member.name,
            "display_name": member.display_name,
            "server": member.guild.name,
            "member_count": member_count
        }
        title_text = self._render_text_template(
            guild_config.get("welcome_card_title"),
            context,
            DEFAULT_WELCOME_CARD_TITLE
        )
        title_text = self._normalize_welcome_title(title_text, context)
        subtitle_text = self._render_text_template(
            guild_config.get("welcome_card_subtitle"),
            context,
            DEFAULT_WELCOME_CARD_SUBTITLE
        )
        subtitle_text = self._normalize_welcome_subtitle(subtitle_text, context)

        draw = ImageDraw.Draw(canvas)
        title_size = self._clamp_font_size(
            guild_config.get("welcome_card_title_size"),
            DEFAULT_WELCOME_CARD_TITLE_SIZE
        )
        subtitle_size = self._clamp_font_size(
            guild_config.get("welcome_card_subtitle_size"),
            DEFAULT_WELCOME_CARD_SUBTITLE_SIZE
        )
        title_font_key = self._get_font_key(guild_config.get("welcome_card_title_font"))
        subtitle_font_key = self._get_font_key(guild_config.get("welcome_card_subtitle_font"))
        title_font, title_bitmap_fallback = self._load_font(title_size, font_key=title_font_key, bold=True)
        subtitle_font, subtitle_bitmap_fallback = self._load_font(subtitle_size, font_key=subtitle_font_key, bold=True)

        title_width, _ = self._measure_text(
            draw,
            title_text,
            title_font,
            target_size=title_size,
            bitmap_fallback=title_bitmap_fallback
        )
        subtitle_width, _ = self._measure_text(
            draw,
            subtitle_text,
            subtitle_font,
            target_size=subtitle_size,
            bitmap_fallback=subtitle_bitmap_fallback
        )

        title_position = ((width - title_width) / 2, 318)
        subtitle_position = ((width - subtitle_width) / 2, 392)
        shadow = (0, 0, 0, 130)
        self._draw_scaled_text(
            canvas,
            position=(title_position[0] + 2, title_position[1] + 2),
            text=title_text,
            font=title_font,
            fill=shadow,
            target_size=title_size,
            bitmap_fallback=title_bitmap_fallback
        )
        self._draw_scaled_text(
            canvas,
            position=title_position,
            text=title_text,
            font=title_font,
            fill=text_color,
            target_size=title_size,
            bitmap_fallback=title_bitmap_fallback
        )
        self._draw_scaled_text(
            canvas,
            position=(subtitle_position[0] + 2, subtitle_position[1] + 2),
            text=subtitle_text,
            font=subtitle_font,
            fill=shadow,
            target_size=subtitle_size,
            bitmap_fallback=subtitle_bitmap_fallback
        )
        self._draw_scaled_text(
            canvas,
            position=subtitle_position,
            text=subtitle_text,
            font=subtitle_font,
            fill=text_color,
            target_size=subtitle_size,
            bitmap_fallback=subtitle_bitmap_fallback
        )

        output = BytesIO()
        canvas.convert("RGB").save(output, format="PNG", quality=95)
        output.seek(0)
        return discord.File(output, filename="welcome-card.png")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Handle new member join - Send welcome message and verification"""
        if not self.module_config.get('enabled', True):
            return

        guild_config = await self.db.get_guild(member.guild.id)
        if not guild_config:
            return
        await self._heal_welcome_card_config(member.guild, guild_config)

        verified_role_id = guild_config.get('verified_role')
        verification_type = self._get_verification_mode(guild_config)
        verification_method = guild_config.get('verification_method', 'dm')
        welcome_message_template = guild_config.get('welcome_message',
            f"Welcome to **{member.guild.name}**! 👋\n\n"
            "Please verify yourself to gain access to the server."
        )
        
        # Replace placeholders with actual values
        welcome_message = welcome_message_template.replace('{user}', member.mention)
        welcome_message = welcome_message.replace('{username}', member.display_name)
        welcome_message = welcome_message.replace('{server}', member.guild.name)
        # Replace channel names with mentions (e.g., "verify-channel" -> #verify-channel)
        import re
        for channel in member.guild.text_channels:
            # Replace channel name patterns with actual mentions
            welcome_message = welcome_message.replace(channel.name, channel.mention)
            welcome_message = welcome_message.replace(f"#{channel.name}", channel.mention)

        member_position = await self._reserve_member_join_position(member, guild_config)
        welcome_context = {
            "user": member.mention,
            "username": member.name,
            "display_name": member.display_name,
            "server": member.guild.name,
            "member_count": member_position
        }

        # Send welcome message in welcome channel (PUBLIC - everyone can see)
        welcome_channel_id = guild_config.get('welcome_channel')
        if welcome_channel_id:
            welcome_channel = member.guild.get_channel(welcome_channel_id)
            if welcome_channel:
                try:
                    if guild_config.get("welcome_card_enabled", False):
                        welcome_content = self._render_text_template(
                            guild_config.get("welcome_card_message"),
                            welcome_context,
                            DEFAULT_WELCOME_CARD_MESSAGE
                        )
                        welcome_file = await self._build_welcome_card(
                            member,
                            guild_config,
                            member_position=member_position
                        )
                        if welcome_file:
                            await welcome_channel.send(content=welcome_content, file=welcome_file)
                        else:
                            raise RuntimeError("Failed to render welcome card")
                    else:
                        welcome_embed = EmbedFactory.create(
                            title=f"👋 Welcome to {member.guild.name}!",
                            description=f"{member.mention}\n\n{welcome_message}",
                            color=EmbedColor.SUCCESS
                        )
                        welcome_embed.set_thumbnail(url=member.display_avatar.url)
                        await welcome_channel.send(embed=welcome_embed)
                    logger.info(f"Sent welcome message for {member} in {welcome_channel}")
                except Exception as error:
                    logger.error(f"Error sending welcome message for {member}: {error}", exc_info=True)

        # Send verification only if verified_role is configured
        if not verified_role_id:
            return

        if guild_config.get("verification_enabled", True) is False:
            logger.info("Verification is disabled for %s; skipping join verification for %s", member.guild, member)
            return

        if guild_config.get("rules_panel_enabled", False):
            logger.info("Rules panel verification is enabled for %s; awaiting member acceptance in rules channel", member.guild)
            return

        # Send verification to verify channel (if configured) - ONLY VISIBLE TO USER
        verify_channel_id = guild_config.get('verify_channel')
        if verification_method == 'channel' and verify_channel_id:
            verify_channel = member.guild.get_channel(verify_channel_id)
            if verify_channel:
                try:
                    if verification_type == 'button':
                        embed = EmbedFactory.create(
                            title=f"🔐 Verification",
                            description=f"{member.mention}, click the button below to verify.",
                            color=EmbedColor.PRIMARY
                        )
                        view = VerificationButton(self)
                        msg = await verify_channel.send(embed=embed, view=view, delete_after=300)
                        logger.info(f"Sent verification to channel for {member}")
                    elif verification_type == 'captcha':
                        code = self._generate_captcha_code()
                        
                        # Create a view with button that shows code only to the user
                        class ChannelCaptchaView(discord.ui.View):
                            def __init__(self, user_id, verification_code, cog):
                                super().__init__(timeout=None)
                                self.user_id = user_id
                                self.code = verification_code
                                self.cog = cog
                            
                            @discord.ui.button(label="Show My Code", style=discord.ButtonStyle.primary, emoji="🔐")
                            async def show_code(self, interaction: discord.Interaction, button: discord.ui.Button):
                                if interaction.user.id != self.user_id:
                                    await interaction.response.send_message("This is not for you!", ephemeral=True)
                                    return
                                await interaction.response.send_message(
                                    f"Your verification code: `{self.code}`\n\nClick the button below to enter it.",
                                    ephemeral=True,
                                    view=ChannelCaptchaEntryView(self.user_id, self.code, self.cog)
                                )
                        
                        class ChannelCaptchaEntryView(discord.ui.View):
                            def __init__(self, user_id, verification_code, cog):
                                super().__init__(timeout=None)
                                self.user_id = user_id
                                self.code = verification_code
                                self.cog = cog
                            
                            @discord.ui.button(label="Enter Code", style=discord.ButtonStyle.success, emoji="✅")
                            async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button):
                                if interaction.user.id != self.user_id:
                                    await interaction.response.send_message("This is not for you!", ephemeral=True)
                                    return
                                modal = CaptchaModal(self.code, self.cog)
                                await interaction.response.send_modal(modal)
                        
                        embed = EmbedFactory.create(
                            title=f"🔐 Verification",
                            description=f"{member.mention}, click the button below to see your verification code (only you will see it).",
                            color=EmbedColor.PRIMARY
                        )
                        
                        view = ChannelCaptchaView(member.id, code, self)
                        await verify_channel.send(embed=embed, view=view, delete_after=300)
                        logger.info(f"Sent captcha verification to channel for {member}")
                except Exception as e:
                    logger.error(f"Error sending verification to channel: {e}", exc_info=True)
            return

        # Send DM verification (PRIVATE) - fallback or if method is 'dm'
        try:
            if verification_type == 'button':
                embed = EmbedFactory.create(
                    title=f"🔐 Welcome to {member.guild.name}",
                    description=welcome_message,
                    color=EmbedColor.PRIMARY
                )
                embed.set_thumbnail(url=member.guild.icon.url if member.guild.icon else None)
                view = VerificationButton(self)
                await member.send(embed=embed, view=view)

            elif verification_type == 'captcha':
                code = self._generate_captcha_code()
                embed = EmbedFactory.create(
                    title=f"🔐 Welcome to {member.guild.name}",
                    description=f"{welcome_message}\n\n**Your verification code:** `{code}`\n\nClick the button below and enter this code.",
                    color=EmbedColor.PRIMARY
                )
                embed.set_thumbnail(url=member.guild.icon.url if member.guild.icon else None)
                view = CaptchaEntryView(
                    self,
                    user_id=member.id,
                    guild_id=member.guild.id,
                    correct_code=code,
                    source="verification"
                )
                await member.send(embed=embed, view=view)

            logger.info(f"Sent DM verification to {member} in {member.guild}")

        except discord.Forbidden:
            logger.warning(f"Could not DM {member} in {member.guild} - DMs disabled")
            log_channel_id = guild_config.get('log_channel')
            if log_channel_id:
                log_channel = member.guild.get_channel(log_channel_id)
                if log_channel:
                    await log_channel.send(
                        embed=EmbedFactory.error(
                            "Verification DM Failed",
                            f"Could not send verification DM to {member.mention} (DMs disabled)"
                        )
                    )

    async def verify_user(
        self,
        interaction: discord.Interaction,
        source: str = "verification",
        guild_id: Optional[int] = None
    ):
        """Verify a user and assign role (SILENT - no public announcements)"""
        await self._defer_interaction_if_needed(interaction)
        guild, member, guild_config = await self._resolve_verification_context(interaction, guild_id)
        if guild is None or member is None or guild_config is None:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Error", "Server not configured"),
                ephemeral=True
            )
            return

        if guild_config.get("verification_enabled", True) is False:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Disabled", "Verification is currently disabled for this server."),
                ephemeral=True
            )
            return

        verified_role_id = guild_config.get('verified_role')
        if not verified_role_id:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Error", "Verified role not configured"),
                ephemeral=True
            )
            return

        verified_role = guild.get_role(verified_role_id)
        if not verified_role:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Error", "Verified role not found"),
                ephemeral=True
            )
            return

        if verified_role in member.roles:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.info("Already Verified", "You are already verified!"),
                ephemeral=True
            )
            return

        if source in {"rules_accept", "rules_accept_captcha"} and self._platform_link_enabled(guild_config):
            user_data = await self._ensure_user_record(member.id, guild.id)
            saved_profile = await self._get_saved_platform_identity(user_data)
            if saved_profile:
                success, roles_added, roles_removed, nickname_note = await self._apply_platform_verification(
                    member,
                    guild_config,
                    profile_data=saved_profile
                )
                if not success:
                    await self._send_interaction_embed(
                        interaction,
                        embed=EmbedFactory.error(
                            "Verification Error",
                            nickname_note or "I couldn't finish your platform-based verification."
                        ),
                        ephemeral=True
                    )
                    return

                message = (
                    f"You've accepted the rules for **{guild.name}**.\n\n"
                    "I restored your saved platform identity and verified you automatically."
                )
                signpost_message = self._get_verification_signpost_message(guild_config, member)
                if signpost_message:
                    message += f"\n\n**Next Step:**\n{signpost_message}"
                if nickname_note:
                    message += f"\n\n**Nickname Note:** {nickname_note}"

                await self._send_interaction_embed(
                    interaction,
                    embed=EmbedFactory.success("✅ Rules Accepted!", message),
                    ephemeral=True
                )
                logger.info(
                    "Verified user %s in %s via saved platform identity (%s)",
                    member,
                    guild,
                    saved_profile["platform"]
                )
                return

            await self._start_platform_link_prompt(
                interaction,
                guild=guild,
                member=member,
                guild_config=guild_config
            )
            return

        try:
            # Silently add verified role
            await member.add_roles(verified_role)

            # Send private success message
            success_title = "✅ Verified Successfully!"
            success_message = (
                f"Welcome to **{guild.name}**!\n\n"
                "You now have access to all channels."
            )
            if source in {"rules_accept", "rules_accept_captcha"}:
                success_title = "✅ Rules Accepted!"
                success_message = (
                    f"You've accepted the rules for **{guild.name}**.\n\n"
                    "Your verification is complete and you now have access to all channels."
                )
            signpost_message = self._get_verification_signpost_message(guild_config, member)
            if signpost_message:
                success_message += f"\n\n**Next Step:**\n{signpost_message}"

            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.success(
                    success_title,
                    success_message
                ),
                ephemeral=True
            )

            # Log silently (no public announcement)
            logger.info("Verified user %s in %s (silent) via %s", member, guild, source)

        except discord.Forbidden:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Error", "I don't have permission to assign roles"),
                ephemeral=True
            )
        except Exception as error:
            logger.error("Error verifying user: %s", error, exc_info=True)
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Error", "An error occurred during verification"),
                ephemeral=True
            )

    async def complete_platform_link(
        self,
        interaction: discord.Interaction,
        *,
        platform: str,
        raw_value: str,
        guild_id: Optional[int] = None
    ) -> None:
        """Validate a selected streaming profile, then complete platform-based verification."""
        await self._defer_interaction_if_needed(interaction)
        guild, member, guild_config = await self._resolve_verification_context(interaction, guild_id)
        if guild is None or member is None or guild_config is None:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Error", "I couldn't load this server's verification context."),
                ephemeral=True
            )
            return

        if platform not in VERIFICATION_PLATFORM_CHOICES:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Unsupported Platform", "Please choose Twitch, YouTube, or Kick."),
                ephemeral=True
            )
            return

        profile_data, error = await self._resolve_platform_profile(platform, raw_value)
        if error or not profile_data:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error(
                    f"{VERIFICATION_PLATFORM_LABELS[platform]} Lookup Failed",
                    error or "I couldn't verify that profile."
                ),
                ephemeral=True
            )
            return

        success, roles_added, roles_removed, nickname_note = await self._apply_platform_verification(
            member,
            guild_config,
            profile_data=profile_data
        )
        if not success:
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Verification Error", nickname_note or "I couldn't apply your platform verification settings."),
                ephemeral=True
            )
            return

        await self._save_platform_identity(member.id, guild.id, profile_data)

        added_names = ", ".join(role.name for role in roles_added) if roles_added else "No new roles were needed"
        removed_names = ", ".join(role.name for role in roles_removed) if roles_removed else "None"
        message = (
            f"**Platform:** {VERIFICATION_PLATFORM_LABELS[platform]}\n"
            f"**Profile:** [{profile_data['display_name']}]({profile_data['profile_url']})\n"
            f"**Roles Added:** {added_names}\n"
            f"**Roles Removed:** {removed_names}"
        )
        signpost_message = self._get_verification_signpost_message(guild_config, member)
        if signpost_message:
            message += f"\n\n**Next Step:**\n{signpost_message}"
        if nickname_note:
            message += f"\n**Nickname Note:** {nickname_note}"
        else:
            message += f"\n**Nickname:** Updated to `{profile_data['display_name'][:32]}`"

        await self._send_interaction_embed(
            interaction,
            embed=EmbedFactory.success("✅ Verification Complete!", message),
            ephemeral=True
        )
        logger.info(
            "Completed platform verification for %s in %s via %s (%s)",
            member,
            guild,
            platform,
            profile_data["username"]
        )

    @app_commands.command(name="verification-role", description="Set the role granted after verification or rules acceptance (Admin)")
    @app_commands.describe(role="Role to assign when a member completes verification")
    @is_admin()
    async def verification_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role
    ):
        """Set the verified role used by the rules panel and verification system."""
        if role.is_default():
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Role", "Please choose a normal server role, not `@everyone`."),
                ephemeral=True
            )
            return

        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        await self.db.update_guild(interaction.guild.id, {
            "verified_role": role.id,
            "verification_enabled": True,
            "verification_type": guild_config.get("verification_type", DEFAULT_VERIFICATION_MODE),
            "rules_verification_mode": self._get_verification_mode(guild_config)
        })

        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Verification Role Updated",
                f"Members who complete verification will now receive {role.mention}."
            ),
            ephemeral=True
        )

    @app_commands.command(name="verification-status", description="View the current verification setup (Admin)")
    @is_admin()
    async def verification_status(self, interaction: discord.Interaction):
        """Show the currently saved verification configuration."""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config or not guild_config.get("verified_role"):
            await interaction.response.send_message(
                embed=EmbedFactory.info(
                    "Verification Status",
                    "Verification is not currently configured for this server."
                ),
                ephemeral=True
            )
            return

        verified_role = interaction.guild.get_role(guild_config.get("verified_role"))
        welcome_channel = interaction.guild.get_channel(guild_config.get("welcome_channel")) if guild_config.get("welcome_channel") else None
        verify_channel = interaction.guild.get_channel(guild_config.get("verify_channel")) if guild_config.get("verify_channel") else None
        verification_enabled = guild_config.get("verification_enabled", True)

        embed = EmbedFactory.create(
            title="🔐 Verification Status",
            color=EmbedColor.INFO,
            fields=[
                {"name": "Enabled", "value": "Yes" if verification_enabled else "No", "inline": True},
                {"name": "Method", "value": guild_config.get("verification_method", "dm"), "inline": True},
                {"name": "Type", "value": guild_config.get("verification_type", DEFAULT_VERIFICATION_MODE), "inline": True},
                {"name": "Rules Accept Flow", "value": self._get_verification_mode(guild_config), "inline": True},
                {"name": "Rules Panel", "value": "Enabled" if guild_config.get("rules_panel_enabled", False) else "Disabled", "inline": True},
                {"name": "Platform Link Stage", "value": "Enabled" if self._platform_link_enabled(guild_config) else "Disabled", "inline": True},
                {"name": "Platform Save Retention", "value": "7 days", "inline": True},
                {"name": "Verified Role", "value": verified_role.mention if verified_role else "Missing role", "inline": False},
                {
                    "name": "Platform Roles",
                    "value": (
                        f"Twitch: {interaction.guild.get_role(self._get_platform_role_id(guild_config, 'twitch')).mention if self._get_platform_role_id(guild_config, 'twitch') and interaction.guild.get_role(self._get_platform_role_id(guild_config, 'twitch')) else 'Not set'}\n"
                        f"YouTube: {interaction.guild.get_role(self._get_platform_role_id(guild_config, 'youtube')).mention if self._get_platform_role_id(guild_config, 'youtube') and interaction.guild.get_role(self._get_platform_role_id(guild_config, 'youtube')) else 'Not set'}\n"
                        f"Kick: {interaction.guild.get_role(self._get_platform_role_id(guild_config, 'kick')).mention if self._get_platform_role_id(guild_config, 'kick') and interaction.guild.get_role(self._get_platform_role_id(guild_config, 'kick')) else 'Not set'}"
                    ),
                    "inline": False
                },
                {"name": "Welcome Channel", "value": welcome_channel.mention if welcome_channel else "Not set", "inline": False},
                {"name": "Verify Channel", "value": verify_channel.mention if verify_channel else "DM only / not set", "inline": False},
                {
                    "name": "Rules Channel",
                    "value": (
                        interaction.guild.get_channel(guild_config.get("rules_channel")).mention
                        if guild_config.get("rules_channel") and interaction.guild.get_channel(guild_config.get("rules_channel"))
                        else "Not set"
                    ),
                    "inline": False
                },
                {
                    "name": "Welcome Message",
                    "value": (guild_config.get("welcome_message") or "Not set")[:250],
                    "inline": False
                },
                {
                    "name": "Verification Signpost",
                    "value": (guild_config.get("verification_signpost_message") or "Not set")[:250],
                    "inline": False
                },
                {
                    "name": "Note",
                    "value": (
                        "There is only one saved verification configuration per server. "
                        "Use `/verification-role` to set the role granted after verification, "
                        "`/embed_rules verification:true` to post the rules gate, and `/verification-mode` to choose whether Accept verifies instantly or sends a DM captcha first. "
                        "If the rules panel is enabled, the Rules Accept Flow decides whether clicking Accept grants access instantly or sends a DM captcha first. "
                        "Any old verification messages already sent in channels are just normal messages and can be deleted manually."
                    ),
                    "inline": False
                }
            ]
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="verification-platform-role", description="Set the role granted for a viewing platform (Admin)")
    @app_commands.describe(
        platform="Streaming platform to configure",
        role="Role members should receive for that platform"
    )
    @app_commands.choices(
        platform=[
            app_commands.Choice(name="Twitch", value="twitch"),
            app_commands.Choice(name="YouTube", value="youtube"),
            app_commands.Choice(name="Kick", value="kick")
        ]
    )
    @is_admin()
    async def verification_platform_role(
        self,
        interaction: discord.Interaction,
        platform: str,
        role: discord.Role
    ):
        """Set the role assigned when a member links a specific viewing platform."""
        if role.is_default():
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Role", "Please choose a normal server role, not `@everyone`."),
                ephemeral=True
            )
            return

        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        role_map = guild_config.get("platform_link_roles", {}) or {}
        role_map[platform] = role.id
        await self.db.update_guild(interaction.guild.id, {"platform_link_roles": role_map})

        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Platform Role Updated",
                f"Members who link **{VERIFICATION_PLATFORM_LABELS[platform]}** will now receive {role.mention}."
            ),
            ephemeral=True
        )

    @app_commands.command(name="verification-platform-toggle", description="Enable or disable required platform-link verification (Admin)")
    @app_commands.describe(enabled="Whether members must link Twitch, YouTube, or Kick after accepting the rules")
    @is_admin()
    async def verification_platform_toggle(self, interaction: discord.Interaction, enabled: bool):
        """Toggle the optional second-stage platform link flow."""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        if enabled:
            missing_roles = [
                VERIFICATION_PLATFORM_LABELS[platform]
                for platform in VERIFICATION_PLATFORM_CHOICES
                if not self._get_platform_role_id(guild_config, platform)
            ]
            missing_credentials = []
            if not (os.getenv("TWITCH_CLIENT_ID") or self.config.get("api_keys", {}).get("twitch_client_id")) or not (os.getenv("TWITCH_CLIENT_SECRET") or self.config.get("api_keys", {}).get("twitch_client_secret")):
                missing_credentials.append("Twitch credentials")
            if not self._get_youtube_api_key():
                missing_credentials.append("YouTube API key")
            if not all(self._get_kick_credentials()):
                missing_credentials.append("Kick credentials")

            if missing_roles or missing_credentials:
                problems = []
                if missing_roles:
                    problems.append("Missing platform roles: " + ", ".join(missing_roles))
                if missing_credentials:
                    problems.append("Missing credentials: " + ", ".join(missing_credentials))
                await interaction.response.send_message(
                    embed=EmbedFactory.error(
                        "Platform Verification Not Ready",
                        "\n".join(problems)
                    ),
                    ephemeral=True
                )
                return

        await self.db.update_guild(interaction.guild.id, {"platform_link_enabled": enabled})
        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Platform Verification Updated",
                (
                    "Members must now link their Twitch, YouTube, or Kick profile after accepting the rules before they receive the verified role."
                    if enabled
                    else "The extra platform-link stage is now disabled. Rules acceptance will behave as before."
                )
            ),
            ephemeral=True
        )

    @app_commands.command(name="verification-signpost", description="Set or clear the optional post-verification guidance message (Admin)")
    @app_commands.describe(
        message="Optional message shown after verification completes",
        channel="Optional channel to direct members toward",
        message_link="Optional message link to include"
    )
    @is_admin()
    async def verification_signpost(
        self,
        interaction: discord.Interaction,
        message: Optional[str] = None,
        channel: Optional[discord.TextChannel] = None,
        message_link: Optional[str] = None
    ):
        """Configure an optional signpost shown after members finish verification."""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        parts = []
        trimmed_message = (message or "").strip()
        trimmed_link = (message_link or "").strip()

        if trimmed_message:
            parts.append(trimmed_message)
        if channel is not None:
            parts.append(f"Head to {channel.mention} for the next step.")
        if trimmed_link:
            parts.append(f"Important message: {trimmed_link}")

        signpost_value = "\n".join(parts).strip() or None
        await self.db.update_guild(interaction.guild.id, {"verification_signpost_message": signpost_value})

        if signpost_value:
            await interaction.response.send_message(
                embed=EmbedFactory.success(
                    "Verification Signpost Updated",
                    (
                        "Members will now see this after verification:\n\n"
                        f"{signpost_value}\n\n"
                        "Supported placeholders: `{user}` `{username}` `{display_name}` `{server}`"
                    )
                ),
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.success(
                    "Verification Signpost Cleared",
                    "Members will no longer receive an extra signpost message after verification."
                ),
                ephemeral=True
            )

    @app_commands.command(name="verification-mode", description="Choose what happens after members click Accept (Admin)")
    @app_commands.describe(mode="Use 'button' for instant access or 'captcha' to send a DM captcha after Accept")
    @is_admin()
    async def verification_mode(self, interaction: discord.Interaction, mode: str):
        """Toggle the rules-panel follow-up flow between instant verify and DM captcha."""
        mode = mode.lower().strip()
        if mode not in {"button", "captcha"}:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Mode", "Mode must be `button` or `captcha`."),
                ephemeral=True
            )
            return

        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        await self.db.update_guild(interaction.guild.id, {
            "verification_type": mode,
            "rules_verification_mode": mode,
            "verification_enabled": True
        })

        description = (
            "Members who click the rules Accept button will now receive a captcha in DMs before they get access."
            if mode == "captcha"
            else "Members who click the rules Accept button will now be verified immediately."
        )
        await interaction.response.send_message(
            embed=EmbedFactory.success("Verification Mode Updated", description),
            ephemeral=True
        )

    @app_commands.command(name="verification-platform-link", description="Link or update your main viewing platform")
    async def verification_platform_link(self, interaction: discord.Interaction):
        """Let a member redo or complete their platform-link verification."""
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.error("Server Only", "This command can only be used inside the server."),
                ephemeral=True
            )
            return

        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config or not self._platform_link_enabled(guild_config):
            await self._send_interaction_embed(
                interaction,
                embed=EmbedFactory.info(
                    "Platform Link Disabled",
                    "This server does not currently require platform-link verification."
                ),
                ephemeral=True
            )
            return

        await self._start_platform_link_prompt(
            interaction,
            guild=interaction.guild,
            member=interaction.user,
            guild_config=guild_config,
            redo=True
        )

    @app_commands.command(name="verification-disable", description="Disable server verification (Admin)")
    @is_admin()
    async def verification_disable(self, interaction: discord.Interaction):
        """Disable verification for future joins."""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config or not guild_config.get("verified_role"):
            await interaction.response.send_message(
                embed=EmbedFactory.info("Verification Disabled", "Verification is not currently configured."),
                ephemeral=True
            )
            return

        await self.db.update_guild(interaction.guild.id, {
            "verification_enabled": False,
            "verified_role": None,
            "verify_channel": None,
            "rules_panel_enabled": False,
            "rules_channel": None
        })

        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Verification Disabled",
                "Future joins will no longer receive verification prompts. Existing verification messages already sent in channels will need to be deleted manually if you want them gone."
            ),
            ephemeral=True
        )

    @app_commands.command(name="welcome-card-config", description="Configure the join welcome card (Admin)")
    @app_commands.describe(
        channel="Optional welcome channel override",
        enabled="Enable or disable welcome cards",
        message="Text sent above the welcome image",
        title="Main title on the welcome image",
        subtitle="Subtitle on the welcome image",
        title_font="Font family for the main title",
        subtitle_font="Font family for the subtitle",
        title_size="Title font size (24-140)",
        subtitle_size="Subtitle font size (24-140)",
        accent_color="Accent hex color such as #F5B8C7",
        text_color="Text hex color such as #F1C1CC",
        background_image_url="Optional background image URL for the card",
        background_image="Optional image upload to store directly for the card"
    )
    @app_commands.choices(title_font=WELCOME_CARD_FONT_CHOICES, subtitle_font=WELCOME_CARD_FONT_CHOICES)
    @is_admin()
    async def welcome_card_config(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        enabled: Optional[bool] = None,
        message: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        title_font: Optional[str] = None,
        subtitle_font: Optional[str] = None,
        title_size: Optional[int] = None,
        subtitle_size: Optional[int] = None,
        accent_color: Optional[str] = None,
        text_color: Optional[str] = None,
        background_image_url: Optional[str] = None,
        background_image: Optional[discord.Attachment] = None
    ):
        """Configure welcome card appearance and behavior."""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        if background_image_url is not None and background_image is not None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Choose One Background Source",
                    "Use either `background_image_url` or `background_image` in one command, not both."
                ),
                ephemeral=True
            )
            return

        if accent_color is not None and not self._is_valid_hex_color(accent_color):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Colour", "Accent color must be a 6-digit hex value such as `#F5B8C7`."),
                ephemeral=True
            )
            return

        if text_color is not None and not self._is_valid_hex_color(text_color):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Colour", "Text color must be a 6-digit hex value such as `#F1C1CC`."),
                ephemeral=True
            )
            return

        if background_image is not None:
            if background_image.size and background_image.size > WELCOME_CARD_BACKGROUND_MAX_BYTES:
                await interaction.response.send_message(
                    embed=EmbedFactory.error(
                        "Image Too Large",
                        "Background image files must be 8 MB or smaller."
                    ),
                    ephemeral=True
                )
                return

            if background_image.content_type and not background_image.content_type.startswith("image/"):
                await interaction.response.send_message(
                    embed=EmbedFactory.error(
                        "Invalid Background",
                        "Please upload a valid image file for the welcome card background."
                    ),
                    ephemeral=True
                )
                return

            try:
                background_bytes = await background_image.read()
            except discord.HTTPException as error:
                logger.warning("Failed to read uploaded welcome card background: %s", error)
                await interaction.response.send_message(
                    embed=EmbedFactory.error(
                        "Upload Failed",
                        "I couldn't download that uploaded image from Discord. Please try again."
                    ),
                    ephemeral=True
                )
                return

            stored, store_error = await self._store_welcome_background_image(
                interaction.guild.id,
                guild_config,
                background_bytes,
                source_url=background_image.url
            )
            if not stored:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Invalid Background", store_error or "That image could not be saved."),
                    ephemeral=True
                )
                return

        if background_image_url is not None:
            trimmed_url = background_image_url.strip()
            if not trimmed_url:
                update_data = {
                    "welcome_card_background_url": None,
                    "welcome_card_background_image_data": None
                }
                await self.db.update_guild(interaction.guild.id, update_data)
                guild_config.update(update_data)
            else:
                background = await self._fetch_url_image(trimmed_url)
                if background is None:
                    await interaction.response.send_message(
                        embed=EmbedFactory.error(
                            "Background Fetch Failed",
                            "I couldn't download a valid image from that URL. Please use a direct image link or upload the file instead."
                        ),
                        ephemeral=True
                    )
                    return

                encoded_image = self._encode_welcome_background_image(background)
                update_data = {
                    "welcome_card_background_url": trimmed_url,
                    "welcome_card_background_image_data": encoded_image
                }
                await self.db.update_guild(interaction.guild.id, update_data)
                guild_config.update(update_data)

        update_data = {}
        if channel is not None:
            update_data["welcome_channel"] = channel.id
        if enabled is not None:
            update_data["welcome_card_enabled"] = enabled
        if message is not None:
            update_data["welcome_card_message"] = message
        if title is not None:
            update_data["welcome_card_title"] = title
        if subtitle is not None:
            update_data["welcome_card_subtitle"] = subtitle
        if title_font is not None:
            update_data["welcome_card_title_font"] = self._get_font_key(title_font)
        if subtitle_font is not None:
            update_data["welcome_card_subtitle_font"] = self._get_font_key(subtitle_font)
        if title_size is not None:
            update_data["welcome_card_title_size"] = self._clamp_font_size(
                title_size,
                DEFAULT_WELCOME_CARD_TITLE_SIZE
            )
        if subtitle_size is not None:
            update_data["welcome_card_subtitle_size"] = self._clamp_font_size(
                subtitle_size,
                DEFAULT_WELCOME_CARD_SUBTITLE_SIZE
            )
        if accent_color is not None:
            update_data["welcome_card_accent_color"] = accent_color
        if text_color is not None:
            update_data["welcome_card_text_color"] = text_color

        if update_data:
            await self.db.update_guild(interaction.guild.id, update_data)
            guild_config.update(update_data)

        embed = EmbedFactory.create(
            title="🖼️ Welcome Card Settings",
            color=EmbedColor.INFO,
            fields=[
                {"name": "Enabled", "value": "Yes" if guild_config.get("welcome_card_enabled", False) else "No", "inline": True},
                {
                    "name": "Welcome Channel",
                    "value": (interaction.guild.get_channel(guild_config.get("welcome_channel")).mention if guild_config.get("welcome_channel") and interaction.guild.get_channel(guild_config.get("welcome_channel")) else "Not set"),
                    "inline": True
                },
                {"name": "Accent Color", "value": guild_config.get("welcome_card_accent_color", DEFAULT_WELCOME_CARD_ACCENT), "inline": True},
                {"name": "Text Color", "value": guild_config.get("welcome_card_text_color", DEFAULT_WELCOME_CARD_TEXT), "inline": True},
                {"name": "Title Font", "value": self._get_font_label(guild_config.get("welcome_card_title_font")), "inline": True},
                {"name": "Subtitle Font", "value": self._get_font_label(guild_config.get("welcome_card_subtitle_font")), "inline": True},
                {"name": "Title Size", "value": str(guild_config.get("welcome_card_title_size", DEFAULT_WELCOME_CARD_TITLE_SIZE)), "inline": True},
                {"name": "Subtitle Size", "value": str(guild_config.get("welcome_card_subtitle_size", DEFAULT_WELCOME_CARD_SUBTITLE_SIZE)), "inline": True},
                {"name": "Message", "value": (guild_config.get("welcome_card_message") or DEFAULT_WELCOME_CARD_MESSAGE)[:250], "inline": False},
                {"name": "Title", "value": guild_config.get("welcome_card_title", DEFAULT_WELCOME_CARD_TITLE), "inline": False},
                {"name": "Subtitle", "value": guild_config.get("welcome_card_subtitle", DEFAULT_WELCOME_CARD_SUBTITLE), "inline": False},
                {
                    "name": "Background Image",
                    "value": guild_config.get("welcome_card_background_url") or ("Stored custom image" if guild_config.get("welcome_card_background_image_data") else "Using generated default background"),
                    "inline": False
                },
                {
                    "name": "Background Storage",
                    "value": "Durable saved copy" if guild_config.get("welcome_card_background_image_data") else "No saved copy",
                    "inline": False
                },
                {
                    "name": "Placeholders",
                    "value": "`{user}` `{username}` `{display_name}` `{server}` `{member_count}`",
                    "inline": False
                },
                {
                    "name": "Font Options",
                    "value": ", ".join(font["label"] for font in WELCOME_CARD_FONT_PRESETS.values()),
                    "inline": False
                }
            ]
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="welcome-card-preview", description="Preview the current welcome card (Admin)")
    @is_admin()
    async def welcome_card_preview(self, interaction: discord.Interaction):
        """Preview the configured welcome card using the invoking admin."""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)
        await self._heal_welcome_card_config(interaction.guild, guild_config)

        preview_file = await self._build_welcome_card(interaction.user, guild_config)
        if not preview_file:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Preview Failed", "I couldn't generate the welcome card preview."),
                ephemeral=True
            )
            return

        context = {
            "user": interaction.user.mention,
            "username": interaction.user.name,
            "display_name": interaction.user.display_name,
            "server": interaction.guild.name,
            "member_count": await self._get_member_position(interaction.user)
        }
        welcome_content = self._render_text_template(
            guild_config.get("welcome_card_message"),
            context,
            DEFAULT_WELCOME_CARD_MESSAGE
        )
        await interaction.response.send_message(content=welcome_content, file=preview_file, ephemeral=True)

    @app_commands.command(name="set-welcome-message", description="Set custom welcome DM message (Admin)")
    @app_commands.describe(message="Custom welcome message for new members")
    @is_admin()
    async def set_welcome_message(self, interaction: discord.Interaction, message: str):
        """Set custom welcome message for verification DMs (ADMIN ONLY)"""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

        await self.db.update_guild(interaction.guild.id, {
            'welcome_message': message
        })

        embed = EmbedFactory.success(
            "✅ Welcome Message Updated",
            f"**New Welcome Message:**\n{message}\n\n"
            "This will be sent in DMs to new members when the legacy DM verification flow is used."
        )
        await interaction.response.send_message(embed=embed)
        logger.info(f"Welcome message updated in {interaction.guild}")

    @app_commands.command(name="send-verification", description="Send verification button in current channel (Admin)")
    @is_admin()
    async def send_verification(self, interaction: discord.Interaction):
        """Manually send verification button to current channel (ADMIN ONLY)"""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config or not guild_config.get('verified_role'):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Configured", "Please set a verified role first with `/verification-role`."),
                ephemeral=True
            )
            return

        embed = EmbedFactory.verification_prompt()
        view = VerificationButton(self)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            embed=EmbedFactory.success("Sent", "Verification button sent to this channel!"),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Verification(bot, bot.db, bot.config))
