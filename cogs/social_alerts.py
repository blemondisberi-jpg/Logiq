"""
Social Alerts Cog for Logiq
Monitor Twitch, YouTube, Twitter/X for new content
"""

import asyncio
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

    def _build_twitch_embed(self, stream: dict, user: dict) -> discord.Embed:
        """Build a rich Twitch live embed."""
        stream_url = f"https://twitch.tv/{user['login']}"
        preview_url = stream.get("thumbnail_url", "")
        if preview_url:
            preview_url = preview_url.replace("{width}", "1280").replace("{height}", "720")

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

    async def _get_twitch_access_token(self) -> Optional[str]:
        """Get or refresh the Twitch app access token."""
        if self._twitch_access_token and self._twitch_token_expires_at:
            if discord.utils.utcnow() < self._twitch_token_expires_at:
                return self._twitch_access_token

        client_id, client_secret = self._get_twitch_credentials()
        if not client_id or not client_secret:
            logger.warning("Twitch alerts are configured but TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET are missing")
            return None

        session = await self.get_session()
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials"
        }

        try:
            async with session.post("https://id.twitch.tv/oauth2/token", params=payload) as response:
                if response.status != 200:
                    body = await response.text()
                    logger.error("Failed to get Twitch access token: %s %s", response.status, body)
                    return None
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Failed to contact Twitch for access token: %s", error, exc_info=True)
            return None

        self._twitch_access_token = data["access_token"]
        expires_in = max(int(data.get("expires_in", 0)) - 60, 60)
        self._twitch_token_expires_at = discord.utils.utcnow() + timedelta(seconds=expires_in)
        return self._twitch_access_token

    async def _fetch_twitch_user(self, username: str) -> Optional[dict]:
        """Fetch Twitch user metadata."""
        token = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not token or not client_id:
            return None

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
                    return None
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching Twitch user %s: %s", username, error, exc_info=True)
            return None

        users = data.get("data", [])
        return users[0] if users else None

    async def _fetch_twitch_stream(self, user_id: str) -> Optional[dict]:
        """Fetch current live Twitch stream for a user ID."""
        token = await self._get_twitch_access_token()
        client_id, _ = self._get_twitch_credentials()
        if not token or not client_id:
            return None

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
                    return None
                data = await response.json()
        except aiohttp.ClientError as error:
            logger.error("Error fetching Twitch stream for %s: %s", user_id, error, exc_info=True)
            return None

        streams = data.get("data", [])
        return streams[0] if streams else None

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
        user = await self._fetch_twitch_user(username)
        if not user:
            return

        stream = await self._fetch_twitch_stream(user["id"])
        if not stream:
            if alert.get("last_content_id") is not None:
                await self.db.db.social_alerts.update_one(
                    {"_id": alert["_id"]},
                    {"$set": {"last_content_id": None, "last_check": discord.utils.utcnow().timestamp()}}
                )
            return

        if stream["id"] == alert.get("last_content_id"):
            await self.db.db.social_alerts.update_one(
                {"_id": alert["_id"]},
                {"$set": {"last_check": discord.utils.utcnow().timestamp()}}
            )
            return

        guild = self.bot.get_guild(alert["guild_id"])
        if not guild:
            logger.warning("Guild %s not found for Twitch alert %s", alert["guild_id"], username)
            return

        channel = guild.get_channel(alert["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Channel %s not found for Twitch alert %s", alert["channel_id"], username)
            return

        sent = await self._send_twitch_alert(alert, channel, stream, user)
        if sent:
            await self.db.db.social_alerts.update_one(
                {"_id": alert["_id"]},
                {
                    "$set": {
                        "last_content_id": stream["id"],
                        "last_check": discord.utils.utcnow().timestamp(),
                        "last_stream_title": stream.get("title"),
                        "last_stream_started_at": stream.get("started_at")
                    }
                }
            )

    async def check_youtube(self, alert: dict):
        """Check YouTube for new videos."""
        logger.debug("Checking YouTube for %s", alert["username"])

    async def check_twitter(self, alert: dict):
        """Check Twitter/X for new tweets."""
        logger.debug("Checking Twitter for %s", alert["username"])

    @app_commands.command(name="alert-add", description="Add social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/twitter)",
        username="Username or channel ID",
        channel="Channel to send alerts to",
        message_template="Optional custom message. Use {display_name}, {url}, {title}, {username}, {platform}, {viewers}, {everyone}, {here}"
    )
    @is_admin()
    async def add_alert(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str,
        channel: discord.TextChannel,
        message_template: Optional[str] = None
    ):
        """Add social media alert (ADMIN ONLY)"""
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

        alert_data = {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "platform": platform,
            "username": username.lower(),
            "message_template": message_template,
            "last_check": None,
            "last_content_id": None
        }

        await self.db.db.social_alerts.insert_one(alert_data)

        platform_emoji = {
            "twitch": "🟣",
            "youtube": "🔴",
            "twitter": "🐦"
        }
        template_preview = message_template or DEFAULT_ALERT_TEMPLATE

        embed = EmbedFactory.success(
            "Alert Added",
            f"{platform_emoji.get(platform, '📢')} **{platform.title()}** alert added!\n\n"
            f"**Username:** {username}\n"
            f"**Channel:** {channel.mention}\n"
            f"**Custom Message:**\n{template_preview}\n\n"
            f"You'll be notified when {username} {'goes live' if platform == 'twitch' else 'posts new content'}!"
        )
        await interaction.response.send_message(embed=embed)
        logger.info("%s added %s alert for %s", interaction.user, platform, username)

    @app_commands.command(name="alert-edit", description="Edit an existing social media alert (Admin)")
    @app_commands.describe(
        platform="Platform (twitch/youtube/twitter)",
        username="Username or channel ID",
        channel="Optional new alert channel",
        message_template="Optional new custom message. Use {display_name}, {url}, {title}, {username}, {platform}, {viewers}, {everyone}, {here}"
    )
    @is_admin()
    async def edit_alert(
        self,
        interaction: discord.Interaction,
        platform: str,
        username: str,
        channel: Optional[discord.TextChannel] = None,
        message_template: Optional[str] = None
    ):
        """Edit an existing social media alert."""
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

        if channel is None and message_template is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Nothing To Update", "Provide a new channel, a new message template, or both."),
                ephemeral=True
            )
            return

        update_data = {}
        if channel is not None:
            update_data["channel_id"] = channel.id
        if message_template is not None:
            update_data["message_template"] = message_template

        await self.db.db.social_alerts.update_one({"_id": alert["_id"]}, {"$set": update_data})

        updated_channel = channel or interaction.guild.get_channel(alert["channel_id"])
        updated_template = message_template if message_template is not None else alert.get("message_template") or DEFAULT_ALERT_TEMPLATE
        embed = EmbedFactory.success(
            "Alert Updated",
            f"Updated alert for **{username}** on **{platform}**.\n\n"
            f"**Channel:** {updated_channel.mention if updated_channel else 'Unknown'}\n"
            f"**Custom Message:**\n{updated_template}"
        )
        await interaction.response.send_message(embed=embed)
        logger.info("%s updated %s alert for %s", interaction.user, platform, username)

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
            user = await self._fetch_twitch_user(username.lower())
            if user:
                stream = await self._fetch_twitch_stream(user["id"])
            else:
                stream = None

            if not user:
                user = {
                    "login": username.lower(),
                    "display_name": username,
                    "profile_image_url": None
                }
            if not stream:
                stream = {
                    "title": "Test Stream Title",
                    "viewer_count": 73,
                    "game_name": "Just Chatting",
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

            await interaction.response.send_message(
                embed=EmbedFactory.success("Test Sent", f"Twitch test notification sent to {channel.mention}"),
                ephemeral=True
            )
            return

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
