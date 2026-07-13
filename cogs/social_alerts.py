"""
Social Alerts Cog for Logiq
Monitor Twitch, YouTube, Twitter/X for new content
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode, urlparse

import aiohttp
import discord
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from discord import app_commands
from discord.ext import commands, tasks

from database.db_manager import DatabaseManager
from utils.embeds import EmbedColor, EmbedFactory
from utils.permissions import is_admin

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = ["twitch", "youtube", "kick", "twitter"]
DEFAULT_ALERT_TEMPLATE = (
    "Hey @everyone, **{display_name}** is now live on {url}! Go check it out!"
)
TWITCH_EMBED_COLOR = 0x9146FF
YOUTUBE_EMBED_COLOR = 0xFF0000
KICK_EMBED_COLOR = 0x53FC18
TWITTER_EMBED_COLOR = 0x1DA1F2
TWITCH_EVENTSUB_PATH = "/webhooks/twitch/eventsub"
KICK_EVENTS_PATH = "/webhooks/kick/events"
YOUTUBE_OAUTH_CALLBACK_PATH = "/oauth/youtube/callback"
TWITCH_EVENTSUB_TYPES = ("stream.online", "stream.offline")
KICK_EVENT_TYPES = ("livestream.status.updated", "livestream.metadata.updated")
YOUTUBE_OAUTH_SCOPES = ("https://www.googleapis.com/auth/youtube.readonly",)
TWITCH_EVENTSUB_RECREATE_STATUSES = {
    "webhook_callback_verification_failed",
    "notification_failures_exceeded",
    "authorization_revoked",
    "user_removed",
    "version_removed",
}
TWITCH_EVENTSUB_POLL_SUPPRESSION_SECONDS = 90
KICK_WEBHOOK_PUBLIC_KEY_FALLBACK = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAq/+l1WnlRrGSolDMA+A8
6rAhMbQGmQ2SapVcGM3zq8ANXjnhDWocMqfWcTd95btDydITa10kDvHzw9WQOqp2
MZI7ZyrfzJuz5nhTPCiJwTwnEtWft7nV14BYRDHvlfqPUaZ+1KR4OCaO/wWIk/rQ
L/TjY0M70gse8rlBkbo2a8rKhu69RQTRsoaf4DVhDPEeSeI5jVrRDGAMGL3cGuyY
6CLKGdjVEM78g3JfYOvDU/RvfqD7L89TZ3iN94jrmWdGz34JNlEI5hqK8dd7C5EF
BEbZ5jgB8s8ReQV8H+MkuffjdAj3ajDDX3DOJMIut1lBrUVD1AaSrGCKHooWoL2e
twIDAQAB
-----END PUBLIC KEY-----"""


class SocialAlertTemplateModal(discord.ui.Modal):
    """Modal for writing multi-line social alert templates."""

    def __init__(
        self,
        cog: "SocialAlerts",
        platform: str,
        username: str,
        channel: Optional[discord.TextChannel],
        mode: str,
        existing_alert: Optional[dict] = None
    ):
        title = "Create Social Alert" if mode == "create" else "Edit Social Alert"
        super().__init__(title=title)
        self.cog = cog
        self.platform = platform
        self.username = username
        self.channel = channel
        self.mode = mode
        self.existing_alert = existing_alert

        default_value = None
        if existing_alert:
            default_value = existing_alert.get("message_template") or DEFAULT_ALERT_TEMPLATE

        self.message_template = discord.ui.TextInput(
            label="Announcement Message",
            placeholder="Use {display_name}, {url}, {title}, {everyone}. Discord markdown works here.",
            style=discord.TextStyle.paragraph,
            required=False,
            default=default_value,
            max_length=2000
        )
        self.add_item(self.message_template)

    async def on_submit(self, interaction: discord.Interaction):
        """Persist the alert after the modal is submitted."""
        template_value = self.message_template.value.strip()
        message_template = template_value or None

        if self.mode == "create":
            await self.cog._create_alert_record(
                interaction,
                self.platform,
                self.username,
                self.channel,
                message_template
            )
        else:
            await self.cog._update_alert_record(
                interaction,
                self.platform,
                self.username,
                self.channel,
                message_template,
                self.existing_alert
            )


class SocialAlerts(commands.Cog):
    """Social media alerts cog"""

    alert_group = app_commands.Group(
        name="alert",
        description="Manage social media alerts"
    )

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get("modules", {}).get("social_alerts", {})
        self.session = None
        self._twitch_access_token: Optional[str] = None
        self._twitch_token_expires_at: Optional[datetime] = None
        self._twitch_user_cache: dict[str, dict] = {}
        self._recent_eventsub_messages: dict[str, datetime] = {}
        self._eventsub_live_tasks: dict[str, asyncio.Task] = {}
        self._kick_access_token: Optional[str] = None
        self._kick_token_expires_at: Optional[datetime] = None
        self._recent_kick_event_messages: dict[str, datetime] = {}
        self._kick_public_key = None
        self._kick_public_key_fetched_at: Optional[datetime] = None
        self.check_alerts_task.start()
        self.reconcile_eventsub_task.start()

    def cog_unload(self):
        """Cleanup on cog unload"""
        self.check_alerts_task.cancel()
        self.reconcile_eventsub_task.cancel()
        for task in self._eventsub_live_tasks.values():
            task.cancel()
        self._eventsub_live_tasks.clear()
        if self.session:
            asyncio.create_task(self.session.close())

    async def get_session(self):
        """Get or create aiohttp session"""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def _get_twitch_credentials(self) -> tuple[Optional[str], Optional[str]]:
        """Load Twitch credentials from environment or config."""
        client_id = os.getenv("TWITCH_CLIENT_ID") or self.config.get("api_keys", {}).get("twitch_client_id")
        client_secret = os.getenv("TWITCH_CLIENT_SECRET") or self.config.get("api_keys", {}).get("twitch_client_secret")
        return client_id, client_secret

    def _get_kick_credentials(self) -> tuple[Optional[str], Optional[str]]:
        """Load Kick credentials from environment or config."""
        client_id = os.getenv("KICK_CLIENT_ID") or self.config.get("api_keys", {}).get("kick_client_id")
        client_secret = os.getenv("KICK_CLIENT_SECRET") or self.config.get("api_keys", {}).get("kick_client_secret")
        return client_id, client_secret

    def _get_youtube_api_key(self) -> Optional[str]:
        """Load the YouTube Data API key."""
        return os.getenv("YOUTUBE_API_KEY") or self.config.get("api_keys", {}).get("youtube")

    def _get_youtube_oauth_credentials(self) -> tuple[Optional[str], Optional[str]]:
        """Load Google OAuth client credentials for the optional YouTube owner flow."""
        client_id = os.getenv("YOUTUBE_OAUTH_CLIENT_ID") or self.config.get("api_keys", {}).get("youtube_oauth_client_id")
        client_secret = os.getenv("YOUTUBE_OAUTH_CLIENT_SECRET") or self.config.get("api_keys", {}).get("youtube_oauth_client_secret")
        return client_id, client_secret

    def _get_public_base_url(self) -> Optional[str]:
        """Return the configured public HTTPS base URL for webhook/callback endpoints."""
        base_url = (
            os.getenv("PUBLIC_BASE_URL")
            or self.config.get("web", {}).get("public_url")
        )
        if not base_url:
            return None
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith("https://"):
            return None
        return base_url

    def _normalize_twitch_username(self, value: str) -> str:
        """Normalize a Twitch username or URL into a login."""
        candidate = value.strip()
        if "twitch.tv" in candidate.lower():
            parsed = urlparse(candidate)
            candidate = parsed.path.strip("/").split("/")[0] if parsed.path else ""
        return candidate.strip().lstrip("@").lower()

    def _parse_youtube_input(self, value: str) -> tuple[str, str]:
        """Normalize YouTube input into a lookup type and canonical value."""
        candidate = value.strip()
        lowered = candidate.lower()
        if "youtube.com" in lowered or "youtu.be" in lowered:
            parsed = urlparse(candidate)
            path = parsed.path.strip("/")
            if path.startswith("@"):
                return "handle", "@" + path.split("/")[0].lstrip("@")
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] == "channel":
                return "id", parts[1]
            if len(parts) >= 2 and parts[0] == "user":
                return "username", parts[1]
            if len(parts) >= 2 and parts[0] == "c":
                return "handle", "@" + parts[1]
        if candidate.startswith("@"):
            return "handle", "@" + candidate.lstrip("@")
        if candidate.startswith("UC") and len(candidate) >= 20:
            return "id", candidate
        return "handle", "@" + candidate.lstrip("@")

    def _normalize_kick_username(self, value: str) -> str:
        """Normalize a Kick username or URL into a channel slug."""
        candidate = value.strip().lower()
        if "kick.com/" in candidate:
            candidate = candidate.split("kick.com/", 1)[1]
        candidate = candidate.split("?", 1)[0].split("#", 1)[0].strip("/")
        return candidate.lstrip("@")

    def _normalize_alert_username(self, platform: str, username: str) -> str:
        """Normalize stored alert usernames per platform."""
        if platform == "twitch":
            return self._normalize_twitch_username(username)
        if platform == "youtube":
            lookup_type, lookup_value = self._parse_youtube_input(username)
            if lookup_type == "handle":
                return "@" + lookup_value.lstrip("@").lower()
            if lookup_type == "username":
                return lookup_value.lower()
            return lookup_value.strip()
        if platform == "kick":
            return self._normalize_kick_username(username)
        return username.lower().strip()

    def _get_twitch_eventsub_secret(self) -> Optional[str]:
        """Load the EventSub webhook secret."""
        return os.getenv("TWITCH_EVENTSUB_SECRET") or self.config.get("api_keys", {}).get("twitch_eventsub_secret")

    def _get_twitch_eventsub_callback_url(self) -> Optional[str]:
        """Build the public callback URL Twitch should call."""
        callback_url = os.getenv("TWITCH_EVENTSUB_CALLBACK_URL")
        if callback_url:
            callback_url = callback_url.strip().rstrip("/")
            return callback_url if callback_url.startswith("https://") else None
        base_url = self._get_public_base_url()
        return base_url + TWITCH_EVENTSUB_PATH if base_url else None

    def _get_kick_events_callback_url(self) -> Optional[str]:
        """Build the public callback URL Kick should call."""
        callback_url = os.getenv("KICK_EVENTS_CALLBACK_URL")
        if callback_url:
            callback_url = callback_url.strip().rstrip("/")
            return callback_url if callback_url.startswith("https://") else None
        base_url = self._get_public_base_url()
        return base_url + KICK_EVENTS_PATH if base_url else None

    def _get_youtube_oauth_redirect_uri(self) -> Optional[str]:
        """Build the YouTube OAuth callback URL."""
        redirect_uri = os.getenv("YOUTUBE_OAUTH_REDIRECT_URI")
        if redirect_uri:
            redirect_uri = redirect_uri.strip().rstrip("/")
            return redirect_uri if redirect_uri.startswith("https://") else None
        base_url = self._get_public_base_url()
        return base_url + YOUTUBE_OAUTH_CALLBACK_PATH if base_url else None

    def _eventsub_is_configured(self) -> bool:
        """Whether this deployment is configured for Twitch EventSub webhooks."""
        return bool(self._get_twitch_eventsub_secret() and self._get_twitch_eventsub_callback_url())

    def _kick_events_are_configured(self) -> bool:
        """Whether this deployment is configured for Kick webhook events."""
        client_id, client_secret = self._get_kick_credentials()
        return bool(client_id and client_secret and self._get_kick_events_callback_url())

    def _youtube_oauth_is_configured(self) -> bool:
        """Whether this deployment can complete the optional YouTube owner OAuth flow."""
        client_id, client_secret = self._get_youtube_oauth_credentials()
        return bool(client_id and client_secret and self._get_youtube_oauth_redirect_uri())

    def _eventsub_status_is_healthy(self, status: str) -> bool:
        """Whether an EventSub subscription status is fully healthy."""
        return status == "enabled"

    def _eventsub_status_needs_recreate(self, status: str) -> bool:
        """Whether an EventSub subscription should be deleted and recreated."""
        return status in TWITCH_EVENTSUB_RECREATE_STATUSES or status.endswith("_failed")

    def verify_eventsub_signature(self, body: bytes, message_id: str, timestamp: str, signature: str) -> bool:
        """Verify Twitch EventSub webhook signatures."""
        secret = self._get_twitch_eventsub_secret()
        if not secret:
            return False
        digest = hmac.new(
            secret.encode("utf-8"),
            msg=message_id.encode("utf-8") + timestamp.encode("utf-8") + body,
            digestmod=hashlib.sha256
        ).hexdigest()
        expected = f"sha256={digest}"
        return hmac.compare_digest(expected, signature)

    def _render_alert_message(self, template: Optional[str], context: dict) -> str:
        """Render a user-configurable alert template with safe fallbacks."""
        message_template = template or DEFAULT_ALERT_TEMPLATE
        try:
            return message_template.format(**context)
        except KeyError as error:
            missing = error.args[0]
            logger.warning("Alert template missing placeholder %s, falling back to default template", missing)
            return DEFAULT_ALERT_TEMPLATE.format(**context)

    def _format_alert_preview(self, message_template: Optional[str]) -> str:
        """Format the saved template for confirmations."""
        return message_template or DEFAULT_ALERT_TEMPLATE

    def _get_twitch_preview_url(self, stream: dict) -> str:
        """Build a Twitch preview URL with cache busting so Discord refreshes each stream image."""
        preview_url = stream.get("thumbnail_url", "")
        if not preview_url:
            return ""

        preview_url = preview_url.replace("{width}", "1280").replace("{height}", "720")
        cache_key_parts = [
            str(stream.get("id") or ""),
            str(stream.get("started_at") or ""),
            str(stream.get("title") or "")
        ]
        cache_key = hashlib.md5("|".join(cache_key_parts).encode("utf-8")).hexdigest()[:12]
        separator = "&" if "?" in preview_url else "?"
        return f"{preview_url}{separator}cb={cache_key}"

    def _format_debug_timestamp(self, value: Optional[float]) -> str:
        """Format a stored UNIX timestamp for diagnostics."""
        if not value:
            return "None"
        try:
            dt = datetime.fromtimestamp(float(value))
        except (TypeError, ValueError, OSError):
            return "Invalid"
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    async def _save_alert_check_state(
        self,
        alert_id,
        *,
        status: str,
        error: Optional[str] = None,
        stream_id: Optional[str] = None,
        stream_title: Optional[str] = None,
        stream_started_at: Optional[str] = None
    ) -> None:
        """Persist the latest Twitch check status for diagnostics."""
        update_data = {
            "last_check": discord.utils.utcnow().timestamp(),
            "last_check_status": status,
            "last_check_error": error
        }
        if stream_id is not None:
            update_data["last_content_id"] = stream_id
        if stream_title is not None:
            update_data["last_stream_title"] = stream_title
        if stream_started_at is not None:
            update_data["last_stream_started_at"] = stream_started_at

        await self.db.db.social_alerts.update_one({"_id": alert_id}, {"$set": update_data})

    async def _save_eventsub_state(
        self,
        alert_id,
        *,
        online_status: Optional[str] = None,
        offline_status: Optional[str] = None,
        error: Optional[str] = None
    ) -> None:
        """Persist EventSub subscription diagnostics for an alert."""
        update_data = {
            "eventsub_last_checked": discord.utils.utcnow().timestamp(),
            "eventsub_last_error": error
        }
        if online_status is not None:
            update_data["eventsub_online_status"] = online_status
        if offline_status is not None:
            update_data["eventsub_offline_status"] = offline_status

        await self.db.db.social_alerts.update_one({"_id": alert_id}, {"$set": update_data})

    async def _save_alert_delivery_metadata(
        self,
        alert_id,
        *,
        source: Optional[str] = None,
        eventsub_received_at: Optional[float] = None,
        discord_sent_at: Optional[float] = None,
        eventsub_notification_type: Optional[str] = None,
        eventsub_enriched_at: Optional[float] = None
    ) -> None:
        """Persist alert delivery source/timing metadata for diagnostics."""
        update_data = {}
        if source is not None:
            update_data["last_delivery_source"] = source
        if eventsub_received_at is not None:
            update_data["last_eventsub_received_at"] = eventsub_received_at
        if discord_sent_at is not None:
            update_data["last_discord_sent_at"] = discord_sent_at
        if eventsub_notification_type is not None:
            update_data["last_eventsub_notification_type"] = eventsub_notification_type
        if eventsub_enriched_at is not None:
            update_data["last_eventsub_enriched_at"] = eventsub_enriched_at

        if update_data:
            await self.db.db.social_alerts.update_one({"_id": alert_id}, {"$set": update_data})

    def _should_suppress_poll_alert(self, alert: dict) -> bool:
        """Skip poll-driven sends briefly after a successful EventSub-driven delivery."""
        if alert.get("last_delivery_source") != "eventsub":
            return False

        received_at = alert.get("last_eventsub_received_at")
        if not received_at:
            return False

        try:
            elapsed = discord.utils.utcnow().timestamp() - float(received_at)
        except (TypeError, ValueError):
            return False

        if elapsed < 0 or elapsed > TWITCH_EVENTSUB_POLL_SUPPRESSION_SECONDS:
            return False

        return alert.get("last_check_status") in {"sent", "already_announced"}

    async def _create_alert_record(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str,
        channel: discord.TextChannel,
        message_template: Optional[str]
    ) -> None:
        """Create an alert record and confirm success."""
        normalized_username = self._normalize_alert_username(platform, username)
        alert_data = {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "platform": platform,
            "username": normalized_username,
            "message_template": message_template,
            "last_check": None,
            "last_content_id": None
        }

        result = await self.db.db.social_alerts.insert_one(alert_data)
        alert_data["_id"] = result.inserted_id

        platform_emoji = {
            "twitch": "🟣",
            "youtube": "🔴",
            "kick": "🟢",
            "twitter": "🐦"
        }
        template_preview = self._format_alert_preview(message_template)
        is_live_platform = platform in {"twitch", "youtube", "kick"}

        embed = EmbedFactory.success(
            "Alert Added",
            f"{platform_emoji.get(platform, '📢')} **{platform.title()}** alert added!\n\n"
            f"**Username:** {normalized_username}\n"
            f"**Channel:** {channel.mention}\n"
            f"**Custom Message:**\n{template_preview}\n\n"
            f"You'll be notified when {normalized_username} {'goes live' if is_live_platform else 'posts new content'}!"
        )
        if platform == "twitch":
            await self._reconcile_twitch_eventsub_subscriptions()
            embed.add_field(
                name="Initial Check",
                value="Ran an immediate Twitch status check for this alert.",
                inline=False
            )
        elif platform == "kick":
            await self._reconcile_kick_event_subscriptions()
            embed.add_field(
                name="Webhook Sync",
                value="Synced Kick webhook subscriptions for this alert.",
                inline=False
            )
        if platform in {"twitch", "youtube", "kick"}:
            await self._run_single_alert_check(alert_data)
            if platform == "youtube":
                embed.add_field(
                    name="Initial Check",
                    value=f"Ran an immediate {platform.title()} status check for this alert.",
                    inline=False
                )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info("%s added %s alert for %s", interaction.user, platform, normalized_username)

    async def _update_alert_record(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str,
        channel: Optional[discord.TextChannel],
        message_template: Optional[str],
        existing_alert: dict
    ) -> None:
        """Update an alert record and confirm success."""
        update_data = {
            "message_template": message_template
        }
        if channel is not None:
            update_data["channel_id"] = channel.id

        await self.db.db.social_alerts.update_one({"_id": existing_alert["_id"]}, {"$set": update_data})

        updated_channel = channel or interaction.guild.get_channel(existing_alert["channel_id"])
        template_preview = self._format_alert_preview(message_template)
        embed = EmbedFactory.success(
            "Alert Updated",
            f"Updated alert for **{username}** on **{platform}**.\n\n"
            f"**Channel:** {updated_channel.mention if updated_channel else 'Unknown'}\n"
            f"**Custom Message:**\n{template_preview}"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        if platform == "twitch":
            await self._reconcile_twitch_eventsub_subscriptions()
        elif platform == "kick":
            await self._reconcile_kick_event_subscriptions()
        logger.info("%s updated %s alert for %s", interaction.user, platform, existing_alert["username"])

    def _build_twitch_embed(self, stream: dict, user: dict) -> discord.Embed:
        """Build a rich Twitch live embed."""
        stream_url = f"https://twitch.tv/{user['login']}"
        preview_url = self._get_twitch_preview_url(stream)

        embed = EmbedFactory.create(
            title=stream.get("title") or "Live on Twitch",
            description=f"[Watch now on Twitch]({stream_url})",
            color=TWITCH_EMBED_COLOR,
            timestamp=False
        )
        embed.set_author(name=user.get("display_name", user["login"]), url=stream_url)

        profile_image = user.get("profile_image_url")
        if profile_image:
            embed.set_thumbnail(url=profile_image)

        if preview_url:
            embed.set_image(url=preview_url)

        game_name = stream.get("game_name")
        if game_name:
            embed.add_field(name="Category", value=game_name, inline=True)

        embed.add_field(name="Viewers", value=str(stream.get("viewer_count", 0)), inline=True)
        started_at = stream.get("started_at")
        footer_text = "Twitch"
        if started_at:
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                footer_text = f"Twitch • {started.strftime('%d/%m/%Y, %H:%M')}"
            except ValueError:
                pass
        embed.set_footer(text=footer_text)
        return embed

    def _build_twitch_eventsub_embed(self, event: dict, user: dict) -> discord.Embed:
        """Build a lightweight Twitch embed from EventSub payload data."""
        login = user.get("login") or event.get("broadcaster_user_login") or ""
        stream_url = f"https://twitch.tv/{login}" if login else "https://twitch.tv"
        embed = EmbedFactory.create(
            title="Live on Twitch",
            description=f"[Watch now on Twitch]({stream_url})",
            color=TWITCH_EMBED_COLOR,
            timestamp=False
        )
        embed.set_author(
            name=user.get("display_name") or event.get("broadcaster_user_name") or login or "Twitch Streamer",
            url=stream_url
        )

        profile_image = user.get("profile_image_url")
        if profile_image:
            embed.set_thumbnail(url=profile_image)

        embed.add_field(name="Status", value="Going live now", inline=True)

        started_at = event.get("started_at")
        footer_text = "Twitch"
        if started_at:
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                footer_text = f"Twitch • {started.strftime('%d/%m/%Y, %H:%M')}"
            except ValueError:
                pass
        embed.set_footer(text=footer_text)
        return embed

    def _build_kick_embed(self, channel_data: dict) -> discord.Embed:
        """Build a rich Kick live embed."""
        slug = channel_data.get("slug") or "kick"
        stream_url = f"https://kick.com/{slug}"
        stream = channel_data.get("stream") or {}
        title = channel_data.get("stream_title") or "Live on Kick"

        embed = EmbedFactory.create(
            title=title,
            description=f"[Watch now on Kick]({stream_url})",
            color=KICK_EMBED_COLOR,
            timestamp=False
        )
        embed.set_author(name=slug, url=stream_url)

        thumbnail_url = channel_data.get("banner_picture")
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        preview_url = stream.get("thumbnail")
        if preview_url:
            embed.set_image(url=preview_url)

        category_name = (channel_data.get("category") or {}).get("name")
        if category_name:
            embed.add_field(name="Category", value=category_name, inline=True)

        embed.add_field(name="Viewers", value=str(stream.get("viewer_count", 0)), inline=True)
        footer_text = "Kick"
        started_at = stream.get("start_time")
        if started_at:
            try:
                started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                footer_text = f"Kick • {started.strftime('%d/%m/%Y, %H:%M')}"
            except ValueError:
                pass
        embed.set_footer(text=footer_text)
        return embed

    def _build_youtube_embed(self, video: dict, channel_data: dict) -> discord.Embed:
        """Build a rich YouTube live embed."""
        snippet = channel_data.get("snippet") or {}
        channel_title = snippet.get("title") or channel_data.get("id") or "YouTube Channel"
        video_url = video.get("url") or "https://youtube.com"

        embed = EmbedFactory.create(
            title=video.get("title") or "Live on YouTube",
            description=f"[Watch now on YouTube]({video_url})",
            color=YOUTUBE_EMBED_COLOR,
            timestamp=False
        )
        embed.set_author(name=channel_title, url=video_url)

        thumbnail_url = self._get_best_youtube_thumbnail(snippet.get("thumbnails"))
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        preview_url = video.get("thumbnail_url")
        if preview_url:
            embed.set_image(url=preview_url)

        embed.add_field(name="Viewers", value=str(video.get("viewers", 0)), inline=True)
        footer_text = "YouTube"
        started_at = video.get("started_at")
        if started_at:
            try:
                started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                footer_text = f"YouTube • {started.strftime('%d/%m/%Y, %H:%M')}"
            except ValueError:
                pass
        embed.set_footer(text=footer_text)
        return embed

    async def _get_twitch_access_token(self) -> tuple[Optional[str], Optional[str]]:
        """Get or refresh the Twitch app access token."""
        if self._twitch_access_token and self._twitch_token_expires_at:
            if discord.utils.utcnow() < self._twitch_token_expires_at:
                return self._twitch_access_token, None

        client_id, client_secret = self._get_twitch_credentials()
        if not client_id or not client_secret:
            message = "TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET is missing."
            logger.warning("Twitch alerts are configured but %s", message)
            return None, message

        session = await self.get_session()
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials"
        }

        try:
            async with session.post("https://id.twitch.tv/oauth2/token", data=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to get Twitch access token: %s %s", response.status, body)
                    return None, f"Twitch token request failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Failed to contact Twitch for access token: %s", error, exc_info=True)
            return None, "Could not contact Twitch while requesting an access token."

        self._twitch_access_token = data["access_token"]
        expires_in = max(int(data.get("expires_in", 0)) - 60, 60)
        self._twitch_token_expires_at = discord.utils.utcnow() + timedelta(seconds=expires_in)
        return self._twitch_access_token, None

    async def _get_kick_access_token(self) -> tuple[Optional[str], Optional[str]]:
        """Get or refresh the Kick app access token."""
        if self._kick_access_token and self._kick_token_expires_at:
            if discord.utils.utcnow() < self._kick_token_expires_at:
                return self._kick_access_token, None

        client_id, client_secret = self._get_kick_credentials()
        if not client_id or not client_secret:
            message = "KICK_CLIENT_ID or KICK_CLIENT_SECRET is missing."
            logger.warning("Kick alerts are configured but %s", message)
            return None, message

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

    async def _get_youtube_owner_access_token(self, alert: dict) -> tuple[Optional[str], Optional[str]]:
        """Exchange a stored YouTube refresh token for an owner-scoped access token."""
        refresh_token = alert.get("youtube_refresh_token")
        if not refresh_token:
            return None, "No YouTube OAuth refresh token is stored for this alert."

        client_id, client_secret = self._get_youtube_oauth_credentials()
        if not client_id or not client_secret:
            return None, "YouTube OAuth client credentials are missing."

        session = await self.get_session()
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }

        try:
            async with session.post("https://oauth2.googleapis.com/token", data=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to refresh YouTube owner token: %s %s", response.status, body)
                    return None, f"YouTube OAuth refresh failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Could not refresh YouTube owner token: %s", error, exc_info=True)
            return None, "Could not contact Google while refreshing the YouTube owner token."

        access_token = data.get("access_token")
        if not access_token:
            return None, "Google did not return a YouTube OAuth access token."

        expires_in = int(data.get("expires_in", 3600))
        await self.db.db.social_alerts.update_one(
            {"_id": alert["_id"]},
            {"$set": {"youtube_access_token_expires_at": discord.utils.utcnow().timestamp() + expires_in}}
        )
        return access_token, None

    async def _fetch_youtube_owned_channel(self, access_token: str) -> tuple[Optional[dict], Optional[str]]:
        """Fetch the authenticated owner's YouTube channel."""
        session = await self.get_session()
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"part": "snippet", "mine": "true"}

        try:
            async with session.get("https://www.googleapis.com/youtube/v3/channels", params=params, headers=headers) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to fetch authenticated YouTube channel: %s %s", response.status, body)
                    return None, f"YouTube owner channel lookup failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching authenticated YouTube channel: %s", error, exc_info=True)
            return None, "Could not contact YouTube while loading the authenticated channel."

        items = data.get("items", [])
        if not items:
            return None, "Google did not return a YouTube channel for this authenticated account."
        return items[0], None

    async def _fetch_youtube_video_details(
        self,
        video_id: str,
        *,
        api_key: Optional[str] = None,
        access_token: Optional[str] = None
    ) -> tuple[Optional[dict], Optional[str]]:
        """Fetch detailed YouTube video metadata."""
        session = await self.get_session()
        params = {
            "part": "snippet,liveStreamingDetails",
            "id": video_id
        }
        headers = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        else:
            api_key = api_key or self._get_youtube_api_key()
            if not api_key:
                return None, "YOUTUBE_API_KEY is missing."
            params["key"] = api_key

        try:
            async with session.get("https://www.googleapis.com/youtube/v3/videos", params=params, headers=headers) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to fetch YouTube video details for %s: %s %s", video_id, response.status, body)
                    return None, f"YouTube video lookup failed with HTTP {response.status}."
                video_data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching YouTube video details for %s: %s", video_id, error, exc_info=True)
            return None, "Could not contact YouTube while loading live video details."

        details_items = video_data.get("items", [])
        if not details_items:
            return None, f"No YouTube video details were returned for `{video_id}`."

        detailed_item = details_items[0]
        snippet = detailed_item.get("snippet") or {}
        live_details = detailed_item.get("liveStreamingDetails") or {}
        return {
            "id": video_id,
            "title": snippet.get("title") or "Live on YouTube",
            "thumbnail_url": self._get_best_youtube_thumbnail(snippet.get("thumbnails")),
            "started_at": live_details.get("actualStartTime") or snippet.get("publishedAt"),
            "viewers": int(live_details.get("concurrentViewers") or 0),
            "url": f"https://www.youtube.com/watch?v={video_id}"
        }, None

    async def _fetch_youtube_live_video_via_oauth(self, alert: dict) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
        """Check the authenticated owner's live broadcast state via the YouTube Live Streaming API."""
        access_token, token_error = await self._get_youtube_owner_access_token(alert)
        if not access_token:
            return None, None, token_error

        owner_channel, owner_error = await self._fetch_youtube_owned_channel(access_token)
        if not owner_channel:
            return None, None, owner_error

        owner_channel_id = owner_channel.get("id")
        expected_channel_id = alert.get("youtube_channel_id")
        if expected_channel_id and owner_channel_id and owner_channel_id != expected_channel_id:
            return None, None, "The authenticated YouTube account does not own the configured alert channel."

        session = await self.get_session()
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "part": "id,snippet,status",
            "mine": "true",
            "broadcastStatus": "active",
            "broadcastType": "all"
        }

        try:
            async with session.get("https://www.googleapis.com/youtube/v3/liveBroadcasts", params=params, headers=headers) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to fetch YouTube live broadcasts via OAuth: %s %s", response.status, body)
                    return None, owner_channel, f"YouTube Live API lookup failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching YouTube live broadcasts via OAuth: %s", error, exc_info=True)
            return None, owner_channel, "Could not contact YouTube while loading owner live broadcasts."

        items = data.get("items", [])
        if not items:
            return None, owner_channel, None

        broadcast = items[0]
        video_id = broadcast.get("id")
        if not video_id:
            return None, owner_channel, "YouTube Live API returned an active broadcast without an ID."

        video, video_error = await self._fetch_youtube_video_details(video_id, access_token=access_token)
        return video, owner_channel, video_error

    async def _create_oauth_state(self, *, platform: str, alert_id: str, guild_id: int, username: str) -> str:
        """Create a short-lived OAuth state token in MongoDB."""
        token = secrets.token_urlsafe(24)
        await self.db.db.social_alert_oauth_states.insert_one({
            "state": token,
            "platform": platform,
            "alert_id": alert_id,
            "guild_id": guild_id,
            "username": username,
            "created_at": discord.utils.utcnow().timestamp(),
            "expires_at": (discord.utils.utcnow() + timedelta(minutes=15)).timestamp()
        })
        return token

    async def _consume_oauth_state(self, state: str, platform: str) -> Optional[dict]:
        """Atomically load and delete a stored OAuth state token."""
        document = await self.db.db.social_alert_oauth_states.find_one_and_delete({
            "state": state,
            "platform": platform
        })
        if not document:
            return None
        expires_at = document.get("expires_at")
        if expires_at and expires_at < discord.utils.utcnow().timestamp():
            return None
        return document

    async def _get_kick_public_key(self):
        """Fetch and cache Kick's RSA public key for webhook verification."""
        if self._kick_public_key and self._kick_public_key_fetched_at:
            if discord.utils.utcnow() - self._kick_public_key_fetched_at < timedelta(hours=6):
                return self._kick_public_key

        session = await self.get_session()
        pem = None
        try:
            async with session.get("https://api.kick.com/public/v1/public-key") as response:
                if response.status == 200:
                    body = await response.text()
                    body = body.strip()
                    if body:
                        pem = body
                else:
                    logger.warning("Failed to refresh Kick public key: HTTP %s", response.status)
        except aiohttp.ClientError as error:
            logger.warning("Could not fetch Kick public key dynamically: %s", error)

        pem = pem or KICK_WEBHOOK_PUBLIC_KEY_FALLBACK
        self._kick_public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
        self._kick_public_key_fetched_at = discord.utils.utcnow()
        return self._kick_public_key

    async def verify_kick_event_signature(self, body: bytes, message_id: str, timestamp: str, signature: str) -> bool:
        """Verify Kick webhook signatures against Kick's public key."""
        try:
            public_key = await self._get_kick_public_key()
            signed_message = message_id.encode("utf-8") + b"." + timestamp.encode("utf-8") + b"." + body
            decoded_signature = base64.b64decode(signature)
            public_key.verify(
                decoded_signature,
                signed_message,
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            return True
        except (ValueError, InvalidSignature, TypeError) as error:
            logger.warning("Kick webhook signature verification failed: %s", error)
            return False

    def _get_best_youtube_thumbnail(self, thumbnails: Optional[dict]) -> Optional[str]:
        """Pick the highest-value YouTube thumbnail URL available."""
        if not thumbnails:
            return None
        for key in ("maxres", "standard", "high", "medium", "default"):
            url = (thumbnails.get(key) or {}).get("url")
            if url:
                return url
        return None

    async def _fetch_youtube_channel(
        self,
        raw_value: str,
        *,
        channel_id: Optional[str] = None
    ) -> tuple[Optional[dict], Optional[str]]:
        """Fetch YouTube channel metadata from a handle, username, or channel ID."""
        api_key = self._get_youtube_api_key()
        if not api_key:
            return None, "YOUTUBE_API_KEY is missing."

        lookup_type = "id"
        lookup_value = channel_id or raw_value
        if not channel_id:
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

        return items[0], None

    async def _fetch_youtube_live_video(self, channel_id: str) -> tuple[Optional[dict], Optional[str]]:
        """Fetch the currently live YouTube video for a channel, if any."""
        api_key = self._get_youtube_api_key()
        if not api_key:
            return None, "YOUTUBE_API_KEY is missing."

        session = await self.get_session()
        search_params = {
            "part": "snippet",
            "channelId": channel_id,
            "eventType": "live",
            "type": "video",
            "maxResults": 1,
            "order": "date",
            "key": api_key
        }

        try:
            async with session.get("https://www.googleapis.com/youtube/v3/search", params=search_params) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to search live YouTube video for %s: %s %s", channel_id, response.status, body)
                    return None, f"YouTube live lookup failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error searching live YouTube video for %s: %s", channel_id, error, exc_info=True)
            return None, "Could not contact YouTube while checking live status."

        items = data.get("items", [])
        if not items:
            return None, None

        search_item = items[0]
        video_id = ((search_item.get("id") or {}).get("videoId") or "").strip()
        if not video_id:
            return None, "YouTube returned a live result without a video ID."
        video, video_error = await self._fetch_youtube_video_details(video_id, api_key=api_key)
        if not video:
            return None, video_error
        if not video.get("thumbnail_url"):
            video["thumbnail_url"] = self._get_best_youtube_thumbnail(search_item.get("snippet", {}).get("thumbnails"))
        return video, None

    async def _fetch_kick_channels_batch(self, slugs: list[str]) -> tuple[dict[str, dict], Optional[str]]:
        """Fetch Kick channel metadata in batches."""
        if not slugs:
            return {}, None

        token, token_error = await self._get_kick_access_token()
        if not token:
            return {}, token_error or "Kick credentials are unavailable."

        session = await self.get_session()
        headers = {"Authorization": f"Bearer {token}"}
        results: dict[str, dict] = {}

        try:
            for index in range(0, len(slugs), 50):
                batch = slugs[index:index + 50]
                params = [("slug", slug) for slug in batch]
                async with session.get("https://api.kick.com/public/v1/channels", params=params, headers=headers) as response:
                    if response.status != 200:
                        body = await response.text()
                        logger.error("Failed batch Kick channel lookup: %s %s", response.status, body)
                        return {}, f"Kick channel lookup failed with HTTP {response.status}."
                    data = await response.json()

                for channel in data.get("data", []):
                    slug = (channel.get("slug") or "").lower()
                    if slug:
                        results[slug] = channel
        except aiohttp.ClientError as error:
            logger.error("Error fetching Kick channel batch: %s", error, exc_info=True)
            return {}, "Could not contact Kick while looking up channels."

        return results, None

    async def _fetch_kick_channel(self, slug: str) -> tuple[Optional[dict], Optional[str]]:
        """Fetch Kick channel metadata for a single slug."""
        channels, error = await self._fetch_kick_channels_batch([slug])
        if error:
            return None, error
        return channels.get(slug.lower()), None

    async def _list_kick_event_subscriptions(self) -> tuple[list[dict], Optional[str]]:
        """List current Kick webhook event subscriptions for this application."""
        token, token_error = await self._get_kick_access_token()
        if not token:
            return [], token_error or "Kick credentials are unavailable."

        session = await self.get_session()
        headers = {"Authorization": f"Bearer {token}"}

        try:
            async with session.get("https://api.kick.com/public/v1/events/subscriptions", headers=headers) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to list Kick event subscriptions: %s %s", response.status, body)
                    return [], f"Kick subscription listing failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error listing Kick event subscriptions: %s", error, exc_info=True)
            return [], "Could not contact Kick while listing webhook subscriptions."

        return data.get("data", []), None

    async def _create_kick_event_subscriptions(self, broadcaster_id: int, event_names: list[str]) -> tuple[dict[str, str], Optional[str]]:
        """Create Kick webhook subscriptions for one broadcaster."""
        token, token_error = await self._get_kick_access_token()
        callback_url = self._get_kick_events_callback_url()
        if not token:
            return {}, token_error or "Kick credentials are unavailable."
        if not callback_url:
            return {}, "Kick webhook callback URL is missing."

        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        payload = {
            "broadcaster_user_id": int(broadcaster_id),
            "events": [{"name": name, "version": 1} for name in event_names],
            "method": "webhook"
        }

        try:
            async with session.post("https://api.kick.com/public/v1/events/subscriptions", json=payload, headers=headers) as response:
                if response.status not in (200, 201, 202, 409):
                    body = await response.text()
                    logger.error("Failed to create Kick subscriptions: %s %s", response.status, body)
                    return {}, f"Kick subscription creation failed with HTTP {response.status}."
                data = await response.json() if response.status != 409 else {}
        except aiohttp.ClientError as error:
            logger.error("Error creating Kick subscriptions: %s", error, exc_info=True)
            return {}, "Could not contact Kick while creating webhook subscriptions."

        created = {}
        for item in data.get("data", []):
            event_name = item.get("event")
            sub_id = item.get("id")
            if event_name and sub_id:
                created[event_name] = sub_id
        return created, None

    async def _delete_kick_event_subscription(self, subscription_id: str) -> tuple[bool, Optional[str]]:
        """Delete one Kick webhook subscription."""
        token, token_error = await self._get_kick_access_token()
        if not token:
            return False, token_error or "Kick credentials are unavailable."

        session = await self.get_session()
        headers = {"Authorization": f"Bearer {token}"}

        try:
            async with session.delete(f"https://api.kick.com/public/v1/events/subscriptions/{subscription_id}", headers=headers) as response:
                if response.status not in (200, 204, 404):
                    body = await response.text()
                    logger.error("Failed to delete Kick subscription %s: %s %s", subscription_id, response.status, body)
                    return False, f"Kick subscription deletion failed with HTTP {response.status}."
        except aiohttp.ClientError as error:
            logger.error("Error deleting Kick subscription %s: %s", subscription_id, error, exc_info=True)
            return False, "Could not contact Kick while deleting webhook subscriptions."

        return True, None

    async def _save_kick_subscription_state(
        self,
        alert_id,
        *,
        status_status: Optional[str] = None,
        metadata_status: Optional[str] = None,
        error: Optional[str] = None
    ) -> None:
        """Persist Kick webhook subscription diagnostics for an alert."""
        update_data = {
            "kick_last_checked": discord.utils.utcnow().timestamp(),
            "kick_last_error": error
        }
        if status_status is not None:
            update_data["kick_status_subscription"] = status_status
        if metadata_status is not None:
            update_data["kick_metadata_subscription"] = metadata_status
        await self.db.db.social_alerts.update_one({"_id": alert_id}, {"$set": update_data})

    async def _reconcile_kick_event_subscriptions(self) -> dict[str, int]:
        """Ensure Kick webhook subscriptions exist for configured Kick alerts."""
        summary = {
            "alerts": 0,
            "resolved": 0,
            "created": 0,
            "deleted": 0,
            "failed": 0,
            "healthy": 0,
            "missing": 0,
        }
        if not self._kick_events_are_configured():
            return summary

        alerts = await self.db.db.social_alerts.find({"platform": "kick"}).to_list(length=1000)
        summary["alerts"] = len(alerts)
        if not alerts:
            return summary

        slugs = sorted({alert["username"].lower() for alert in alerts})
        channels_by_slug, lookup_error = await self._fetch_kick_channels_batch(slugs)
        if lookup_error:
            summary["failed"] = len(alerts)
            for alert in alerts:
                await self._save_kick_subscription_state(alert["_id"], error=lookup_error)
            return summary

        desired_broadcasters: dict[int, str] = {}
        for alert in alerts:
            channel_data = channels_by_slug.get(alert["username"].lower())
            if not channel_data:
                summary["failed"] += 1
                await self._save_kick_subscription_state(
                    alert["_id"],
                    status_status="missing",
                    metadata_status="missing",
                    error=f"No Kick channel found for `{alert['username']}`."
                )
                continue
            raw_broadcaster_id = channel_data.get("broadcaster_user_id")
            if raw_broadcaster_id is None:
                summary["failed"] += 1
                await self._save_kick_subscription_state(
                    alert["_id"],
                    status_status="missing",
                    metadata_status="missing",
                    error=f"Kick did not return a broadcaster ID for `{alert['username']}`."
                )
                continue
            broadcaster_id = int(raw_broadcaster_id)
            desired_broadcasters[broadcaster_id] = channel_data.get("slug", alert["username"])
            summary["resolved"] += 1
            if alert.get("kick_broadcaster_id") != broadcaster_id:
                await self.db.db.social_alerts.update_one(
                    {"_id": alert["_id"]},
                    {"$set": {"kick_broadcaster_id": broadcaster_id}}
                )

        subscriptions, sub_error = await self._list_kick_event_subscriptions()
        if sub_error:
            summary["failed"] += len(desired_broadcasters)
            for alert in alerts:
                await self._save_kick_subscription_state(alert["_id"], error=sub_error)
            return summary

        existing_map: dict[tuple[int, str], dict] = {}
        for subscription in subscriptions:
            broadcaster_id = subscription.get("broadcaster_user_id")
            event_name = subscription.get("event")
            if not broadcaster_id or event_name not in KICK_EVENT_TYPES:
                continue
            if subscription.get("method") != "webhook":
                continue
            existing_map[(int(broadcaster_id), event_name)] = subscription

        for broadcaster_id in desired_broadcasters:
            missing_events = [
                event_name
                for event_name in KICK_EVENT_TYPES
                if (broadcaster_id, event_name) not in existing_map
            ]
            if not missing_events:
                continue
            created, create_error = await self._create_kick_event_subscriptions(broadcaster_id, missing_events)
            if create_error:
                summary["failed"] += 1
                logger.warning("Failed to ensure Kick subscriptions for broadcaster %s: %s", broadcaster_id, create_error)
            else:
                summary["created"] += len(created) or len(missing_events)

        refreshed_subscriptions, refreshed_error = await self._list_kick_event_subscriptions()
        refreshed_lookup: dict[tuple[int, str], dict] = {}
        if not refreshed_error:
            for subscription in refreshed_subscriptions:
                broadcaster_id = subscription.get("broadcaster_user_id")
                event_name = subscription.get("event")
                if not broadcaster_id or event_name not in KICK_EVENT_TYPES:
                    continue
                if subscription.get("method") != "webhook":
                    continue
                refreshed_lookup[(int(broadcaster_id), event_name)] = subscription

        for alert in alerts:
            broadcaster_id = alert.get("kick_broadcaster_id")
            if not broadcaster_id:
                continue
            status_sub = refreshed_lookup.get((int(broadcaster_id), "livestream.status.updated"))
            metadata_sub = refreshed_lookup.get((int(broadcaster_id), "livestream.metadata.updated"))
            status_value = "enabled" if status_sub else "missing"
            metadata_value = "enabled" if metadata_sub else "missing"
            error = refreshed_error
            if not error and "missing" in {status_value, metadata_value}:
                error = "One or more Kick webhook subscriptions are missing."
            await self._save_kick_subscription_state(
                alert["_id"],
                status_status=status_value,
                metadata_status=metadata_value,
                error=error
            )
            if status_value == "enabled" and metadata_value == "enabled":
                summary["healthy"] += 1
            else:
                summary["missing"] += 1

        return summary

    async def _fetch_twitch_user(self, username: str) -> tuple[Optional[dict], Optional[str]]:
        """Fetch Twitch user metadata."""
        cached = self._twitch_user_cache.get(username.lower())
        if cached and cached.get("expires_at") and discord.utils.utcnow() < cached["expires_at"]:
            return cached["user"], None

        token, token_error = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not token or not client_id:
            return None, token_error or "Twitch credentials are unavailable."

        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id
        }

        try:
            async with session.get(
                "https://api.twitch.tv/helix/users",
                params={"login": username},
                headers=headers
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to fetch Twitch user %s: %s %s", username, response.status, body)
                    return None, f"Twitch user lookup failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching Twitch user %s: %s", username, error, exc_info=True)
            return None, "Could not contact Twitch while looking up the channel."

        users = data.get("data", [])
        if not users:
            return None, f"No Twitch channel found for `{username}`."
        user = users[0]
        self._twitch_user_cache[username.lower()] = {
            "user": user,
            "expires_at": discord.utils.utcnow() + timedelta(hours=6)
        }
        return user, None

    async def _fetch_twitch_users_batch(self, usernames: list[str]) -> tuple[dict[str, dict], Optional[str]]:
        """Fetch Twitch user metadata in batches and refresh the local cache."""
        usernames = [username.lower() for username in usernames]
        resolved: dict[str, dict] = {}
        uncached = []

        now = discord.utils.utcnow()
        for username in usernames:
            cached = self._twitch_user_cache.get(username)
            if cached and cached.get("expires_at") and now < cached["expires_at"]:
                resolved[username] = cached["user"]
            else:
                uncached.append(username)

        if not uncached:
            return resolved, None

        token, token_error = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not token or not client_id:
            return resolved, token_error or "Twitch credentials are unavailable."

        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id
        }

        try:
            for index in range(0, len(uncached), 100):
                batch = uncached[index:index + 100]
                params = [("login", username) for username in batch]
                async with session.get(
                    "https://api.twitch.tv/helix/users",
                    params=params,
                    headers=headers
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        logger.error("Failed batch Twitch user lookup: %s %s", response.status, body)
                        return resolved, f"Twitch user lookup failed with HTTP {response.status}."
                    data = await response.json()

                for user in data.get("data", []):
                    login = user["login"].lower()
                    self._twitch_user_cache[login] = {
                        "user": user,
                        "expires_at": discord.utils.utcnow() + timedelta(hours=6)
                    }
                    resolved[login] = user
        except aiohttp.ClientError as error:
            logger.error("Error fetching Twitch user batch: %s", error, exc_info=True)
            return resolved, "Could not contact Twitch while looking up channels."

        return resolved, None

    async def _fetch_twitch_stream(self, user_id: str) -> tuple[Optional[dict], Optional[str]]:
        """Fetch current live Twitch stream for a user ID."""
        token, token_error = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not token or not client_id:
            return None, token_error or "Twitch credentials are unavailable."

        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id
        }

        try:
            async with session.get(
                "https://api.twitch.tv/helix/streams",
                params={"user_id": user_id},
                headers=headers
            ) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to fetch Twitch stream for %s: %s %s", user_id, response.status, body)
                    return None, f"Twitch stream lookup failed with HTTP {response.status}."
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching Twitch stream for %s: %s", user_id, error, exc_info=True)
            return None, "Could not contact Twitch while checking live status."

        streams = data.get("data", [])
        return (streams[0] if streams else None), None

    async def _fetch_twitch_streams_batch(self, user_ids: list[str]) -> tuple[dict[str, dict], Optional[str]]:
        """Fetch current live streams for multiple Twitch user IDs in batches."""
        if not user_ids:
            return {}, None

        token, token_error = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not token or not client_id:
            return {}, token_error or "Twitch credentials are unavailable."

        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id
        }
        results: dict[str, dict] = {}

        try:
            for index in range(0, len(user_ids), 100):
                batch = user_ids[index:index + 100]
                params = [("user_id", user_id) for user_id in batch]
                async with session.get(
                    "https://api.twitch.tv/helix/streams",
                    params=params,
                    headers=headers
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        logger.error("Failed batch Twitch stream lookup: %s %s", response.status, body)
                        return {}, f"Twitch stream lookup failed with HTTP {response.status}."
                    data = await response.json()

                for stream in data.get("data", []):
                    results[stream["user_id"]] = stream
        except aiohttp.ClientError as error:
            logger.error("Error fetching Twitch stream batch: %s", error, exc_info=True)
            return {}, "Could not contact Twitch while checking live status."

        return results, None

    async def _list_twitch_eventsub_subscriptions(self) -> tuple[list[dict], Optional[str]]:
        """List current Twitch EventSub subscriptions for this application."""
        token, token_error = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not token or not client_id:
            return [], token_error or "Twitch credentials are unavailable."

        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id
        }

        subscriptions = []
        cursor = None
        try:
            while True:
                params = {"after": cursor} if cursor else None
                async with session.get(
                    "https://api.twitch.tv/helix/eventsub/subscriptions",
                    params=params,
                    headers=headers
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        logger.error("Failed to list Twitch EventSub subscriptions: %s %s", response.status, body)
                        return [], f"Twitch EventSub listing failed with HTTP {response.status}."
                    data = await response.json()

                subscriptions.extend(data.get("data", []))
                cursor = data.get("pagination", {}).get("cursor")
                if not cursor:
                    break
        except aiohttp.ClientError as error:
            logger.error("Error listing Twitch EventSub subscriptions: %s", error, exc_info=True)
            return [], "Could not contact Twitch while listing EventSub subscriptions."

        return subscriptions, None

    async def _create_twitch_eventsub_subscription(self, broadcaster_id: str, event_type: str) -> tuple[bool, Optional[str]]:
        """Create a Twitch EventSub webhook subscription."""
        callback_url = self._get_twitch_eventsub_callback_url()
        secret = self._get_twitch_eventsub_secret()
        token, token_error = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not callback_url or not secret:
            return False, "EventSub callback URL or secret is missing."
        if not token or not client_id:
            return False, token_error or "Twitch credentials are unavailable."

        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id,
            "Content-Type": "application/json"
        }
        payload = {
            "type": event_type,
            "version": "1",
            "condition": {"broadcaster_user_id": broadcaster_id},
            "transport": {
                "method": "webhook",
                "callback": callback_url,
                "secret": secret
            }
        }

        try:
            async with session.post(
                "https://api.twitch.tv/helix/eventsub/subscriptions",
                headers=headers,
                json=payload
            ) as response:
                if response.status not in (202, 409):
                    body = await response.text()
                    logger.error("Failed to create Twitch EventSub subscription: %s %s", response.status, body)
                    return False, f"Twitch EventSub creation failed with HTTP {response.status}."
        except aiohttp.ClientError as error:
            logger.error("Error creating Twitch EventSub subscription: %s", error, exc_info=True)
            return False, "Could not contact Twitch while creating an EventSub subscription."

        return True, None

    async def _delete_twitch_eventsub_subscription(self, subscription_id: str) -> tuple[bool, Optional[str]]:
        """Delete a Twitch EventSub subscription."""
        token, token_error = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not token or not client_id:
            return False, token_error or "Twitch credentials are unavailable."

        session = await self.get_session()
        headers = {
            "Authorization": f"Bearer {token}",
            "Client-Id": client_id
        }

        try:
            async with session.delete(
                "https://api.twitch.tv/helix/eventsub/subscriptions",
                params={"id": subscription_id},
                headers=headers
            ) as response:
                if response.status not in (204, 404):
                    body = await response.text()
                    logger.error("Failed to delete Twitch EventSub subscription %s: %s %s", subscription_id, response.status, body)
                    return False, f"Twitch EventSub deletion failed with HTTP {response.status}."
        except aiohttp.ClientError as error:
            logger.error("Error deleting Twitch EventSub subscription %s: %s", subscription_id, error, exc_info=True)
            return False, "Could not contact Twitch while deleting an EventSub subscription."

        return True, None

    async def _reconcile_twitch_eventsub_subscriptions(self) -> dict[str, int]:
        """Ensure Twitch EventSub subscriptions exist for configured Twitch alerts."""
        summary = {
            "alerts": 0,
            "resolved": 0,
            "created": 0,
            "deleted": 0,
            "failed": 0,
            "healthy": 0,
            "missing": 0,
        }
        if not self._eventsub_is_configured():
            return summary

        cursor = self.db.db.social_alerts.find({"platform": "twitch"})
        alerts = await cursor.to_list(length=1000)
        summary["alerts"] = len(alerts)
        if not alerts:
            return summary

        usernames = sorted({alert["username"].lower() for alert in alerts})
        users_by_login, user_error = await self._fetch_twitch_users_batch(usernames)
        if user_error:
            logger.warning("Skipping EventSub reconcile because Twitch users could not be resolved: %s", user_error)
            summary["failed"] = len(alerts)
            for alert in alerts:
                await self._save_eventsub_state(alert["_id"], error=user_error)
            return summary

        desired_broadcasters: dict[str, str] = {}
        for alert in alerts:
            user = users_by_login.get(alert["username"].lower())
            if not user:
                summary["failed"] += 1
                await self._save_eventsub_state(
                    alert["_id"],
                    online_status="missing",
                    offline_status="missing",
                    error=f"No Twitch channel found for `{alert['username']}`."
                )
                continue
            desired_broadcasters[user["id"]] = user["login"].lower()
            summary["resolved"] += 1
            if alert.get("twitch_broadcaster_id") != user["id"]:
                await self.db.db.social_alerts.update_one(
                    {"_id": alert["_id"]},
                    {"$set": {"twitch_broadcaster_id": user["id"]}}
                )

        subscriptions, sub_error = await self._list_twitch_eventsub_subscriptions()
        if sub_error:
            logger.warning("Skipping EventSub reconcile because current subscriptions could not be listed: %s", sub_error)
            summary["failed"] += len(desired_broadcasters)
            for alert in alerts:
                await self._save_eventsub_state(alert["_id"], error=sub_error)
            return summary

        callback_url = self._get_twitch_eventsub_callback_url()
        existing_map = {}
        for subscription in subscriptions:
            if subscription.get("type") not in TWITCH_EVENTSUB_TYPES:
                continue
            transport = subscription.get("transport", {})
            if transport.get("method") != "webhook" or transport.get("callback") != callback_url:
                continue
            broadcaster_id = subscription.get("condition", {}).get("broadcaster_user_id")
            if not broadcaster_id:
                continue
            existing_map[(broadcaster_id, subscription["type"])] = subscription

        for key, subscription in list(existing_map.items()):
            if key[0] not in desired_broadcasters:
                continue
            status = subscription.get("status", "unknown")
            if self._eventsub_status_needs_recreate(status):
                ok, error = await self._delete_twitch_eventsub_subscription(subscription["id"])
                if not ok:
                    summary["failed"] += 1
                    logger.warning(
                        "Failed to delete unhealthy EventSub subscription %s (%s): %s",
                        subscription["id"],
                        status,
                        error
                    )
                else:
                    summary["deleted"] += 1
                    existing_map.pop(key, None)

        for broadcaster_id in desired_broadcasters:
            for event_type in TWITCH_EVENTSUB_TYPES:
                if (broadcaster_id, event_type) not in existing_map:
                    ok, error = await self._create_twitch_eventsub_subscription(broadcaster_id, event_type)
                    if not ok:
                        summary["failed"] += 1
                        logger.warning("Failed to ensure EventSub %s for broadcaster %s: %s", event_type, broadcaster_id, error)
                    else:
                        summary["created"] += 1

        desired_pairs = {(broadcaster_id, event_type) for broadcaster_id in desired_broadcasters for event_type in TWITCH_EVENTSUB_TYPES}
        for key, subscription in existing_map.items():
            if key not in desired_pairs:
                ok, error = await self._delete_twitch_eventsub_subscription(subscription["id"])
                if not ok:
                    summary["failed"] += 1
                    logger.warning("Failed to delete orphan EventSub subscription %s: %s", subscription["id"], error)
                else:
                    summary["deleted"] += 1

        refreshed_subscriptions, refreshed_error = await self._list_twitch_eventsub_subscriptions()
        status_lookup = {}
        if not refreshed_error:
            for subscription in refreshed_subscriptions:
                if subscription.get("type") not in TWITCH_EVENTSUB_TYPES:
                    continue
                if subscription.get("condition", {}).get("broadcaster_user_id") not in desired_broadcasters:
                    continue
                transport = subscription.get("transport", {})
                if transport.get("method") != "webhook" or transport.get("callback") != callback_url:
                    continue
                status_lookup[(subscription["condition"]["broadcaster_user_id"], subscription["type"])] = subscription.get("status", "unknown")

        for alert in alerts:
            broadcaster_id = alert.get("twitch_broadcaster_id")
            if not broadcaster_id:
                continue

            online_status = status_lookup.get((broadcaster_id, "stream.online"), "missing")
            offline_status = status_lookup.get((broadcaster_id, "stream.offline"), "missing")
            error = refreshed_error
            if not error:
                if "missing" in (online_status, offline_status):
                    error = "One or more Twitch EventSub subscriptions are missing."
                elif not (
                    self._eventsub_status_is_healthy(online_status)
                    and self._eventsub_status_is_healthy(offline_status)
                ):
                    error = (
                        "One or more Twitch EventSub subscriptions are not fully enabled yet. "
                        f"online={online_status}, offline={offline_status}"
                    )

            await self._save_eventsub_state(
                alert["_id"],
                online_status=online_status,
                offline_status=offline_status,
                error=error
            )
            if (
                self._eventsub_status_is_healthy(online_status)
                and self._eventsub_status_is_healthy(offline_status)
            ):
                summary["healthy"] += 1
            else:
                summary["missing"] += 1

        return summary

    async def _get_eventsub_status_for_broadcaster(self, broadcaster_id: str) -> tuple[dict[str, str], Optional[str]]:
        """Return EventSub subscription status for the given broadcaster."""
        subscriptions, error = await self._list_twitch_eventsub_subscriptions()
        if error:
            return {}, error

        callback_url = self._get_twitch_eventsub_callback_url()
        status_map: dict[str, str] = {}
        for subscription in subscriptions:
            if subscription.get("type") not in TWITCH_EVENTSUB_TYPES:
                continue
            if subscription.get("condition", {}).get("broadcaster_user_id") != broadcaster_id:
                continue
            transport = subscription.get("transport", {})
            if transport.get("method") != "webhook" or transport.get("callback") != callback_url:
                continue
            status_map[subscription["type"]] = subscription.get("status", "unknown")

        return status_map, None

    async def handle_twitch_eventsub_request(self, message_type: str, payload: dict) -> Optional[str]:
        """Process Twitch EventSub webhook payloads."""
        if message_type == "webhook_callback_verification":
            subscription = payload.get("subscription", {})
            logger.info(
                "Verified Twitch EventSub subscription for %s (%s)",
                subscription.get("type"),
                subscription.get("condition", {}).get("broadcaster_user_id")
            )
            return payload.get("challenge", "")

        if message_type == "revocation":
            subscription = payload.get("subscription", {})
            logger.warning(
                "Twitch EventSub subscription revoked for %s (%s): %s",
                subscription.get("type"),
                subscription.get("condition", {}).get("broadcaster_user_id"),
                subscription.get("status")
            )
            return None

        if message_type != "notification":
            return None

        subscription = payload.get("subscription", {})
        event = payload.get("event", {})
        subscription_type = subscription.get("type")
        broadcaster_id = event.get("broadcaster_user_id") or subscription.get("condition", {}).get("broadcaster_user_id")
        if not broadcaster_id or subscription_type not in TWITCH_EVENTSUB_TYPES:
            return None

        alerts = await self.db.db.social_alerts.find({
            "platform": "twitch",
            "$or": [
                {"twitch_broadcaster_id": broadcaster_id},
                {"username": event.get("broadcaster_user_login", "").lower()}
            ]
        }).to_list(length=1000)

        if not alerts:
            return None

        if subscription_type == "stream.offline":
            existing_task = self._eventsub_live_tasks.pop(broadcaster_id, None)
            if existing_task and not existing_task.done():
                existing_task.cancel()
            for alert in alerts:
                await self._save_alert_delivery_metadata(
                    alert["_id"],
                    source="eventsub",
                    eventsub_received_at=discord.utils.utcnow().timestamp(),
                    eventsub_notification_type="stream.offline"
                )
                await self._save_alert_check_state(alert["_id"], status="offline", error=None, stream_id=None)
                if alert.get("last_content_id") is not None:
                    await self.db.db.social_alerts.update_one(
                        {"_id": alert["_id"]},
                        {"$set": {"last_content_id": None}}
                    )
            return None

        existing_task = self._eventsub_live_tasks.get(broadcaster_id)
        if existing_task and not existing_task.done():
            logger.info("Ignoring duplicate Twitch EventSub live notification for broadcaster %s while a live task is already running", broadcaster_id)
            return None

        task = asyncio.create_task(
            self._process_twitch_eventsub_live_notification(
                broadcaster_id=broadcaster_id,
                alerts=alerts,
                event=event
            )
        )
        self._eventsub_live_tasks[broadcaster_id] = task
        return None

    async def _process_twitch_eventsub_live_notification(
        self,
        *,
        broadcaster_id: str,
        alerts: list[dict],
        event: dict
    ) -> None:
        """Resolve a Twitch stream shortly after EventSub says it went live."""
        try:
            event_received_at = discord.utils.utcnow().timestamp()
            for alert in alerts:
                await self._save_alert_delivery_metadata(
                    alert["_id"],
                    source="eventsub",
                    eventsub_received_at=event_received_at,
                    eventsub_notification_type="stream.online"
                )

            user_login = (event.get("broadcaster_user_login") or "").lower()
            if user_login:
                cached = self._twitch_user_cache.get(user_login)
                if cached and cached.get("expires_at") and discord.utils.utcnow() < cached["expires_at"]:
                    user = cached["user"]
                else:
                    user, user_error = await self._fetch_twitch_user(user_login)
                    if not user:
                        logger.warning(
                            "EventSub received live event but Twitch user lookup failed for %s: %s",
                            user_login,
                            user_error
                        )
                        return
            else:
                user = {
                    "id": broadcaster_id,
                    "login": "",
                    "display_name": event.get("broadcaster_user_name") or "Unknown"
                }

            stream_identifier = event.get("id") or f"eventsub:{broadcaster_id}:{event.get('started_at') or ''}"
            provisional_messages: list[tuple[dict, discord.Message]] = []
            for alert in alerts:
                if stream_identifier == alert.get("last_content_id"):
                    await self._save_alert_check_state(
                        alert["_id"],
                        status="already_announced",
                        error=None,
                        stream_id=stream_identifier,
                        stream_started_at=event.get("started_at")
                    )
                    continue

                guild = self.bot.get_guild(alert["guild_id"])
                if not guild:
                    continue
                channel = guild.get_channel(alert["channel_id"])
                if not isinstance(channel, discord.TextChannel):
                    continue

                message = await self._send_twitch_eventsub_provisional_alert(alert, channel, event, user)
                if message is None:
                    await self._save_alert_check_state(
                        alert["_id"],
                        status="send_failed",
                        error="Discord rejected the provisional EventSub alert message."
                    )
                    continue

                provisional_messages.append((alert, message))
                await self._save_alert_delivery_metadata(
                    alert["_id"],
                    source="eventsub",
                    discord_sent_at=discord.utils.utcnow().timestamp()
                )
                await self._save_alert_check_state(
                    alert["_id"],
                    status="sent",
                    error=None,
                    stream_id=stream_identifier,
                    stream_title="Live on Twitch",
                    stream_started_at=event.get("started_at")
                )

            stream = None
            stream_error = None
            for attempt in range(12):
                stream, stream_error = await self._fetch_twitch_stream(broadcaster_id)
                if stream:
                    if attempt:
                        logger.info(
                            "Resolved Twitch stream for broadcaster %s via EventSub after %s retries",
                            broadcaster_id,
                            attempt
                        )
                    break
                if stream_error:
                    break
                await asyncio.sleep(1)

            if stream_error:
                logger.warning(
                    "EventSub received live event but stream fetch failed for broadcaster %s after retries: %s",
                    broadcaster_id,
                    stream_error
                )
                return

            if not stream:
                return

            rich_embed = self._build_twitch_embed(stream, user)
            stream_url = f"https://twitch.tv/{user['login']}"

            for alert, message in provisional_messages:
                context = {
                    "username": user["login"],
                    "display_name": user.get("display_name", user["login"]),
                    "url": stream_url,
                    "title": stream.get("title") or "Live on Twitch",
                    "platform": "Twitch",
                    "viewers": stream.get("viewer_count", 0),
                    "everyone": "@everyone",
                    "here": "@here"
                }
                rich_content = self._render_alert_message(alert.get("message_template"), context)

                try:
                    await message.edit(content=rich_content, embed=rich_embed)
                    await self._save_alert_delivery_metadata(
                        alert["_id"],
                        source="eventsub",
                        eventsub_enriched_at=discord.utils.utcnow().timestamp()
                    )
                except discord.Forbidden:
                    logger.warning("Missing permissions to edit provisional Twitch alert message %s", message.id)
                except discord.HTTPException as error:
                    logger.warning(
                        "Failed to enrich provisional Twitch alert for %s: %s",
                        alert["username"],
                        error,
                        exc_info=True
                    )

                await self._save_alert_check_state(
                    alert["_id"],
                    status="sent",
                    error=None,
                    stream_id=stream["id"],
                    stream_title=stream.get("title"),
                    stream_started_at=stream.get("started_at")
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Unhandled error while processing Twitch EventSub live notification for broadcaster %s",
                broadcaster_id
            )
        finally:
            current_task = self._eventsub_live_tasks.get(broadcaster_id)
            if current_task is asyncio.current_task():
                self._eventsub_live_tasks.pop(broadcaster_id, None)

    async def handle_kick_event_request(self, event_type: str, payload: dict) -> None:
        """Process Kick webhook payloads."""
        if event_type not in KICK_EVENT_TYPES:
            return

        broadcaster = payload.get("broadcaster") or {}
        broadcaster_id = broadcaster.get("user_id")
        try:
            broadcaster_id_int = int(broadcaster_id) if broadcaster_id is not None else None
        except (TypeError, ValueError):
            broadcaster_id_int = None
        channel_slug = (broadcaster.get("channel_slug") or "").lower()
        if broadcaster_id_int is None and not channel_slug:
            return

        alerts = await self.db.db.social_alerts.find({
            "platform": "kick",
            "$or": [
                {"kick_broadcaster_id": broadcaster_id_int} if broadcaster_id_int is not None else {"username": channel_slug},
                {"username": channel_slug} if channel_slug else {"kick_broadcaster_id": broadcaster_id_int}
            ]
        }).to_list(length=1000)
        if not alerts:
            return

        is_live = bool(payload.get("is_live"))
        started_at = payload.get("started_at")
        stream_id = f"{broadcaster_id_int or channel_slug}:{started_at or ''}"

        if event_type == "livestream.status.updated" and not is_live:
            for alert in alerts:
                await self._save_alert_check_state(
                    alert["_id"],
                    status="offline",
                    error=None,
                    stream_id=None,
                    stream_title=payload.get("title"),
                    stream_started_at=started_at
                )
                if alert.get("last_content_id") is not None:
                    await self.db.db.social_alerts.update_one({"_id": alert["_id"]}, {"$set": {"last_content_id": None}})
            return

        if event_type == "livestream.metadata.updated":
            return

        channel_data = None
        if channel_slug:
            channel_data, _ = await self._fetch_kick_channel(channel_slug)

        if not channel_data:
            channel_data = {
                "slug": channel_slug or alerts[0]["username"],
                "banner_picture": broadcaster.get("profile_picture"),
                "stream_title": payload.get("title") or "Live on Kick",
                "category": None,
                "broadcaster_user_id": broadcaster_id_int,
                "stream": {
                    "is_live": is_live,
                    "viewer_count": 0,
                    "start_time": started_at,
                    "thumbnail": None,
                }
            }

        for alert in alerts:
            if stream_id == alert.get("last_content_id"):
                await self._save_alert_check_state(
                    alert["_id"],
                    status="already_announced",
                    error=None,
                    stream_id=stream_id,
                    stream_title=channel_data.get("stream_title"),
                    stream_started_at=started_at
                )
                continue

            guild = self.bot.get_guild(alert["guild_id"])
            if not guild:
                continue
            channel = guild.get_channel(alert["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                await self._save_alert_check_state(alert["_id"], status="channel_missing", error="Alert channel not found.")
                continue

            sent = await self._send_kick_alert(alert, channel, channel_data)
            if sent:
                await self._save_alert_check_state(
                    alert["_id"],
                    status="sent",
                    error=None,
                    stream_id=stream_id,
                    stream_title=channel_data.get("stream_title"),
                    stream_started_at=started_at
                )
            else:
                await self._save_alert_check_state(alert["_id"], status="send_failed", error="Discord rejected the alert message.")

    async def handle_youtube_oauth_callback(
        self,
        *,
        state: str,
        code: Optional[str],
        error: Optional[str]
    ) -> tuple[str, str]:
        """Complete the optional YouTube owner OAuth flow for a specific alert."""
        if error:
            return "YouTube OAuth Cancelled", f"Google returned an OAuth error: {error}"

        state_doc = await self._consume_oauth_state(state, "youtube")
        if not state_doc:
            return "YouTube OAuth Failed", "This YouTube OAuth link is missing or has expired. Please generate a new one."

        if not code:
            return "YouTube OAuth Failed", "Google did not return an authorization code."

        client_id, client_secret = self._get_youtube_oauth_credentials()
        redirect_uri = self._get_youtube_oauth_redirect_uri()
        if not client_id or not client_secret or not redirect_uri:
            return "YouTube OAuth Failed", "The bot is missing YouTube OAuth configuration."

        alert_id = state_doc["alert_id"]
        alert = await self.db.db.social_alerts.find_one({"_id": alert_id})
        if not alert:
            return "YouTube OAuth Failed", "The target YouTube alert no longer exists."

        session = await self.get_session()
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri
        }

        try:
            async with session.post("https://oauth2.googleapis.com/token", data=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed YouTube OAuth token exchange: %s %s", response.status, body)
                    return "YouTube OAuth Failed", f"Google token exchange failed with HTTP {response.status}."
                token_data = await response.json()
        except aiohttp.ClientError as token_error:
            logger.error("Error exchanging YouTube OAuth code: %s", token_error, exc_info=True)
            return "YouTube OAuth Failed", "Could not contact Google while exchanging the authorization code."

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        if not access_token or not refresh_token:
            return "YouTube OAuth Failed", "Google did not return the required YouTube OAuth tokens."

        owner_channel, owner_error = await self._fetch_youtube_owned_channel(access_token)
        if not owner_channel:
            return "YouTube OAuth Failed", owner_error or "Could not identify the authenticated YouTube channel."

        owner_channel_id = owner_channel.get("id")
        public_channel, public_error = await self._fetch_youtube_channel(
            alert["username"],
            channel_id=alert.get("youtube_channel_id")
        )
        if not public_channel:
            return "YouTube OAuth Failed", public_error or "Could not resolve the alert's public YouTube channel."

        public_channel_id = public_channel.get("id")
        if owner_channel_id and public_channel_id and owner_channel_id != public_channel_id:
            return (
                "YouTube OAuth Rejected",
                "The authenticated Google account does not own the YouTube channel configured for this alert."
            )

        expires_in = int(token_data.get("expires_in", 3600))
        update_data = {
            "youtube_channel_id": owner_channel_id or public_channel_id,
            "youtube_oauth_channel_id": owner_channel_id,
            "youtube_oauth_channel_title": (owner_channel.get("snippet") or {}).get("title"),
            "youtube_refresh_token": refresh_token,
            "youtube_access_token_expires_at": discord.utils.utcnow().timestamp() + expires_in,
            "youtube_oauth_connected_at": discord.utils.utcnow().timestamp()
        }
        await self.db.db.social_alerts.update_one({"_id": alert["_id"]}, {"$set": update_data})
        return (
            "YouTube OAuth Connected",
            f"The alert for `{alert['username']}` is now linked to the owning YouTube channel and can use the YouTube Live API path."
        )

    async def _send_twitch_alert(
        self,
        alert: dict,
        channel: discord.TextChannel,
        stream: dict,
        user: dict
    ) -> bool:
        """Send a formatted Twitch alert to a channel."""
        stream_url = f"https://twitch.tv/{user['login']}"
        context = {
            "username": user["login"],
            "display_name": user.get("display_name", user["login"]),
            "url": stream_url,
            "title": stream.get("title") or "Live on Twitch",
            "platform": "Twitch",
            "viewers": stream.get("viewer_count", 0),
            "everyone": "@everyone",
            "here": "@here"
        }
        content = self._render_alert_message(alert.get("message_template"), context)
        embed = self._build_twitch_embed(stream, user)

        try:
            await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True)
            )
            return True
        except discord.Forbidden:
            logger.warning("Missing permissions to send Twitch alert in channel %s", channel.id)
            return False
        except discord.HTTPException as error:
            logger.warning(
                "Primary Twitch alert send failed for %s, retrying without preview image: %s",
                alert["username"],
                error,
                exc_info=True
            )

            fallback_embed = self._build_twitch_embed(stream, user)
            fallback_embed.set_image(url=None)

            try:
                await channel.send(
                    content=content,
                    embed=fallback_embed,
                    allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True)
                )
                return True
            except discord.Forbidden:
                logger.warning("Missing permissions to send fallback Twitch alert in channel %s", channel.id)
                return False
            except discord.HTTPException as fallback_error:
                logger.error(
                    "Failed to send fallback Twitch alert for %s: %s",
                    alert["username"],
                    fallback_error,
                    exc_info=True
                )
                return False

    async def _send_twitch_eventsub_provisional_alert(
        self,
        alert: dict,
        channel: discord.TextChannel,
        event: dict,
        user: dict
    ) -> Optional[discord.Message]:
        """Send an immediate provisional Twitch alert from EventSub payload data."""
        login = user.get("login") or event.get("broadcaster_user_login") or alert["username"]
        stream_url = f"https://twitch.tv/{login}"
        context = {
            "username": login,
            "display_name": user.get("display_name") or event.get("broadcaster_user_name") or login,
            "url": stream_url,
            "title": "Live on Twitch",
            "platform": "Twitch",
            "viewers": 0,
            "everyone": "@everyone",
            "here": "@here"
        }
        content = self._render_alert_message(alert.get("message_template"), context)
        embed = self._build_twitch_eventsub_embed(event, user)

        try:
            return await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True)
            )
        except discord.Forbidden:
            logger.warning("Missing permissions to send provisional Twitch alert in channel %s", channel.id)
            return None
        except discord.HTTPException as error:
            logger.error(
                "Failed to send provisional Twitch alert for %s: %s",
                alert["username"],
                error,
                exc_info=True
            )
            return None

    async def _send_kick_alert(
        self,
        alert: dict,
        channel: discord.TextChannel,
        channel_data: dict
    ) -> bool:
        """Send a formatted Kick alert to a channel."""
        slug = channel_data.get("slug") or alert["username"]
        stream = channel_data.get("stream") or {}
        stream_url = f"https://kick.com/{slug}"
        context = {
            "username": slug,
            "display_name": slug,
            "url": stream_url,
            "title": channel_data.get("stream_title") or "Live on Kick",
            "platform": "Kick",
            "viewers": stream.get("viewer_count", 0),
            "everyone": "@everyone",
            "here": "@here"
        }
        content = self._render_alert_message(alert.get("message_template"), context)
        embed = self._build_kick_embed(channel_data)

        try:
            await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True)
            )
            return True
        except discord.Forbidden:
            logger.warning("Missing permissions to send Kick alert in channel %s", channel.id)
            return False
        except discord.HTTPException as error:
            logger.warning("Failed to send Kick alert for %s: %s", alert["username"], error, exc_info=True)
            return False

    async def _send_youtube_alert(
        self,
        alert: dict,
        channel: discord.TextChannel,
        video: dict,
        channel_data: dict
    ) -> bool:
        """Send a formatted YouTube live alert to a channel."""
        snippet = channel_data.get("snippet") or {}
        display_name = snippet.get("title") or alert["username"]
        context = {
            "username": alert["username"],
            "display_name": display_name,
            "url": video.get("url") or f"https://youtube.com/{alert['username']}",
            "title": video.get("title") or "Live on YouTube",
            "platform": "YouTube",
            "viewers": video.get("viewers", 0),
            "everyone": "@everyone",
            "here": "@here"
        }
        content = self._render_alert_message(alert.get("message_template"), context)
        embed = self._build_youtube_embed(video, channel_data)

        try:
            await channel.send(
                content=content,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True)
            )
            return True
        except discord.Forbidden:
            logger.warning("Missing permissions to send YouTube alert in channel %s", channel.id)
            return False
        except discord.HTTPException as error:
            logger.warning("Failed to send YouTube alert for %s: %s", alert["username"], error, exc_info=True)
            return False

    async def _check_twitch_alerts_batch(self, alerts: list[dict]) -> None:
        """Check all Twitch alerts using batched Twitch API requests."""
        usernames = sorted({alert["username"].lower() for alert in alerts})
        users_by_login, user_error = await self._fetch_twitch_users_batch(usernames)
        if user_error:
            for alert in alerts:
                await self._save_alert_check_state(alert["_id"], status="user_lookup_failed", error=user_error)
            return

        user_ids = sorted({user["id"] for user in users_by_login.values()})
        streams_by_user_id, stream_error = await self._fetch_twitch_streams_batch(user_ids)
        if stream_error:
            for alert in alerts:
                await self._save_alert_check_state(alert["_id"], status="stream_lookup_failed", error=stream_error)
            return

        for alert in alerts:
            username = alert["username"].lower()
            user = users_by_login.get(username)
            if not user:
                await self._save_alert_check_state(
                    alert["_id"],
                    status="user_lookup_failed",
                    error=f"No Twitch channel found for `{username}`."
                )
                continue

            stream = streams_by_user_id.get(user["id"])
            if not stream:
                await self._save_alert_check_state(alert["_id"], status="offline", error=None, stream_id=None)
                if alert.get("last_content_id") is not None:
                    await self.db.db.social_alerts.update_one(
                        {"_id": alert["_id"]},
                        {"$set": {"last_content_id": None}}
                    )
                continue

            if self._should_suppress_poll_alert(alert):
                await self._save_alert_check_state(
                    alert["_id"],
                    status="already_announced",
                    error=None,
                    stream_id=stream["id"],
                    stream_title=stream.get("title"),
                    stream_started_at=stream.get("started_at")
                )
                continue

            if stream["id"] == alert.get("last_content_id"):
                await self._save_alert_check_state(
                    alert["_id"],
                    status="already_announced",
                    error=None,
                    stream_id=stream["id"],
                    stream_title=stream.get("title"),
                    stream_started_at=stream.get("started_at")
                )
                continue

            guild = self.bot.get_guild(alert["guild_id"])
            if not guild:
                logger.warning("Guild %s not found for Twitch alert %s", alert["guild_id"], username)
                continue

            channel = guild.get_channel(alert["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                logger.warning("Channel %s not found for Twitch alert %s", alert["channel_id"], username)
                await self._save_alert_check_state(alert["_id"], status="channel_missing", error="Alert channel not found.")
                continue

            sent = await self._send_twitch_alert(alert, channel, stream, user)
            if sent:
                await self._save_alert_delivery_metadata(
                    alert["_id"],
                    source="poll",
                    discord_sent_at=discord.utils.utcnow().timestamp()
                )
                await self._save_alert_check_state(
                    alert["_id"],
                    status="sent",
                    error=None,
                    stream_id=stream["id"],
                    stream_title=stream.get("title"),
                    stream_started_at=stream.get("started_at")
                )
            else:
                await self._save_alert_check_state(alert["_id"], status="send_failed", error="Discord rejected the alert message.")

    async def _check_kick_alerts_batch(self, alerts: list[dict]) -> None:
        """Check all Kick alerts using batched Kick API requests."""
        slugs = sorted({alert["username"].lower() for alert in alerts})
        channels_by_slug, channel_error = await self._fetch_kick_channels_batch(slugs)
        if channel_error:
            for alert in alerts:
                await self._save_alert_check_state(alert["_id"], status="channel_lookup_failed", error=channel_error)
            return

        for alert in alerts:
            slug = alert["username"].lower()
            channel_data = channels_by_slug.get(slug)
            if not channel_data:
                await self._save_alert_check_state(
                    alert["_id"],
                    status="channel_lookup_failed",
                    error=f"No Kick channel found for `{alert['username']}`."
                )
                continue

            stream = channel_data.get("stream") or {}
            if not stream.get("is_live"):
                await self._save_alert_check_state(alert["_id"], status="offline", error=None, stream_id=None)
                if alert.get("last_content_id") is not None:
                    await self.db.db.social_alerts.update_one({"_id": alert["_id"]}, {"$set": {"last_content_id": None}})
                continue

            stream_id = f"{channel_data.get('broadcaster_user_id', slug)}:{stream.get('start_time') or ''}"
            if stream_id == alert.get("last_content_id"):
                await self._save_alert_check_state(
                    alert["_id"],
                    status="already_announced",
                    error=None,
                    stream_id=stream_id,
                    stream_title=channel_data.get("stream_title"),
                    stream_started_at=stream.get("start_time")
                )
                continue

            guild = self.bot.get_guild(alert["guild_id"])
            if not guild:
                logger.warning("Guild %s not found for Kick alert %s", alert["guild_id"], slug)
                continue

            channel = guild.get_channel(alert["channel_id"])
            if not isinstance(channel, discord.TextChannel):
                logger.warning("Channel %s not found for Kick alert %s", alert["channel_id"], slug)
                await self._save_alert_check_state(alert["_id"], status="channel_missing", error="Alert channel not found.")
                continue

            sent = await self._send_kick_alert(alert, channel, channel_data)
            if sent:
                await self._save_alert_delivery_metadata(
                    alert["_id"],
                    source="poll",
                    discord_sent_at=discord.utils.utcnow().timestamp()
                )
                await self._save_alert_check_state(
                    alert["_id"],
                    status="sent",
                    error=None,
                    stream_id=stream_id,
                    stream_title=channel_data.get("stream_title"),
                    stream_started_at=stream.get("start_time")
                )
            else:
                await self._save_alert_check_state(alert["_id"], status="send_failed", error="Discord rejected the alert message.")

    @tasks.loop(seconds=15)
    async def check_alerts_task(self):
        """Check for new content from monitored accounts"""
        try:
            cursor = self.db.db.social_alerts.find({})
            alerts = await cursor.to_list(length=1000)

            twitch_alerts = [alert for alert in alerts if alert.get("platform") == "twitch"]
            if twitch_alerts:
                await self._check_twitch_alerts_batch(twitch_alerts)

            for alert in alerts:
                try:
                    platform = alert["platform"]
                    if platform == "youtube":
                        await self.check_youtube(alert)
                    elif platform == "twitter":
                        await self.check_twitter(alert)
                except Exception as error:
                    logger.error("Error checking alert %s: %s", alert.get("_id"), error, exc_info=True)
        except Exception as error:
            logger.error("Error in social alerts task: %s", error, exc_info=True)

    @check_alerts_task.before_loop
    async def before_check_alerts(self):
        """Wait for bot to be ready"""
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=15)
    async def reconcile_eventsub_task(self):
        """Reconcile platform webhook subscriptions for configured live alerts."""
        try:
            await self._reconcile_twitch_eventsub_subscriptions()
            await self._reconcile_kick_event_subscriptions()
        except Exception as error:
            logger.error("Error reconciling live alert webhook subscriptions: %s", error, exc_info=True)

    @reconcile_eventsub_task.before_loop
    async def before_reconcile_eventsub(self):
        """Wait for bot readiness before managing EventSub."""
        await self.bot.wait_until_ready()

    async def check_twitch(self, alert: dict):
        """Check Twitch for live streams."""
        username = alert["username"]
        user, user_error = await self._fetch_twitch_user(username)
        if not user:
            await self._save_alert_check_state(alert["_id"], status="user_lookup_failed", error=user_error)
            return

        stream, stream_error = await self._fetch_twitch_stream(user["id"])
        if stream_error:
            await self._save_alert_check_state(alert["_id"], status="stream_lookup_failed", error=stream_error)
            return

        if not stream:
            await self._save_alert_check_state(alert["_id"], status="offline", error=None, stream_id=None)
            if alert.get("last_content_id") is not None:
                await self.db.db.social_alerts.update_one(
                    {"_id": alert["_id"]},
                    {"$set": {"last_content_id": None}}
                )
            return

        if self._should_suppress_poll_alert(alert):
            await self._save_alert_check_state(
                alert["_id"],
                status="already_announced",
                error=None,
                stream_id=stream["id"],
                stream_title=stream.get("title"),
                stream_started_at=stream.get("started_at")
            )
            return

        if stream["id"] == alert.get("last_content_id"):
            await self._save_alert_check_state(
                alert["_id"],
                status="already_announced",
                error=None,
                stream_id=stream["id"],
                stream_title=stream.get("title"),
                stream_started_at=stream.get("started_at")
            )
            return

        guild = self.bot.get_guild(alert["guild_id"])
        if not guild:
            logger.warning("Guild %s not found for Twitch alert %s", alert["guild_id"], username)
            return

        channel = guild.get_channel(alert["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Channel %s not found for Twitch alert %s", alert["channel_id"], username)
            await self._save_alert_check_state(alert["_id"], status="channel_missing", error="Alert channel not found.")
            return

        sent = await self._send_twitch_alert(alert, channel, stream, user)
        if sent:
            await self._save_alert_delivery_metadata(
                alert["_id"],
                source="poll",
                discord_sent_at=discord.utils.utcnow().timestamp()
            )
            await self._save_alert_check_state(
                alert["_id"],
                status="sent",
                error=None,
                stream_id=stream["id"],
                stream_title=stream.get("title"),
                stream_started_at=stream.get("started_at")
            )
        else:
            await self._save_alert_check_state(alert["_id"], status="send_failed", error="Discord rejected the alert message.")

    async def check_youtube(self, alert: dict):
        """Check YouTube for active live streams."""
        oauth_enabled = bool(alert.get("youtube_refresh_token"))
        channel_data = None
        channel_error = None
        video = None
        video_error = None
        oauth_error = None

        if oauth_enabled:
            video, owner_channel, oauth_error = await self._fetch_youtube_live_video_via_oauth(alert)
            if not oauth_error:
                channel_data = owner_channel
                resolved_channel_id = owner_channel.get("id") if owner_channel else alert.get("youtube_channel_id")
            else:
                logger.warning(
                    "Falling back to public YouTube polling for alert %s because OAuth path failed: %s",
                    alert.get("_id"),
                    oauth_error
                )

        if not channel_data:
            channel_data, channel_error = await self._fetch_youtube_channel(
                alert["username"],
                channel_id=alert.get("youtube_channel_id")
            )
            if not channel_data:
                fallback_error = channel_error or oauth_error
                await self._save_alert_check_state(alert["_id"], status="channel_lookup_failed", error=fallback_error)
                return

            resolved_channel_id = channel_data.get("id")
            if not resolved_channel_id:
                await self._save_alert_check_state(
                    alert["_id"],
                    status="channel_lookup_failed",
                    error="YouTube did not return a channel ID for this channel."
                )
                return
            if resolved_channel_id and resolved_channel_id != alert.get("youtube_channel_id"):
                await self.db.db.social_alerts.update_one(
                    {"_id": alert["_id"]},
                    {"$set": {"youtube_channel_id": resolved_channel_id}}
                )

            if oauth_error or not oauth_enabled:
                video, video_error = await self._fetch_youtube_live_video(resolved_channel_id)
                if video_error:
                    await self._save_alert_check_state(alert["_id"], status="stream_lookup_failed", error=video_error)
                    return

        if not video:
            await self._save_alert_check_state(alert["_id"], status="offline", error=None, stream_id=None)
            if alert.get("last_content_id") is not None:
                await self.db.db.social_alerts.update_one({"_id": alert["_id"]}, {"$set": {"last_content_id": None}})
            return

        if video["id"] == alert.get("last_content_id"):
            await self._save_alert_check_state(
                alert["_id"],
                status="already_announced",
                error=None,
                stream_id=video["id"],
                stream_title=video.get("title"),
                stream_started_at=video.get("started_at")
            )
            return

        guild = self.bot.get_guild(alert["guild_id"])
        if not guild:
            logger.warning("Guild %s not found for YouTube alert %s", alert["guild_id"], alert["username"])
            return

        channel = guild.get_channel(alert["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Channel %s not found for YouTube alert %s", alert["channel_id"], alert["username"])
            await self._save_alert_check_state(alert["_id"], status="channel_missing", error="Alert channel not found.")
            return

        sent = await self._send_youtube_alert(alert, channel, video, channel_data)
        if sent:
            await self._save_alert_delivery_metadata(
                alert["_id"],
                source="poll",
                discord_sent_at=discord.utils.utcnow().timestamp()
            )
            await self._save_alert_check_state(
                alert["_id"],
                status="sent",
                error=None,
                stream_id=video["id"],
                stream_title=video.get("title"),
                stream_started_at=video.get("started_at")
            )
        else:
            await self._save_alert_check_state(alert["_id"], status="send_failed", error="Discord rejected the alert message.")

    async def check_kick(self, alert: dict):
        """Check Kick for live streams."""
        channel_data, channel_error = await self._fetch_kick_channel(alert["username"])
        if not channel_data:
            await self._save_alert_check_state(alert["_id"], status="channel_lookup_failed", error=channel_error)
            return

        stream = channel_data.get("stream") or {}
        if not stream.get("is_live"):
            await self._save_alert_check_state(alert["_id"], status="offline", error=None, stream_id=None)
            if alert.get("last_content_id") is not None:
                await self.db.db.social_alerts.update_one({"_id": alert["_id"]}, {"$set": {"last_content_id": None}})
            return

        stream_id = f"{channel_data.get('broadcaster_user_id', alert['username'])}:{stream.get('start_time') or ''}"
        if stream_id == alert.get("last_content_id"):
            await self._save_alert_check_state(
                alert["_id"],
                status="already_announced",
                error=None,
                stream_id=stream_id,
                stream_title=channel_data.get("stream_title"),
                stream_started_at=stream.get("start_time")
            )
            return

        guild = self.bot.get_guild(alert["guild_id"])
        if not guild:
            logger.warning("Guild %s not found for Kick alert %s", alert["guild_id"], alert["username"])
            return

        channel = guild.get_channel(alert["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Channel %s not found for Kick alert %s", alert["channel_id"], alert["username"])
            await self._save_alert_check_state(alert["_id"], status="channel_missing", error="Alert channel not found.")
            return

        sent = await self._send_kick_alert(alert, channel, channel_data)
        if sent:
            await self._save_alert_delivery_metadata(
                alert["_id"],
                source="poll",
                discord_sent_at=discord.utils.utcnow().timestamp()
            )
            await self._save_alert_check_state(
                alert["_id"],
                status="sent",
                error=None,
                stream_id=stream_id,
                stream_title=channel_data.get("stream_title"),
                stream_started_at=stream.get("start_time")
            )
        else:
            await self._save_alert_check_state(alert["_id"], status="send_failed", error="Discord rejected the alert message.")

    async def check_twitter(self, alert: dict):
        """Check Twitter/X for new tweets."""
        logger.debug("Checking Twitter for %s", alert["username"])

    async def _run_single_alert_check(self, alert: dict) -> None:
        """Run one alert check immediately."""
        platform = alert["platform"]
        if platform == "twitch":
            await self.check_twitch(alert)
        elif platform == "youtube":
            await self.check_youtube(alert)
        elif platform == "kick":
            await self.check_kick(alert)
        elif platform == "twitter":
            await self.check_twitter(alert)

    @alert_group.command(name="add", description="Add social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/kick/twitter)",
        username="Username or channel ID",
        channel="Channel to send alerts to"
    )
    @is_admin()
    async def add_alert(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str,
        channel: discord.TextChannel
    ):
        """Open the social alert setup wizard."""
        platform = platform.lower()
        if platform not in SUPPORTED_PLATFORMS:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, kick, or twitter"),
                ephemeral=True
            )
            return

        normalized_username = self._normalize_alert_username(platform, username)

        existing = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": normalized_username
        })

        if existing:
            await interaction.response.send_message(
                embed=EmbedFactory.warning("Already Exists", f"Alert for {normalized_username} on {platform} already exists"),
                ephemeral=True
            )
            return

        try:
            modal = SocialAlertTemplateModal(self, platform, normalized_username, channel, mode="create")
            await interaction.response.send_modal(modal)
        except Exception as error:
            logger.error("Failed to open social alert create modal: %s", error, exc_info=True)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Setup Failed",
                    "I couldn't open the alert setup wizard. Please try again."
                ),
                ephemeral=True
            )

    @alert_group.command(name="edit", description="Edit an existing social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/kick/twitter)",
        username="Username or channel ID",
        channel="Optional new alert channel"
    )
    @is_admin()
    async def edit_alert(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str,
        channel: Optional[discord.TextChannel] = None
    ):
        """Open the social alert edit wizard."""
        platform = platform.lower()
        if platform not in SUPPORTED_PLATFORMS:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, kick, or twitter"),
                ephemeral=True
            )
            return

        normalized_username = self._normalize_alert_username(platform, username)

        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": normalized_username
        })
        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {normalized_username} on {platform}"),
                ephemeral=True
            )
            return

        try:
            modal = SocialAlertTemplateModal(
                self,
                platform,
                normalized_username,
                channel,
                mode="edit",
                existing_alert=alert
            )
            await interaction.response.send_modal(modal)
        except Exception as error:
            logger.error("Failed to open social alert edit modal: %s", error, exc_info=True)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Setup Failed",
                    "I couldn't open the alert edit wizard. Please try again."
                ),
                ephemeral=True
            )

    @alert_group.command(name="remove", description="Remove social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/kick/twitter)",
        username="Username or channel ID"
    )
    @is_admin()
    async def remove_alert(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str
    ):
        """Remove social media alert (ADMIN ONLY)"""
        platform = platform.lower()
        if platform not in SUPPORTED_PLATFORMS:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, kick, or twitter"),
                ephemeral=True
            )
            return

        normalized_username = self._normalize_alert_username(platform, username)

        result = await self.db.db.social_alerts.delete_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": normalized_username
        })

        if result.deleted_count == 0:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {normalized_username} on {platform}"),
                ephemeral=True
            )
            return

        embed = EmbedFactory.success(
            "Alert Removed",
            f"Removed {platform} alert for **{normalized_username}**"
        )
        await interaction.response.send_message(embed=embed)
        if platform == "twitch":
            await self._reconcile_twitch_eventsub_subscriptions()
        elif platform == "kick":
            await self._reconcile_kick_event_subscriptions()
        logger.info("%s removed %s alert for %s", interaction.user, platform, normalized_username)

    @alert_group.command(name="list", description="List all social media alerts (Admin)")
    @is_admin()
    async def list_alerts(self, interaction: discord.Interaction):
        """List all social media alerts (ADMIN ONLY)"""
        cursor = self.db.db.social_alerts.find({"guild_id": interaction.guild.id})
        alerts = await cursor.to_list(length=100)

        if not alerts:
            await interaction.response.send_message(
                embed=EmbedFactory.info("No Alerts", "No social media alerts configured"),
                ephemeral=True
            )
            return

        grouped = {"twitch": [], "youtube": [], "kick": [], "twitter": []}
        for alert in alerts:
            platform = alert["platform"]
            if platform in grouped:
                channel = interaction.guild.get_channel(alert["channel_id"])
                suffix = ""
                if alert.get("message_template"):
                    suffix = " | custom message"
                grouped[platform].append(
                    f"• **{alert['username']}** → {channel.mention if channel else 'Unknown'}{suffix}"
                )

        description = ""
        platform_emoji = {
            "twitch": "🟣 **Twitch**",
            "youtube": "🔴 **YouTube**",
            "kick": "🟢 **Kick**",
            "twitter": "🐦 **Twitter/X**"
        }

        for platform, items in grouped.items():
            if items:
                description += f"\n{platform_emoji[platform]}\n"
                description += "\n".join(items) + "\n"

        embed = EmbedFactory.create(
            title="📢 Social Media Alerts",
            description=description or "No alerts configured",
            color=EmbedColor.INFO
        )

        await interaction.response.send_message(embed=embed)

    @alert_group.command(name="test", description="Test social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/kick/twitter)",
        username="Username to test"
    )
    @is_admin()
    async def test_alert(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str
    ):
        """Test a social media alert (ADMIN ONLY)"""
        platform = platform.lower()
        if platform not in SUPPORTED_PLATFORMS:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, kick, or twitter"),
                ephemeral=True
            )
            return

        normalized_username = self._normalize_alert_username(platform, username)

        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": normalized_username
        })

        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {normalized_username} on {platform}"),
                ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(alert["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=EmbedFactory.error("Channel Not Found", "Alert channel no longer exists"),
                ephemeral=True
            )
            return

        if platform == "twitch":
            user, user_error = await self._fetch_twitch_user(normalized_username)
            if not user:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Twitch Lookup Failed", user_error or "I couldn't resolve that Twitch channel."),
                    ephemeral=True
                )
                return

            stream, stream_error = await self._fetch_twitch_stream(user["id"])
            if stream_error:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Twitch Check Failed", stream_error),
                    ephemeral=True
                )
                return

            if not stream:
                stream = {
                    "title": "Offline Preview",
                    "viewer_count": 0,
                    "game_name": "Offline",
                    "thumbnail_url": "https://static-cdn.jtvnw.net/ttv-static/404_preview-1280x720.jpg",
                    "started_at": discord.utils.utcnow().isoformat()
                }

            sent = await self._send_twitch_alert(alert, channel, stream, user)
            if not sent:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Send Failed", "I couldn't send the Twitch test alert to the target channel."),
                    ephemeral=True
                )
                return

            description = f"Twitch test notification sent to {channel.mention}."
            if stream.get("title") == "Offline Preview":
                description += "\n\nThe channel is currently offline, so this used a preview embed with the real profile data."

            await interaction.response.send_message(
                embed=EmbedFactory.success("Test Sent", description),
                ephemeral=True
            )
            return

        if platform == "twitter":
            embed = EmbedFactory.info(
                "Test Not Available",
                "Twitter/X alerts are still stubbed in this build, so there isn't a live test flow yet."
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if platform == "kick":
            channel_data, channel_error = await self._fetch_kick_channel(normalized_username)
            if not channel_data:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Kick Lookup Failed", channel_error or "I couldn't resolve that Kick channel."),
                    ephemeral=True
                )
                return

            live_stream = channel_data.get("stream") or {}
            if not live_stream.get("is_live"):
                channel_data = {
                    **channel_data,
                    "stream_title": channel_data.get("stream_title") or "Offline Preview",
                    "stream": {
                        "is_live": True,
                        "viewer_count": 0,
                        "start_time": discord.utils.utcnow().isoformat(),
                        "thumbnail": None
                    }
                }

            sent = await self._send_kick_alert(alert, channel, channel_data)
            if not sent:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Send Failed", "I couldn't send the Kick test alert to the target channel."),
                    ephemeral=True
                )
                return

            description = f"Kick test notification sent to {channel.mention}."
            if not live_stream.get("is_live"):
                description += "\n\nThe channel is currently offline, so this used a preview embed with the real channel data."

            await interaction.response.send_message(
                embed=EmbedFactory.success("Test Sent", description),
                ephemeral=True
            )
            return

        if platform == "youtube":
            if alert.get("youtube_refresh_token"):
                video, channel_data, video_error = await self._fetch_youtube_live_video_via_oauth(alert)
                channel_error = video_error
                if not channel_data:
                    channel_data, channel_error = await self._fetch_youtube_channel(
                        normalized_username,
                        channel_id=alert.get("youtube_channel_id")
                    )
                    if channel_data:
                        video, video_error = await self._fetch_youtube_live_video(channel_data["id"])
            else:
                channel_data, channel_error = await self._fetch_youtube_channel(
                    normalized_username,
                    channel_id=alert.get("youtube_channel_id")
                )
                if not channel_data:
                    await interaction.response.send_message(
                        embed=EmbedFactory.error("YouTube Lookup Failed", channel_error or "I couldn't resolve that YouTube channel."),
                        ephemeral=True
                    )
                    return
                video, video_error = await self._fetch_youtube_live_video(channel_data["id"])
                channel_error = None

            if not channel_data:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("YouTube Lookup Failed", channel_error or "I couldn't resolve that YouTube channel."),
                    ephemeral=True
                )
                return

            if video_error:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("YouTube Check Failed", video_error),
                    ephemeral=True
                )
                return

            if not video:
                video = {
                    "id": "preview",
                    "title": "Offline Preview",
                    "thumbnail_url": self._get_best_youtube_thumbnail((channel_data.get("snippet") or {}).get("thumbnails")),
                    "started_at": discord.utils.utcnow().isoformat(),
                    "viewers": 0,
                    "url": f"https://youtube.com/channel/{channel_data['id']}"
                }

            sent = await self._send_youtube_alert(alert, channel, video, channel_data)
            if not sent:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Send Failed", "I couldn't send the YouTube test alert to the target channel."),
                    ephemeral=True
                )
                return

            description = f"YouTube test notification sent to {channel.mention}."
            if video["id"] == "preview":
                description += "\n\nThe channel is currently offline, so this used a preview embed with the real channel data."

            await interaction.response.send_message(
                embed=EmbedFactory.success("Test Sent", description),
                ephemeral=True
            )
            return

    @alert_group.command(name="debug", description="Diagnose a social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/kick/twitter)",
        username="Username to diagnose"
    )
    @is_admin()
    async def debug_alert(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str
    ):
        """Diagnose why an alert is or is not firing."""
        platform = platform.lower()
        if platform not in SUPPORTED_PLATFORMS:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, kick, or twitter"),
                ephemeral=True
            )
            return

        normalized_username = self._normalize_alert_username(platform, username)

        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": normalized_username
        })
        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {normalized_username} on {platform}"),
                ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(alert["channel_id"])
        fields = [
            {"name": "Platform", "value": platform, "inline": True},
            {"name": "Username", "value": alert["username"], "inline": True},
            {"name": "Channel", "value": channel.mention if channel else "Missing", "inline": True},
            {"name": "Last Status", "value": alert.get("last_check_status", "Never checked"), "inline": False},
            {"name": "Last Error", "value": alert.get("last_check_error") or "None", "inline": False}
        ]

        if platform == "twitch":
            client_id, client_secret = self._get_twitch_credentials()
            fields.append({
                "name": "Credentials Loaded",
                "value": "Yes" if client_id and client_secret else "No",
                "inline": True
            })
            fields.append({
                "name": "Delivery Mode",
                "value": "EventSub Webhook" if self._eventsub_is_configured() else "Polling",
                "inline": True
            })
            fields.append({
                "name": "Callback URL",
                "value": self._get_twitch_eventsub_callback_url() or "Not configured",
                "inline": False
            })
            user, user_error = await self._fetch_twitch_user(normalized_username)
            if user:
                stream, stream_error = await self._fetch_twitch_stream(user["id"])
                state = "Live" if stream else "Offline"
                if stream_error:
                    state = f"Error: {stream_error}"
                fields.append({"name": "Channel Lookup", "value": user.get("display_name", username), "inline": True})
                fields.append({"name": "Live State", "value": state, "inline": True})
                if self._eventsub_is_configured():
                    status_map, sub_error = await self._get_eventsub_status_for_broadcaster(user["id"])
                    if sub_error:
                        fields.append({"name": "EventSub Status", "value": sub_error, "inline": False})
                    else:
                        online_status = status_map.get("stream.online", alert.get("eventsub_online_status", "missing"))
                        offline_status = status_map.get("stream.offline", alert.get("eventsub_offline_status", "missing"))
                        fields.append({
                            "name": "EventSub Subscriptions",
                            "value": f"stream.online: `{online_status}`\nstream.offline: `{offline_status}`",
                            "inline": False
                        })
                if alert.get("eventsub_last_error"):
                    fields.append({
                        "name": "EventSub Last Error",
                        "value": alert.get("eventsub_last_error"),
                        "inline": False
                    })
                fields.append({
                    "name": "Last Delivery Source",
                    "value": alert.get("last_delivery_source", "Unknown"),
                    "inline": True
                })
                fields.append({
                    "name": "EventSub Received",
                    "value": self._format_debug_timestamp(alert.get("last_eventsub_received_at")),
                    "inline": True
                })
                fields.append({
                    "name": "Discord Sent",
                    "value": self._format_debug_timestamp(alert.get("last_discord_sent_at")),
                    "inline": True
                })
                if alert.get("last_eventsub_notification_type") or alert.get("last_eventsub_enriched_at"):
                    fields.append({
                        "name": "EventSub Delivery",
                        "value": (
                            f"type: {alert.get('last_eventsub_notification_type', 'unknown')}\n"
                            f"enriched: {self._format_debug_timestamp(alert.get('last_eventsub_enriched_at'))}"
                        ),
                        "inline": False
                    })
            else:
                fields.append({"name": "Channel Lookup", "value": user_error or "Failed", "inline": False})
        elif platform == "kick":
            client_id, client_secret = self._get_kick_credentials()
            fields.append({
                "name": "Credentials Loaded",
                "value": "Yes" if client_id and client_secret else "No",
                "inline": True
            })
            fields.append({"name": "Delivery Mode", "value": "Webhook Events", "inline": True})
            fields.append({
                "name": "Callback URL",
                "value": self._get_kick_events_callback_url() or "Not configured",
                "inline": False
            })
            channel_data, channel_error = await self._fetch_kick_channel(normalized_username)
            if channel_data:
                stream = channel_data.get("stream") or {}
                state = "Live" if stream.get("is_live") else "Offline"
                fields.append({"name": "Channel Lookup", "value": channel_data.get("slug", normalized_username), "inline": True})
                fields.append({"name": "Live State", "value": state, "inline": True})
                fields.append({
                    "name": "Broadcaster ID",
                    "value": str(channel_data.get("broadcaster_user_id") or "Unknown"),
                    "inline": True
                })
                fields.append({
                    "name": "Start Time",
                    "value": str(stream.get("start_time") or "None"),
                    "inline": True
                })
                fields.append({
                    "name": "Kick Subscriptions",
                    "value": (
                        f"status: `{alert.get('kick_status_subscription', 'missing')}`\n"
                        f"metadata: `{alert.get('kick_metadata_subscription', 'missing')}`"
                    ),
                    "inline": False
                })
                if alert.get("kick_last_error"):
                    fields.append({
                        "name": "Kick Last Error",
                        "value": alert.get("kick_last_error"),
                        "inline": False
                    })
            else:
                fields.append({"name": "Channel Lookup", "value": channel_error or "Failed", "inline": False})
        elif platform == "youtube":
            api_key = self._get_youtube_api_key()
            fields.append({
                "name": "Credentials Loaded",
                "value": "Yes" if api_key else "No",
                "inline": True
            })
            fields.append({
                "name": "Delivery Mode",
                "value": "OAuth Live API" if alert.get("youtube_refresh_token") else "Public Polling",
                "inline": True
            })
            fields.append({
                "name": "OAuth Connected",
                "value": "Yes" if alert.get("youtube_refresh_token") else "No",
                "inline": True
            })
            if alert.get("youtube_refresh_token"):
                video, channel_data, video_error = await self._fetch_youtube_live_video_via_oauth(alert)
                channel_error = video_error
            else:
                channel_data, channel_error = await self._fetch_youtube_channel(
                    normalized_username,
                    channel_id=alert.get("youtube_channel_id")
                )
                video = None
                video_error = None
            if channel_data:
                if video is None and not alert.get("youtube_refresh_token"):
                    video, video_error = await self._fetch_youtube_live_video(channel_data["id"])
                state = "Live" if video else "Offline"
                if video_error:
                    state = f"Error: {video_error}"
                fields.append({
                    "name": "Channel Lookup",
                    "value": (channel_data.get("snippet") or {}).get("title") or normalized_username,
                    "inline": True
                })
                fields.append({"name": "Live State", "value": state, "inline": True})
                fields.append({"name": "Channel ID", "value": channel_data.get("id", "Unknown"), "inline": False})
                if alert.get("youtube_oauth_channel_title"):
                    fields.append({
                        "name": "OAuth Owner Channel",
                        "value": alert.get("youtube_oauth_channel_title"),
                        "inline": False
                    })
            else:
                fields.append({"name": "Channel Lookup", "value": channel_error or "Failed", "inline": False})
        elif platform == "twitter":
            fields.append({"name": "Implementation", "value": "Twitter/X checks are still stubbed in this build.", "inline": False})

        embed = EmbedFactory.create(
            title="📡 Alert Diagnostics",
            color=EmbedColor.INFO,
            fields=fields
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @alert_group.command(name="run", description="Run a social media alert check immediately (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/kick/twitter)",
        username="Username to check now"
    )
    @is_admin()
    async def run_alert(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str
    ):
        """Force an alert check immediately."""
        platform = platform.lower()
        if platform not in SUPPORTED_PLATFORMS:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, kick, or twitter"),
                ephemeral=True
            )
            return

        normalized_username = self._normalize_alert_username(platform, username)

        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": normalized_username
        })
        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {normalized_username} on {platform}"),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self._run_single_alert_check(alert)

        refreshed = await self.db.db.social_alerts.find_one({"_id": alert["_id"]})
        status = refreshed.get("last_check_status", "unknown")
        error = refreshed.get("last_check_error") or "None"
        embed = EmbedFactory.success(
            "Alert Check Complete",
            f"Ran an immediate check for **{normalized_username}** on **{platform}**.\n\n"
            f"**Last Status:** {status}\n"
            f"**Last Error:** {error}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @alert_group.command(name="youtube-oauth-connect", description="Generate a YouTube owner OAuth link for an alert (Admin)")
    @app_commands.describe(username="YouTube alert username/handle to link to the owning channel")
    @is_admin()
    async def youtube_oauth_connect(
        self,
        interaction: discord.Interaction,
        username: str
    ):
        """Generate a one-time OAuth link that the channel owner can open."""
        if not self._youtube_oauth_is_configured():
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "YouTube OAuth Not Configured",
                    "Set `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET`, and a public HTTPS callback URL first."
                ),
                ephemeral=True
            )
            return

        normalized_username = self._normalize_alert_username("youtube", username)
        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": "youtube",
            "username": normalized_username
        })
        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No YouTube alert found for {normalized_username}."),
                ephemeral=True
            )
            return

        channel_data, channel_error = await self._fetch_youtube_channel(
            normalized_username,
            channel_id=alert.get("youtube_channel_id")
        )
        if not channel_data:
            await interaction.response.send_message(
                embed=EmbedFactory.error("YouTube Lookup Failed", channel_error or "I couldn't resolve that YouTube channel."),
                ephemeral=True
            )
            return

        if channel_data.get("id") and channel_data.get("id") != alert.get("youtube_channel_id"):
            await self.db.db.social_alerts.update_one(
                {"_id": alert["_id"]},
                {"$set": {"youtube_channel_id": channel_data["id"]}}
            )
            alert["youtube_channel_id"] = channel_data["id"]

        state = await self._create_oauth_state(
            platform="youtube",
            alert_id=alert["_id"],
            guild_id=interaction.guild.id,
            username=normalized_username
        )
        client_id, _ = self._get_youtube_oauth_credentials()
        redirect_uri = self._get_youtube_oauth_redirect_uri()
        auth_query = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(YOUTUBE_OAUTH_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        })
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{auth_query}"

        embed = EmbedFactory.info(
            "YouTube OAuth Link Ready",
            "Open this link with the Google account that owns the target YouTube channel:\n\n"
            f"{auth_url}\n\n"
            "Once completed, this alert will keep public polling as fallback but prefer the authenticated YouTube Live API path."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @alert_group.command(name="youtube-oauth-disconnect", description="Disconnect the optional YouTube owner OAuth path from an alert (Admin)")
    @app_commands.describe(username="YouTube alert username/handle to disconnect")
    @is_admin()
    async def youtube_oauth_disconnect(
        self,
        interaction: discord.Interaction,
        username: str
    ):
        """Remove stored YouTube owner OAuth tokens from an alert."""
        normalized_username = self._normalize_alert_username("youtube", username)
        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": "youtube",
            "username": normalized_username
        })
        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No YouTube alert found for {normalized_username}."),
                ephemeral=True
            )
            return

        await self.db.db.social_alerts.update_one(
            {"_id": alert["_id"]},
            {"$unset": {
                "youtube_refresh_token": "",
                "youtube_access_token_expires_at": "",
                "youtube_oauth_channel_id": "",
                "youtube_oauth_channel_title": "",
                "youtube_oauth_connected_at": ""
            }}
        )
        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "YouTube OAuth Disconnected",
                f"Removed the optional owner OAuth path from the YouTube alert for **{normalized_username}**. Public polling remains available."
            ),
            ephemeral=True
        )

    @alert_group.command(name="eventsub-sync", description="Force Twitch EventSub subscription sync (Admin)")
    @app_commands.describe(username="Optional Twitch username to inspect after syncing")
    @is_admin()
    async def alert_eventsub_sync(
        self,
        interaction: discord.Interaction,
        username: Optional[str] = None
    ):
        """Force a Twitch EventSub reconciliation and report the result."""
        await interaction.response.defer(ephemeral=True)

        if not self._eventsub_is_configured():
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "EventSub Not Configured",
                    "Set `PUBLIC_BASE_URL` and `TWITCH_EVENTSUB_SECRET` first."
                ),
                ephemeral=True
            )
            return

        summary = await self._reconcile_twitch_eventsub_subscriptions()
        description = (
            f"**Alerts:** {summary['alerts']}\n"
            f"**Resolved Channels:** {summary['resolved']}\n"
            f"**Created Subscriptions:** {summary['created']}\n"
            f"**Deleted Subscriptions:** {summary['deleted']}\n"
            f"**Healthy Alerts:** {summary['healthy']}\n"
            f"**Alerts Still Missing Subscriptions:** {summary['missing']}\n"
            f"**Failures:** {summary['failed']}"
        )

        if username:
            normalized_username = self._normalize_alert_username("twitch", username)
            alert = await self.db.db.social_alerts.find_one({
                "guild_id": interaction.guild.id,
                "platform": "twitch",
                "username": normalized_username
            })
            if alert:
                description += (
                    f"\n\n**{normalized_username} EventSub Status**\n"
                    f"stream.online: `{alert.get('eventsub_online_status', 'missing')}`\n"
                    f"stream.offline: `{alert.get('eventsub_offline_status', 'missing')}`\n"
                    f"error: {alert.get('eventsub_last_error') or 'None'}"
                )

        embed_factory = EmbedFactory.success if summary["failed"] == 0 and summary["missing"] == 0 else EmbedFactory.warning
        await interaction.followup.send(
            embed=embed_factory("EventSub Sync Complete", description),
            ephemeral=True
        )

    @alert_group.command(name="kick-sync", description="Force Kick webhook subscription sync (Admin)")
    @app_commands.describe(username="Optional Kick username to inspect after syncing")
    @is_admin()
    async def kick_sync(
        self,
        interaction: discord.Interaction,
        username: Optional[str] = None
    ):
        """Force a Kick webhook reconciliation and report the result."""
        await interaction.response.defer(ephemeral=True)

        if not self._kick_events_are_configured():
            await interaction.followup.send(
                embed=EmbedFactory.error(
                    "Kick Webhooks Not Configured",
                    "Set `KICK_CLIENT_ID`, `KICK_CLIENT_SECRET`, and `PUBLIC_BASE_URL` first."
                ),
                ephemeral=True
            )
            return

        summary = await self._reconcile_kick_event_subscriptions()
        description = (
            f"**Alerts:** {summary['alerts']}\n"
            f"**Resolved Channels:** {summary['resolved']}\n"
            f"**Created Subscriptions:** {summary['created']}\n"
            f"**Deleted Subscriptions:** {summary['deleted']}\n"
            f"**Healthy Alerts:** {summary['healthy']}\n"
            f"**Alerts Still Missing Subscriptions:** {summary['missing']}\n"
            f"**Failures:** {summary['failed']}"
        )

        if username:
            normalized_username = self._normalize_alert_username("kick", username)
            alert = await self.db.db.social_alerts.find_one({
                "guild_id": interaction.guild.id,
                "platform": "kick",
                "username": normalized_username
            })
            if alert:
                description += (
                    f"\n\n**{normalized_username} Kick Status**\n"
                    f"status: `{alert.get('kick_status_subscription', 'missing')}`\n"
                    f"metadata: `{alert.get('kick_metadata_subscription', 'missing')}`\n"
                    f"error: {alert.get('kick_last_error') or 'None'}"
                )

        embed_factory = EmbedFactory.success if summary["failed"] == 0 and summary["missing"] == 0 else EmbedFactory.warning
        await interaction.followup.send(
            embed=embed_factory("Kick Sync Complete", description),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(SocialAlerts(bot, bot.db, bot.config))
