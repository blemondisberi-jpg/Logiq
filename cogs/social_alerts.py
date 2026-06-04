"""
Social Alerts Cog for Logiq
Monitor Twitch, YouTube, Twitter/X for new content
"""

import asyncio
import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.db_manager import DatabaseManager
from utils.embeds import EmbedColor, EmbedFactory
from utils.permissions import is_admin

logger = logging.getLogger(__name__)

SUPPORTED_PLATFORMS = ["twitch", "youtube", "twitter"]
DEFAULT_ALERT_TEMPLATE = (
    "Hey @everyone, **{display_name}** is now live on {url}! Go check it out!"
)
TWITCH_EMBED_COLOR = 0x9146FF
TWITCH_EVENTSUB_PATH = "/webhooks/twitch/eventsub"
TWITCH_EVENTSUB_TYPES = ("stream.online", "stream.offline")
TWITCH_EVENTSUB_RECREATE_STATUSES = {
    "webhook_callback_verification_failed",
    "notification_failures_exceeded",
    "authorization_revoked",
    "user_removed",
    "version_removed",
}


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
        self.check_alerts_task.start()
        self.reconcile_eventsub_task.start()

    def cog_unload(self):
        """Cleanup on cog unload"""
        self.check_alerts_task.cancel()
        self.reconcile_eventsub_task.cancel()
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

    def _get_twitch_eventsub_secret(self) -> Optional[str]:
        """Load the EventSub webhook secret."""
        return os.getenv("TWITCH_EVENTSUB_SECRET") or self.config.get("api_keys", {}).get("twitch_eventsub_secret")

    def _get_twitch_eventsub_callback_url(self) -> Optional[str]:
        """Build the public callback URL Twitch should call."""
        base_url = (
            os.getenv("TWITCH_EVENTSUB_CALLBACK_URL")
            or os.getenv("PUBLIC_BASE_URL")
            or self.config.get("web", {}).get("public_url")
        )
        if not base_url:
            return None
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith("https://"):
            return None
        return base_url + TWITCH_EVENTSUB_PATH

    def _eventsub_is_configured(self) -> bool:
        """Whether this deployment is configured for Twitch EventSub webhooks."""
        return bool(self._get_twitch_eventsub_secret() and self._get_twitch_eventsub_callback_url())

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

    async def _create_alert_record(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str,
        channel: discord.TextChannel,
        message_template: Optional[str]
    ) -> None:
        """Create an alert record and confirm success."""
        alert_data = {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "platform": platform,
            "username": username.lower(),
            "message_template": message_template,
            "last_check": None,
            "last_content_id": None
        }

        result = await self.db.db.social_alerts.insert_one(alert_data)
        alert_data["_id"] = result.inserted_id

        platform_emoji = {
            "twitch": "🟣",
            "youtube": "🔴",
            "twitter": "🐦"
        }
        template_preview = self._format_alert_preview(message_template)

        embed = EmbedFactory.success(
            "Alert Added",
            f"{platform_emoji.get(platform, '📢')} **{platform.title()}** alert added!\n\n"
            f"**Username:** {username}\n"
            f"**Channel:** {channel.mention}\n"
            f"**Custom Message:**\n{template_preview}\n\n"
            f"You'll be notified when {username} {'goes live' if platform == 'twitch' else 'posts new content'}!"
        )
        if platform == "twitch":
            await self._reconcile_twitch_eventsub_subscriptions()
            await self.check_twitch(alert_data)
            embed.add_field(
                name="Initial Check",
                value="Ran an immediate Twitch status check for this alert.",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logger.info("%s added %s alert for %s", interaction.user, platform, username)

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
        logger.info("%s updated %s alert for %s", interaction.user, platform, username)

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
            for alert in alerts:
                await self._save_alert_check_state(alert["_id"], status="offline", error=None, stream_id=None)
                if alert.get("last_content_id") is not None:
                    await self.db.db.social_alerts.update_one(
                        {"_id": alert["_id"]},
                        {"$set": {"last_content_id": None}}
                    )
            return None

        user_login = event.get("broadcaster_user_login")
        if user_login:
            user = None
            user_error = None
            cached = self._twitch_user_cache.get(user_login.lower())
            if cached and cached.get("expires_at") and discord.utils.utcnow() < cached["expires_at"]:
                user = cached["user"]
            else:
                user, user_error = await self._fetch_twitch_user(user_login.lower())
            if not user:
                logger.warning("EventSub received live event but Twitch user lookup failed for %s: %s", user_login, user_error)
                return None
        else:
            streamless_user = {"id": broadcaster_id, "login": "", "display_name": event.get("broadcaster_user_name") or "Unknown"}
            user = streamless_user

        stream, stream_error = await self._fetch_twitch_stream(broadcaster_id)
        if stream_error or not stream:
            logger.warning("EventSub received live event but stream fetch failed for broadcaster %s: %s", broadcaster_id, stream_error or "No stream returned")
            return None

        for alert in alerts:
            guild = self.bot.get_guild(alert["guild_id"])
            if not guild:
                continue
            channel = guild.get_channel(alert["channel_id"])
            if not isinstance(channel, discord.TextChannel):
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

            sent = await self._send_twitch_alert(alert, channel, stream, user)
            if sent:
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

        return None

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
        """Reconcile Twitch EventSub subscriptions for configured Twitch alerts."""
        try:
            await self._reconcile_twitch_eventsub_subscriptions()
        except Exception as error:
            logger.error("Error reconciling Twitch EventSub subscriptions: %s", error, exc_info=True)

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
        """Check YouTube for new videos."""
        logger.debug("Checking YouTube for %s", alert["username"])

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
        elif platform == "twitter":
            await self.check_twitter(alert)

    @app_commands.command(name="alert-add", description="Add social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/twitter)",
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
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, or twitter"),
                ephemeral=True
            )
            return

        existing = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": username.lower()
        })

        if existing:
            await interaction.response.send_message(
                embed=EmbedFactory.warning("Already Exists", f"Alert for {username} on {platform} already exists"),
                ephemeral=True
            )
            return

        try:
            modal = SocialAlertTemplateModal(self, platform, username, channel, mode="create")
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

    @app_commands.command(name="alert-edit", description="Edit an existing social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/twitter)",
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
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, or twitter"),
                ephemeral=True
            )
            return

        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": username.lower()
        })
        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {username} on {platform}"),
                ephemeral=True
            )
            return

        try:
            modal = SocialAlertTemplateModal(
                self,
                platform,
                username,
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

    @app_commands.command(name="alert-remove", description="Remove social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/twitter)",
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
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, or twitter"),
                ephemeral=True
            )
            return

        result = await self.db.db.social_alerts.delete_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": username.lower()
        })

        if result.deleted_count == 0:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {username} on {platform}"),
                ephemeral=True
            )
            return

        embed = EmbedFactory.success(
            "Alert Removed",
            f"Removed {platform} alert for **{username}**"
        )
        await interaction.response.send_message(embed=embed)
        if platform == "twitch":
            await self._reconcile_twitch_eventsub_subscriptions()
        logger.info("%s removed %s alert for %s", interaction.user, platform, username)

    @app_commands.command(name="alert-list", description="List all social media alerts (Admin)")
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

        grouped = {"twitch": [], "youtube": [], "twitter": []}
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

    @app_commands.command(name="alert-test", description="Test social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/twitter)",
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
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, or twitter"),
                ephemeral=True
            )
            return

        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": username.lower()
        })

        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {username} on {platform}"),
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
            user, user_error = await self._fetch_twitch_user(username.lower())
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

    @app_commands.command(name="alert-debug", description="Diagnose a social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/twitter)",
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
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, or twitter"),
                ephemeral=True
            )
            return

        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": username.lower()
        })
        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {username} on {platform}"),
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
            user, user_error = await self._fetch_twitch_user(username.lower())
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
            else:
                fields.append({"name": "Channel Lookup", "value": user_error or "Failed", "inline": False})

        embed = EmbedFactory.create(
            title="📡 Alert Diagnostics",
            color=EmbedColor.INFO,
            fields=fields
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="alert-run", description="Run a social media alert check immediately (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/twitter)",
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
                embed=EmbedFactory.error("Invalid Platform", "Platform must be twitch, youtube, or twitter"),
                ephemeral=True
            )
            return

        alert = await self.db.db.social_alerts.find_one({
            "guild_id": interaction.guild.id,
            "platform": platform,
            "username": username.lower()
        })
        if not alert:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Not Found", f"No alert found for {username} on {platform}"),
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
            f"Ran an immediate check for **{username}** on **{platform}**.\n\n"
            f"**Last Status:** {status}\n"
            f"**Last Error:** {error}"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="alert-eventsub-sync", description="Force Twitch EventSub subscription sync (Admin)")
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
            alert = await self.db.db.social_alerts.find_one({
                "guild_id": interaction.guild.id,
                "platform": "twitch",
                "username": username.lower()
            })
            if alert:
                description += (
                    f"\n\n**{username} EventSub Status**\n"
                    f"stream.online: `{alert.get('eventsub_online_status', 'missing')}`\n"
                    f"stream.offline: `{alert.get('eventsub_offline_status', 'missing')}`\n"
                    f"error: {alert.get('eventsub_last_error') or 'None'}"
                )

        embed_factory = EmbedFactory.success if summary["failed"] == 0 and summary["missing"] == 0 else EmbedFactory.warning
        await interaction.followup.send(
            embed=embed_factory("EventSub Sync Complete", description),
            ephemeral=True
        )

        platform_data = {
            "youtube": {
                "title": "🔴 New YouTube Video!",
                "description": f"**{username}** uploaded a new video!\n\n**Title:** Test Video\n\n[Watch Now](https://youtube.com/@{username})",
                "color": 0xFF0000
            },
            "twitter": {
                "title": "🐦 New Tweet!",
                "description": f"**{username}** posted a new tweet!\n\n*This is a test tweet notification*\n\n[View Tweet](https://twitter.com/{username})",
                "color": 0x1DA1F2
            }
        }

        data = platform_data[platform]
        embed = EmbedFactory.create(
            title=data["title"],
            description=data["description"],
            color=data["color"]
        )
        embed.set_footer(text="This is a test notification")

        context = {
            "username": username.lower(),
            "display_name": username,
            "url": f"https://{platform}.com/{username}",
            "title": "Test Content",
            "platform": platform.title(),
            "viewers": 0,
            "everyone": "@everyone",
            "here": "@here"
        }
        content = self._render_alert_message(alert.get("message_template"), context)

        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True)
        )
        await interaction.response.send_message(
            embed=EmbedFactory.success("Test Sent", f"Test notification sent to {channel.mention}"),
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(SocialAlerts(bot, bot.db, bot.config))
