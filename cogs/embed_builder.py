"""
Embed Builder Cog for Logiq
Provides slash commands for sending custom embeds to server channels.
"""

import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.app_commands import errors as app_command_errors
from discord.ext import commands

from cogs.verification import VerificationButton
from database.db_manager import DatabaseManager
from utils.embeds import EmbedFactory

logger = logging.getLogger(__name__)

HEX_COLOR_PATTERN = re.compile(r"^#?[0-9A-Fa-f]{6}$")
MESSAGE_ID_PATTERN = re.compile(r"^\d{17,20}$")
MESSAGE_URL_PATTERN = re.compile(
    r"^https?://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/\d+/\d+/(\d{17,20})/?$"
)


def can_manage_embeds():
    """Allow members with Manage Messages or Administrator permissions."""
    async def predicate(interaction: discord.Interaction) -> bool:
        permissions = interaction.user.guild_permissions
        if permissions.administrator or permissions.manage_messages:
            return True
        raise app_command_errors.MissingPermissions(["manage_messages"])

    return app_commands.check(predicate)


class RulesEmbedModal(discord.ui.Modal):
    """Modal for composing a richly formatted rules panel."""

    def __init__(
        self,
        cog: "EmbedBuilder",
        channel: discord.TextChannel,
        *,
        color: str,
        verification: bool,
        title: Optional[str],
        button_label: str = "Accept",
        edit_message: Optional[discord.Message] = None,
        defaults: Optional[dict] = None,
        persist_rules_config: bool = False
    ):
        super().__init__(title="Edit Rules Panel" if edit_message else "Create Rules Panel")
        self.cog = cog
        self.channel = channel
        self.color = color
        self.verification = verification
        self.button_label = button_label
        self.edit_message = edit_message
        self.persist_rules_config = persist_rules_config
        defaults = defaults or {}

        self.panel_message = discord.ui.TextInput(
            label="Message Above The Embed",
            placeholder="Optional text above the embed. Discord markdown works here.",
            style=discord.TextStyle.paragraph,
            required=False,
            default=defaults.get("content"),
            max_length=2000
        )
        self.embed_title = discord.ui.TextInput(
            label="Embed Title",
            placeholder="Server Rules",
            default=defaults.get("title") or title or "Server Rules",
            required=True,
            max_length=256
        )
        self.rules_text = discord.ui.TextInput(
            label="Rules Text",
            placeholder="Write your rules here. Line breaks and markdown are preserved.",
            style=discord.TextStyle.paragraph,
            required=True,
            default=defaults.get("description"),
            max_length=4000
        )
        self.image_url = discord.ui.TextInput(
            label="Embed Image URL",
            placeholder="Optional image URL for the bottom of the embed",
            required=False,
            default=defaults.get("image_url"),
            max_length=1000
        )
        self.footer = discord.ui.TextInput(
            label="Footer Text",
            placeholder="Optional footer text",
            required=False,
            default=defaults.get("footer"),
            max_length=2048
        )

        for item in [
            self.panel_message,
            self.embed_title,
            self.rules_text,
            self.image_url,
            self.footer
        ]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog._submit_rules_embed_modal(
            interaction,
            channel=self.channel,
            color=self.color,
            verification=self.verification,
            button_label=self.button_label,
            edit_message=self.edit_message,
            persist_rules_config=self.persist_rules_config,
            panel_message=self.panel_message.value.strip() or None,
            title=self.embed_title.value.strip() or "Server Rules",
            rules_text=self.rules_text.value,
            image_url=self.image_url.value.strip() or None,
            footer=self.footer.value.strip() or None
        )


class EmbedEditModal(discord.ui.Modal):
    """Modal for editing an existing bot-authored embed."""

    def __init__(
        self,
        cog: "EmbedBuilder",
        *,
        message: discord.Message,
        color: str,
        defaults: dict
    ):
        super().__init__(title="Edit Embed")
        self.cog = cog
        self.message = message
        self.color = color

        self.message_content = discord.ui.TextInput(
            label="Message Above The Embed",
            placeholder="Optional text above the embed. Discord markdown works here.",
            style=discord.TextStyle.paragraph,
            required=False,
            default=defaults.get("content"),
            max_length=2000
        )
        self.embed_title = discord.ui.TextInput(
            label="Embed Title",
            placeholder="Announcement",
            required=True,
            default=defaults.get("title") or "Announcement",
            max_length=256
        )
        self.embed_description = discord.ui.TextInput(
            label="Embed Description",
            placeholder="Write the embed body here.",
            style=discord.TextStyle.paragraph,
            required=True,
            default=defaults.get("description"),
            max_length=4000
        )
        visual_default = self.cog._format_visual_defaults(
            defaults.get("image_url"),
            defaults.get("thumbnail_url")
        )
        self.visual_urls = discord.ui.TextInput(
            label="Image URL | Thumbnail URL",
            placeholder="Optional. Use `image_url | thumbnail_url` or just one image URL.",
            required=False,
            default=visual_default,
            max_length=1000
        )
        self.footer = discord.ui.TextInput(
            label="Footer Text",
            placeholder="Optional footer text",
            required=False,
            default=defaults.get("footer"),
            max_length=2048
        )

        for item in [
            self.message_content,
            self.embed_title,
            self.embed_description,
            self.visual_urls,
            self.footer
        ]:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        image_url, thumbnail_url = self.cog._parse_visual_urls(self.visual_urls.value)
        await self.cog._submit_embed_edit_modal(
            interaction,
            message=self.message,
            color=self.color,
            content=self.message_content.value.strip() or None,
            title=self.embed_title.value.strip() or "Announcement",
            description=self.embed_description.value,
            image_url=image_url,
            thumbnail_url=thumbnail_url,
            footer=self.footer.value.strip() or None
        )


class EmbedBuilder(commands.Cog):
    """Embed creation commands."""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config

    def _log_exception(self, message: str, error: Exception) -> None:
        """Log an unexpected exception using the project's logger when available."""
        if hasattr(self.bot, "logger"):
            self.bot.logger.error(f"{message}: {error}", exc_info=True)
        else:
            logger.error("%s: %s", message, error, exc_info=True)

    def _parse_color(self, value: str) -> Optional[int]:
        """Validate and parse a six-digit hex color."""
        if not HEX_COLOR_PATTERN.fullmatch(value):
            return None
        return int(value.lstrip("#"), 16)

    def _parse_message_reference(self, value: str) -> Optional[int]:
        """Parse a raw message ID or Discord message URL into an integer ID."""
        candidate = value.strip()
        if MESSAGE_ID_PATTERN.fullmatch(candidate):
            return int(candidate)

        url_match = MESSAGE_URL_PATTERN.fullmatch(candidate)
        if url_match:
            return int(url_match.group(1))

        return None

    def _format_permissions(self, permissions: list[str]) -> str:
        """Format permission names for user-facing error messages."""
        return ", ".join(permission.replace("_", " ").title() for permission in permissions)

    def _get_bot_member(self, guild: discord.Guild) -> Optional[discord.Member]:
        """Fetch the bot's guild member object."""
        return guild.me or guild.get_member(self.bot.user.id)

    def _get_missing_channel_permissions(self, channel: discord.TextChannel) -> list[str]:
        """Return the channel permissions the bot is missing for embed delivery."""
        bot_member = self._get_bot_member(channel.guild)
        if bot_member is None:
            return ["view_channel", "send_messages", "embed_links"]

        permissions = channel.permissions_for(bot_member)
        required = ["view_channel", "send_messages", "embed_links"]
        return [permission for permission in required if not getattr(permissions, permission, False)]

    def _extract_embed_defaults(self, message: discord.Message) -> dict:
        """Extract editable defaults from the first embed on a message."""
        embed = message.embeds[0] if message.embeds else None
        color_value = "#5865F2"
        if embed is not None and embed.color is not None and embed.color.value is not None:
            color_value = f"#{embed.color.value:06X}"

        return {
            "content": message.content or None,
            "title": embed.title if embed else None,
            "description": embed.description if embed else None,
            "image_url": embed.image.url if embed and embed.image else None,
            "thumbnail_url": embed.thumbnail.url if embed and embed.thumbnail else None,
            "footer": embed.footer.text if embed and embed.footer else None,
            "color": color_value
        }

    def _format_visual_defaults(
        self,
        image_url: Optional[str],
        thumbnail_url: Optional[str]
    ) -> Optional[str]:
        """Format existing image and thumbnail URLs into one editable field."""
        image_value = (image_url or "").strip()
        thumbnail_value = (thumbnail_url or "").strip()
        if image_value and thumbnail_value:
            return f"{image_value} | {thumbnail_value}"
        if image_value:
            return image_value
        if thumbnail_value:
            return f"| {thumbnail_value}"
        return None

    def _parse_visual_urls(self, value: str) -> tuple[Optional[str], Optional[str]]:
        """Parse a combined image/thumbnail field into separate values."""
        candidate = value.strip()
        if not candidate:
            return None, None

        if "|" in candidate:
            image_part, thumbnail_part = candidate.split("|", 1)
            image_url = image_part.strip() or None
            thumbnail_url = thumbnail_part.strip() or None
            return image_url, thumbnail_url

        return candidate, None

    async def _fetch_editable_message(
        self,
        channel: discord.TextChannel,
        message_id: int,
        *,
        require_embed: bool = True
    ) -> tuple[Optional[discord.Message], Optional[str]]:
        """Fetch a bot-authored message that is safe to edit."""
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            return None, "I couldn't find a message with that ID in the selected channel."
        except discord.Forbidden:
            return None, f"I don't have permission to read message history in {channel.mention}."
        except discord.HTTPException as error:
            self._log_exception("Failed to fetch editable message", error)
            return None, "Discord rejected the message lookup. Please try again."

        if self.bot.user is None or message.author.id != self.bot.user.id:
            return None, "I can only edit messages that were sent by this bot."

        if require_embed and not message.embeds:
            return None, "That bot message does not currently contain an embed to edit."

        return message, None

    def _build_embed(
        self,
        *,
        title: str,
        description: str,
        color_value: int,
        footer: Optional[str] = None,
        image_url: Optional[str] = None,
        thumbnail_url: Optional[str] = None
    ) -> discord.Embed:
        """Build a standard editable embed."""
        return EmbedFactory.create(
            title=title,
            description=description,
            color=color_value,
            footer=footer,
            thumbnail=thumbnail_url,
            image=image_url
        )

    async def _get_verification_view(
        self, guild: discord.Guild
    ) -> tuple[Optional[discord.ui.View], Optional[str]]:
        """Reuse the existing verification button view when verification is configured."""
        verification_cog = self.bot.get_cog("Verification")
        if verification_cog is None:
            return None, "The verification system is not currently loaded."

        guild_config = await self.db.get_guild(guild.id)
        if not guild_config or not guild_config.get("verified_role"):
            return None, "Please run `/verification-role` before attaching a verification button."

        if guild.get_role(guild_config["verified_role"]) is None:
            return None, "The configured verified role no longer exists. Please run `/verification-role` again."

        return VerificationButton(verification_cog), None

    async def _get_rules_accept_view(
        self, guild: discord.Guild, button_label: str
    ) -> tuple[Optional[discord.ui.View], Optional[object], Optional[str]]:
        """Get the rules-acceptance verification view and verification cog."""
        verification_cog = self.bot.get_cog("Verification")
        if verification_cog is None:
            return None, None, "The verification system is not currently loaded."

        guild_config = await self.db.get_guild(guild.id)
        if not guild_config or not guild_config.get("verified_role"):
            return None, None, "Please run `/verification-role` first so there is a verified role to grant."

        if guild.get_role(guild_config["verified_role"]) is None:
            return None, None, "The configured verified role no longer exists. Please run `/verification-role` again."

        guild_config["rules_button_label"] = button_label
        return verification_cog.get_rules_accept_view(guild_config), verification_cog, None

    async def _submit_rules_embed_modal(
        self,
        interaction: discord.Interaction,
        *,
        channel: discord.TextChannel,
        color: str,
        verification: bool,
        button_label: str,
        edit_message: Optional[discord.Message] = None,
        persist_rules_config: bool = False,
        panel_message: Optional[str],
        title: str,
        rules_text: str,
        image_url: Optional[str],
        footer: Optional[str]
    ) -> None:
        """Send a richly formatted rules panel and optionally wire it into verification."""
        color_value = self._parse_color(color)
        if color_value is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Invalid Colour",
                    "Please provide a valid 6-digit hex colour such as `#5865F2`."
                ),
                ephemeral=True
            )
            return

        missing_permissions = self._get_missing_channel_permissions(channel)
        if missing_permissions:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Bot Missing Channel Permissions",
                    f"I cannot send embeds in {channel.mention}. Missing: **{self._format_permissions(missing_permissions)}**."
                ),
                ephemeral=True
            )
            return

        view = None
        verification_cog = self.bot.get_cog("Verification") if persist_rules_config else None
        if verification:
            view, verification_cog, error_message = await self._get_rules_accept_view(
                interaction.guild,
                button_label
            )
            if error_message:
                await interaction.response.send_message(
                    embed=EmbedFactory.error("Verification Unavailable", error_message),
                    ephemeral=True
                )
                return

        try:
            embed = self._build_embed(
                title=title,
                description=rules_text,
                color_value=color_value,
                footer=footer,
                image_url=image_url
            )
            if edit_message is None:
                sent_message = await channel.send(content=panel_message, embed=embed, view=view)
            else:
                sent_message = await edit_message.edit(content=panel_message, embed=embed, view=view)

            if persist_rules_config and verification_cog is not None:
                await verification_cog.save_rules_panel_config(
                    interaction.guild.id,
                    channel_id=channel.id,
                    message_id=sent_message.id,
                    panel_message=panel_message,
                    title=title,
                    description=rules_text,
                    color=color,
                    image_url=image_url,
                    footer=footer,
                    button_label=button_label,
                    enabled=verification
                )

            confirmation = "Rules panel sent successfully."
            if verification:
                confirmation = (
                    "Rules panel sent successfully and the Accept button is now the verification step for incoming members."
                )
            if edit_message is not None:
                confirmation = "Rules panel updated successfully."
                if verification:
                    confirmation = (
                        "Rules panel updated successfully and the Accept button remains the verification step for incoming members."
                    )

            await interaction.response.send_message(
                embed=EmbedFactory.success("Rules Sent", f"{confirmation} Posted in {channel.mention}."),
                ephemeral=True
            )
            logger.info(
                "%s %s a rules panel in %s (%s) with verification=%s",
                interaction.user,
                "updated" if edit_message is not None else "created",
                channel.guild,
                channel.id,
                verification
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Bot Missing Permissions",
                    f"I don't have permission to send messages in {channel.mention}."
                ),
                ephemeral=True
            )
        except discord.HTTPException as error:
            self._log_exception("Failed to send rules panel", error)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Send Failed",
                    "Discord rejected that rules panel. Please review the content and any image URL."
                ),
                ephemeral=True
            )

    async def _submit_embed_edit_modal(
        self,
        interaction: discord.Interaction,
        *,
        message: discord.Message,
        color: str,
        content: Optional[str],
        title: str,
        description: str,
        image_url: Optional[str],
        thumbnail_url: Optional[str],
        footer: Optional[str]
    ) -> None:
        """Edit an existing bot-authored embed message."""
        color_value = self._parse_color(color)
        if color_value is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Invalid Colour",
                    "Please provide a valid 6-digit hex colour such as `#5865F2`."
                ),
                ephemeral=True
            )
            return

        try:
            embed = self._build_embed(
                title=title,
                description=description,
                color_value=color_value,
                footer=footer,
                image_url=image_url,
                thumbnail_url=thumbnail_url
            )
            await message.edit(content=content, embed=embed)
            await interaction.response.send_message(
                embed=EmbedFactory.success(
                    "Embed Updated",
                    f"The embed in {message.channel.mention} was updated successfully."
                ),
                ephemeral=True
            )
            logger.info("%s edited embed message %s in %s", interaction.user, message.id, message.guild)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Bot Missing Permissions",
                    f"I don't have permission to edit that message in {message.channel.mention}."
                ),
                ephemeral=True
            )
        except discord.HTTPException as error:
            self._log_exception("Failed to edit custom embed", error)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Edit Failed",
                    "Discord rejected that embed update. Please double-check the content and any image URLs."
                ),
                ephemeral=True
            )

    async def _send_response(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        ephemeral: bool = True
    ) -> None:
        """Safely reply to an interaction whether or not it has already responded."""
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_command_errors.AppCommandError
    ) -> None:
        """Handle slash command errors for this cog."""
        original = getattr(error, "original", error)

        if isinstance(original, app_command_errors.MissingPermissions):
            await self._send_response(
                interaction,
                embed=EmbedFactory.error(
                    "Missing Permissions",
                    "You need the **Manage Messages** permission or **Administrator** to use this command."
                )
            )
            return

        if isinstance(original, app_command_errors.BotMissingPermissions):
            missing = self._format_permissions(list(original.missing_permissions))
            await self._send_response(
                interaction,
                embed=EmbedFactory.error(
                    "Bot Missing Permissions",
                    f"I need these permissions to complete that action: **{missing}**."
                )
            )
            return

        self._log_exception("Unexpected embed builder command error", original)
        await self._send_response(
            interaction,
            embed=EmbedFactory.error(
                "Unexpected Error",
                "Something went wrong while creating the embed. Please check the logs and try again."
            )
        )

    @app_commands.command(name="embed_create", description="Send a custom embed to a channel")
    @app_commands.describe(
        title="Embed title",
        description="Embed description",
        channel="Channel to send the embed to",
        color="Hex color, such as #5865F2",
        image_url="Optional image URL",
        thumbnail_url="Optional thumbnail URL",
        footer="Optional footer text"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @can_manage_embeds()
    async def embed_create(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        channel: discord.TextChannel,
        color: str = "#5865F2",
        image_url: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        footer: Optional[str] = None
    ):
        """Create and send a custom embed."""
        color_value = self._parse_color(color)
        if color_value is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Invalid Colour",
                    "Please provide a valid 6-digit hex colour such as `#5865F2`."
                ),
                ephemeral=True
            )
            return

        missing_permissions = self._get_missing_channel_permissions(channel)
        if missing_permissions:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Bot Missing Channel Permissions",
                    f"I cannot send embeds in {channel.mention}. Missing: **{self._format_permissions(missing_permissions)}**."
                ),
                ephemeral=True
            )
            return

        try:
            embed = EmbedFactory.create(
                title=title,
                description=description,
                color=color_value,
                footer=footer,
                thumbnail=thumbnail_url,
                image=image_url
            )
            await channel.send(embed=embed)
            await interaction.response.send_message(
                embed=EmbedFactory.success(
                    "Embed Sent",
                    f"Your embed was sent to {channel.mention}."
                ),
                ephemeral=True
            )
            logger.info("%s created an embed in %s (%s)", interaction.user, channel.guild, channel.id)
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Bot Missing Permissions",
                    f"I don't have permission to send messages in {channel.mention}."
                ),
                ephemeral=True
            )
        except discord.HTTPException as error:
            self._log_exception("Failed to send custom embed", error)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Send Failed",
                    "Discord rejected that embed. Please double-check the content and any image URLs."
                ),
                ephemeral=True
            )

    @app_commands.command(name="embed_rules", description="Send a rules embed to a channel")
    @app_commands.describe(
        channel="Channel to send the rules embed to",
        title="Optional rules embed title",
        color="Hex color, such as #5865F2",
        verification="Make the button below this panel the actual verification step",
        button_label="Label for the rules acceptance button"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @can_manage_embeds()
    async def embed_rules(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: Optional[str] = "Server Rules",
        color: str = "#5865F2",
        verification: bool = False,
        button_label: str = "Accept"
    ):
        """Open a rich rules panel builder with optional verification wiring."""
        if self._parse_color(color) is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Invalid Colour",
                    "Please provide a valid 6-digit hex colour such as `#5865F2`."
                ),
                ephemeral=True
            )
            return

        try:
            modal = RulesEmbedModal(
                self,
                channel,
                color=color,
                verification=verification,
                title=title,
                button_label=button_label or "Accept",
                persist_rules_config=verification
            )
            await interaction.response.send_modal(modal)
        except Exception as error:
            self._log_exception("Failed to open rules panel modal", error)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Setup Failed",
                    "I couldn't open the rules panel builder. Please try again."
                ),
                ephemeral=True
            )

    @app_commands.command(name="embed_edit", description="Edit an existing bot embed in a channel")
    @app_commands.describe(
        channel="Channel containing the embed message",
        message_id="Message ID or Discord message link for the bot embed to edit",
        color="Hex color, such as #5865F2"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @can_manage_embeds()
    async def embed_edit(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message_id: str,
        color: Optional[str] = None
    ):
        """Open an editor for an existing bot-authored embed message."""
        parsed_message_id = self._parse_message_reference(message_id)
        if parsed_message_id is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Invalid Message ID",
                    "Please provide either a raw Discord message ID or a full Discord message link."
                ),
                ephemeral=True
            )
            return

        message, error_message = await self._fetch_editable_message(channel, parsed_message_id)
        if error_message:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Edit Unavailable", error_message),
                ephemeral=True
            )
            return

        defaults = self._extract_embed_defaults(message)
        color_value = color or defaults["color"]
        if self._parse_color(color_value) is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Invalid Colour",
                    "Please provide a valid 6-digit hex colour such as `#5865F2`."
                ),
                ephemeral=True
            )
            return

        try:
            modal = EmbedEditModal(
                self,
                message=message,
                color=color_value,
                defaults=defaults
            )
            await interaction.response.send_modal(modal)
        except Exception as error:
            self._log_exception("Failed to open embed edit modal", error)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Setup Failed",
                    "I couldn't open the embed editor. Please try again."
                ),
                ephemeral=True
            )

    @app_commands.command(name="embed_rules_edit", description="Edit an existing rules panel in place")
    @app_commands.describe(
        channel="Channel containing the rules panel",
        message_id="Message ID or Discord message link for the rules panel",
        color="Hex color, such as #5865F2",
        verification="Keep or remove the verification button under this panel",
        button_label="Label for the rules acceptance button when verification is enabled"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @can_manage_embeds()
    async def embed_rules_edit(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message_id: str,
        color: Optional[str] = None,
        verification: Optional[bool] = None,
        button_label: Optional[str] = None
    ):
        """Open the rules panel editor for an existing bot-authored message."""
        parsed_message_id = self._parse_message_reference(message_id)
        if parsed_message_id is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Invalid Message ID",
                    "Please provide either a raw Discord message ID or a full Discord message link."
                ),
                ephemeral=True
            )
            return

        message, error_message = await self._fetch_editable_message(channel, parsed_message_id)
        if error_message:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Edit Unavailable", error_message),
                ephemeral=True
            )
            return

        guild_config = await self.db.get_guild(interaction.guild.id)
        defaults = self._extract_embed_defaults(message)
        color_value = color or defaults["color"]
        if self._parse_color(color_value) is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Invalid Colour",
                    "Please provide a valid 6-digit hex colour such as `#5865F2`."
                ),
                ephemeral=True
            )
            return

        verification_enabled = (
            verification
            if verification is not None
            else bool(guild_config.get("rules_panel_enabled", False)) if guild_config else False
        )
        resolved_button_label = (
            button_label
            or (guild_config.get("rules_button_label") if guild_config else None)
            or "Accept"
        )

        try:
            modal = RulesEmbedModal(
                self,
                channel,
                color=color_value,
                verification=verification_enabled,
                title=defaults.get("title") or "Server Rules",
                button_label=resolved_button_label,
                edit_message=message,
                defaults=defaults,
                persist_rules_config=True
            )
            await interaction.response.send_modal(modal)
        except Exception as error:
            self._log_exception("Failed to open rules panel edit modal", error)
            await interaction.response.send_message(
                embed=EmbedFactory.error(
                    "Setup Failed",
                    "I couldn't open the rules panel editor. Please try again."
                ),
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    """Setup function for cog loading."""
    await bot.add_cog(EmbedBuilder(bot, bot.db, bot.config))
