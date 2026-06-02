"""
Verification Cog for Logiq
Handles user verification with multiple methods
"""

import discord
from discord import app_commands
from discord.ext import commands
import random
import string
from typing import Optional
import logging
from io import BytesIO

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

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


class VerificationSetupModal(discord.ui.Modal, title="Verification Setup"):
    """Modal for setting up verification with welcome message"""

    welcome_message = discord.ui.TextInput(
        label="Welcome Message",
        placeholder="Use {username} for name, {user} for @mention. Type channel names like: verify-channel",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )

    def __init__(self, cog, role, welcome_channel, method, verify_channel, verification_type):
        super().__init__()
        self.cog = cog
        self.role = role
        self.welcome_channel = welcome_channel
        self.method = method
        self.verify_channel = verify_channel
        self.verification_type = verification_type

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        guild_config = await self.cog.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.cog.db.create_guild(interaction.guild.id)

        update_data = {
            'verified_role': self.role.id,
            'welcome_channel': self.welcome_channel.id,
            'verification_enabled': True,
            'verification_type': self.verification_type,
            'verification_method': self.method,
            'welcome_message': self.welcome_message.value
        }
        
        if self.method == 'channel' and self.verify_channel:
            update_data['verify_channel'] = self.verify_channel.id

        await self.cog.db.update_guild(interaction.guild.id, update_data)

        if self.method == 'channel':
            method_text = f"**Verification Channel:** {self.verify_channel.mention}"
            location_text = f"in {self.verify_channel.mention}"
        else:
            method_text = "**Method:** DM (Private Messages)"
            location_text = "via DM"
        
        embed = EmbedFactory.success(
            "✅ Verification Setup Complete",
            f"**Verified Role:** {self.role.mention}\n"
            f"**Welcome Channel:** {self.welcome_channel.mention}\n"
            f"{method_text}\n"
            f"**Type:** {self.verification_type}\n"
            f"**Welcome Message:** {self.welcome_message.value[:100]}...\n\n"
            f"New members will receive a welcome message in {self.welcome_channel.mention} and verification will be sent {location_text}."
        )
        await interaction.response.send_message(embed=embed)
        logger.info(f"Verification setup completed in {interaction.guild} with method: {self.method}")

logger = logging.getLogger(__name__)


class VerificationButton(discord.ui.View):
    """Button-based verification view"""

    def __init__(self, cog: 'Verification'):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.green, custom_id="verify_button", emoji="✅")
    async def verify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Handle verification button click"""
        await self.cog.verify_user(interaction)


class CaptchaModal(discord.ui.Modal, title="Verification Captcha"):
    """Captcha verification modal"""

    def __init__(self, correct_code: str, cog: 'Verification'):
        super().__init__()
        self.correct_code = correct_code
        self.cog = cog

    captcha_code = discord.ui.TextInput(
        label="Enter the code shown",
        placeholder="Enter captcha code",
        required=True,
        max_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        """Handle captcha submission"""
        if self.captcha_code.value.upper() == self.correct_code:
            await self.cog.verify_user(interaction)
        else:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Verification Failed", "Incorrect captcha code. Please try again."),
                ephemeral=True
            )


class Verification(commands.Cog):
    """Verification system cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('verification', {})
        self.session = None

    def cog_unload(self):
        """Cleanup resources on cog unload."""
        if self.session and not self.session.closed:
            self.bot.loop.create_task(self.session.close())

    async def get_session(self):
        """Get or create an aiohttp session."""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def _render_text_template(self, template: Optional[str], context: dict, fallback: str) -> str:
        """Render a text template safely using known placeholders."""
        source = template or fallback
        try:
            return source.format(**context)
        except KeyError as error:
            missing = error.args[0]
            logger.warning("Welcome template missing placeholder %s, falling back to default", missing)
            return fallback.format(**context)

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

    def _load_font(self, size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        """Load a font available in common Linux/macOS deploy environments."""
        candidates = []
        if bold:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/Library/Fonts/Arial Bold.ttf"
            ])
        else:
            candidates.extend([
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/Library/Fonts/Arial.ttf"
            ])

        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
        return ImageFont.load_default()

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

    async def _build_welcome_card(self, member: discord.Member, guild_config: dict) -> Optional[discord.File]:
        """Generate a welcome card image for a joining member."""
        width, height = DEFAULT_WELCOME_CARD_SIZE
        accent = self._parse_hex_color(guild_config.get("welcome_card_accent_color"), DEFAULT_WELCOME_CARD_ACCENT)
        text_color = self._parse_hex_color(guild_config.get("welcome_card_text_color"), DEFAULT_WELCOME_CARD_TEXT)
        background_url = guild_config.get("welcome_card_background_url")

        if background_url:
            background = await self._fetch_url_image(background_url)
            if background is None:
                background = Image.new("RGBA", (width, height), (24, 24, 31, 255))
            else:
                background = ImageOps.fit(background, (width, height), method=Image.Resampling.LANCZOS)
        else:
            background = Image.new("RGBA", (width, height), (24, 24, 31, 255))

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

        member_count = member.guild.member_count or len(member.guild.members)
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
        subtitle_text = self._render_text_template(
            guild_config.get("welcome_card_subtitle"),
            context,
            DEFAULT_WELCOME_CARD_SUBTITLE
        )

        draw = ImageDraw.Draw(canvas)
        title_font = self._load_font(56, bold=True)
        subtitle_font = self._load_font(34, bold=True)

        title_bbox = draw.textbbox((0, 0), title_text, font=title_font)
        subtitle_bbox = draw.textbbox((0, 0), subtitle_text, font=subtitle_font)
        title_width = title_bbox[2] - title_bbox[0]
        subtitle_width = subtitle_bbox[2] - subtitle_bbox[0]

        title_position = ((width - title_width) / 2, 318)
        subtitle_position = ((width - subtitle_width) / 2, 392)
        shadow = (0, 0, 0, 130)
        draw.text((title_position[0] + 2, title_position[1] + 2), title_text, font=title_font, fill=shadow)
        draw.text(title_position, title_text, font=title_font, fill=text_color)
        draw.text((subtitle_position[0] + 2, subtitle_position[1] + 2), subtitle_text, font=subtitle_font, fill=shadow)
        draw.text(subtitle_position, subtitle_text, font=subtitle_font, fill=text_color)

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

        verified_role_id = guild_config.get('verified_role')
        verification_type = guild_config.get('verification_type', 'button')
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

        welcome_context = {
            "user": member.mention,
            "username": member.name,
            "display_name": member.display_name,
            "server": member.guild.name,
            "member_count": member.guild.member_count or len(member.guild.members)
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
                        welcome_file = await self._build_welcome_card(member, guild_config)
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
                        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                        
                        # Create a view with button that shows code only to the user
                        class CaptchaView(discord.ui.View):
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
                                    view=CaptchaEntryView(self.user_id, self.code, self.cog)
                                )
                        
                        class CaptchaEntryView(discord.ui.View):
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
                        
                        view = CaptchaView(member.id, code, self)
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
                code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                embed = EmbedFactory.create(
                    title=f"🔐 Welcome to {member.guild.name}",
                    description=f"{welcome_message}\n\n**Your verification code:** `{code}`\n\nClick the button below and enter this code.",
                    color=EmbedColor.PRIMARY
                )
                embed.set_thumbnail(url=member.guild.icon.url if member.guild.icon else None)

                button = discord.ui.Button(label="Enter Code", style=discord.ButtonStyle.green, custom_id=f"captcha_{member.id}")

                async def captcha_callback(interaction: discord.Interaction):
                    if interaction.user.id != member.id:
                        await interaction.response.send_message("This verification is not for you!", ephemeral=True)
                        return
                    modal = CaptchaModal(code, self)
                    await interaction.response.send_modal(modal)

                button.callback = captcha_callback
                view = discord.ui.View(timeout=None)
                view.add_item(button)

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

    async def verify_user(self, interaction: discord.Interaction):
        """Verify a user and assign role (SILENT - no public announcements)"""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "Server not configured"),
                ephemeral=True
            )
            return

        if guild_config.get("verification_enabled", True) is False:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Verification Disabled", "Verification is currently disabled for this server."),
                ephemeral=True
            )
            return

        verified_role_id = guild_config.get('verified_role')
        if not verified_role_id:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "Verified role not configured"),
                ephemeral=True
            )
            return

        verified_role = interaction.guild.get_role(verified_role_id)
        if not verified_role:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "Verified role not found"),
                ephemeral=True
            )
            return

        if verified_role in interaction.user.roles:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Already Verified", "You are already verified!"),
                ephemeral=True
            )
            return

        try:
            # Silently add verified role
            await interaction.user.add_roles(verified_role)

            # Send private success message
            await interaction.response.send_message(
                embed=EmbedFactory.success(
                    "✅ Verified Successfully!",
                    f"Welcome to **{interaction.guild.name}**!\n\nYou now have access to all channels."
                ),
                ephemeral=True
            )

            # Log silently (no public announcement)
            logger.info(f"Verified user {interaction.user} in {interaction.guild} (silent)")

        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to assign roles"),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error verifying user: {e}", exc_info=True)
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "An error occurred during verification"),
                ephemeral=True
            )

    @app_commands.command(name="setup-verification", description="Setup verification system (Admin)")
    @app_commands.describe(
        role="Role to assign upon verification",
        welcome_channel="Channel to send welcome messages",
        method="Verification method: 'dm' or 'channel'",
        verify_channel="Channel for verification (REQUIRED if method is 'channel')",
        verification_type="Type of verification (button/captcha)"
    )
    @is_admin()
    async def setup_verification(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        welcome_channel: discord.TextChannel,
        method: str,
        verify_channel: Optional[discord.TextChannel] = None,
        verification_type: str = "button"
    ):
        """Setup verification system (ADMIN ONLY)"""
        method = method.lower()
        
        if method not in ['dm', 'channel']:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Method", "Method must be 'dm' or 'channel'"),
                ephemeral=True
            )
            return
        
        if method == 'channel' and not verify_channel:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Missing Channel", "You must specify a verify_channel when using 'channel' method"),
                ephemeral=True
            )
            return
        
        if verification_type not in ['button', 'captcha']:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Type", "Verification type must be 'button' or 'captcha'"),
                ephemeral=True
            )
            return

        # Show modal to get welcome message
        modal = VerificationSetupModal(self, role, welcome_channel, method, verify_channel, verification_type)
        await interaction.response.send_modal(modal)

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
                {"name": "Type", "value": guild_config.get("verification_type", "button"), "inline": True},
                {"name": "Verified Role", "value": verified_role.mention if verified_role else "Missing role", "inline": False},
                {"name": "Welcome Channel", "value": welcome_channel.mention if welcome_channel else "Not set", "inline": False},
                {"name": "Verify Channel", "value": verify_channel.mention if verify_channel else "DM only / not set", "inline": False},
                {
                    "name": "Welcome Message",
                    "value": (guild_config.get("welcome_message") or "Not set")[:250],
                    "inline": False
                },
                {
                    "name": "Note",
                    "value": (
                        "There is only one saved verification configuration per server. "
                        "Running `/setup-verification` again updates that single config rather than creating a second background process. "
                        "Any old verification messages already sent in channels are just normal messages and can be deleted manually."
                    ),
                    "inline": False
                }
            ]
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
            "verify_channel": None
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
        accent_color="Accent hex color such as #F5B8C7",
        text_color="Text hex color such as #F1C1CC",
        background_image_url="Optional background image URL for the card"
    )
    @is_admin()
    async def welcome_card_config(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        enabled: Optional[bool] = None,
        message: Optional[str] = None,
        title: Optional[str] = None,
        subtitle: Optional[str] = None,
        accent_color: Optional[str] = None,
        text_color: Optional[str] = None,
        background_image_url: Optional[str] = None
    ):
        """Configure welcome card appearance and behavior."""
        guild_config = await self.db.get_guild(interaction.guild.id)
        if not guild_config:
            guild_config = await self.db.create_guild(interaction.guild.id)

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
        if accent_color is not None:
            update_data["welcome_card_accent_color"] = accent_color
        if text_color is not None:
            update_data["welcome_card_text_color"] = text_color
        if background_image_url is not None:
            update_data["welcome_card_background_url"] = background_image_url.strip() or None

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
                {"name": "Message", "value": (guild_config.get("welcome_card_message") or DEFAULT_WELCOME_CARD_MESSAGE)[:250], "inline": False},
                {"name": "Title", "value": guild_config.get("welcome_card_title", DEFAULT_WELCOME_CARD_TITLE), "inline": False},
                {"name": "Subtitle", "value": guild_config.get("welcome_card_subtitle", DEFAULT_WELCOME_CARD_SUBTITLE), "inline": False},
                {
                    "name": "Background Image",
                    "value": guild_config.get("welcome_card_background_url") or "Using generated default background",
                    "inline": False
                },
                {
                    "name": "Placeholders",
                    "value": "`{user}` `{username}` `{display_name}` `{server}` `{member_count}`",
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
            "member_count": interaction.guild.member_count or len(interaction.guild.members)
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
            "This will be sent in DMs to new members along with the verification button."
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
                embed=EmbedFactory.error("Not Configured", "Please setup verification first with /setup-verification"),
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
