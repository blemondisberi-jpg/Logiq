"""
Social Alerts Cog for Logiq
Monitor Twitch, YouTube, Twitter/X for new content
"""

import asyncio
import hashlib
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
        self.check_alerts_task.start()

    def cog_unload(self):
        """Cleanup on cog unload"""
        self.check_alerts_task.cancel()
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
            str(stream.get("title") or ""),
            str(stream.get("viewer_count") or 0)
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
        return users[0], None

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
            logger.error("Failed to send Twitch alert for %s: %s", alert["username"], error, exc_info=True)
            return False

    @tasks.loop(minutes=5)
    async def check_alerts_task(self):
        """Check for new content from monitored accounts"""
        try:
            cursor = self.db.db.social_alerts.find({})
            alerts = await cursor.to_list(length=1000)

            for alert in alerts:
                try:
                    platform = alert["platform"]
                    if platform == "twitch":
                        await self.check_twitch(alert)
                    elif platform == "youtube":
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
            user, user_error = await self._fetch_twitch_user(username.lower())
            if user:
                stream, stream_error = await self._fetch_twitch_stream(user["id"])
                state = "Live" if stream else "Offline"
                if stream_error:
                    state = f"Error: {stream_error}"
                fields.append({"name": "Channel Lookup", "value": user.get("display_name", username), "inline": True})
                fields.append({"name": "Live State", "value": state, "inline": True})
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
