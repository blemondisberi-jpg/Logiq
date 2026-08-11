"""
Utility Cog for Logiq
General utility commands
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta
from typing import Optional
import logging
import asyncio

from utils.embeds import EmbedFactory, EmbedColor
from utils.converters import TimeConverter
from utils.permissions import is_admin
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)

HELP_TOPIC_CHOICES = [
    app_commands.Choice(name="Overview", value="overview"),
    app_commands.Choice(name="Setup", value="setup"),
    app_commands.Choice(name="Embeds", value="embeds"),
    app_commands.Choice(name="Verification", value="verification"),
    app_commands.Choice(name="Roles", value="roles"),
    app_commands.Choice(name="Tickets", value="tickets"),
    app_commands.Choice(name="Voice", value="voice"),
    app_commands.Choice(name="Alerts", value="alerts"),
    app_commands.Choice(name="AI", value="ai"),
    app_commands.Choice(name="Community", value="community"),
    app_commands.Choice(name="Economy & Games", value="economy_games"),
    app_commands.Choice(name="Music", value="music"),
    app_commands.Choice(name="Moderation", value="moderation"),
    app_commands.Choice(name="Utilities", value="utilities"),
]

HELP_TOPIC_LAYOUTS = {
    "overview": {
        "title": "📘 Logiq Help",
        "description": (
            "Quick admin overview for the most important setup and maintenance commands.\n"
            "Use `/help topic:<section>` to jump into a specific area."
        ),
        "fields": [
            (
                "🚀 Getting Started",
                ["reload", "sync", "modules", "config"]
            ),
            (
                "🔐 Core Setup",
                ["embed_rules", "verification-role", "verification-mode", "welcome-card-config", "ticket-setup"]
            ),
            (
                "📣 Alerts & Diagnostics",
                ["alert add", "alert edit", "alert debug", "alert eventsub-sync", "alert kick-sync"]
            ),
            (
                "🎨 Panels & Embeds",
                ["embed_create", "embed_edit", "embed_rules_edit", "create-role-menu", "create-color-panel", "repair-role-menu"]
            ),
            (
                "🧩 Other Areas",
                ["ticket-panel", "setup-tempvoice", "ask", "play"]
            ),
        ],
    },
    "setup": {
        "title": "🛠️ Logiq Help - Setup",
        "description": "Use these commands when bringing a new server online or reworking the core setup.",
        "fields": [
            (
                "🔐 Verification & Welcome",
                [
                    "embed_rules",
                    "verification-role",
                    "verification-mode",
                    "verification-platform-toggle",
                    "verification-signpost",
                    "welcome-card-config",
                    "welcome-card-preview",
                    "send-verification",
                ]
            ),
            (
                "🎫 Support & Logging",
                ["ticket-setup", "ticket-panel", "setlogchannel", "auditlog-status"]
            ),
            (
                "📊 Stats & Server Channels",
                ["serverstats-channels setup", "serverstats-channels refresh", "serverstats-channels remove", "setlevelchannel", "birthday-config"]
            ),
        ],
    },
    "embeds": {
        "title": "🧱 Logiq Help - Embeds",
        "description": "Commands for creating, editing, and maintaining custom embed panels without recreating them from scratch.",
        "fields": [
            (
                "✍️ Custom Embeds",
                ["embed_create", "embed_edit"]
            ),
            (
                "📜 Rules Panels",
                ["embed_rules", "embed_rules_edit"]
            ),
            (
                "🔗 Common Pairings",
                ["verification-role", "verification-signpost", "send-verification"]
            ),
        ],
    },
    "verification": {
        "title": "🔐 Logiq Help - Verification",
        "description": "Everything related to rules panels, verified roles, captcha mode, platform linking, and welcome cards.",
        "fields": [
            (
                "📋 Rules & Access",
                [
                    "embed_rules",
                    "embed_rules_edit",
                    "verification-role",
                    "verification-status",
                    "verification-disable",
                    "send-verification",
                ]
            ),
            (
                "🧩 Verification Flow",
                [
                    "verification-mode",
                    "verification-platform-toggle",
                    "verification-platform-role",
                    "verification-platform-link",
                    "verification-signpost",
                ]
            ),
            (
                "🖼️ Welcome Experience",
                ["welcome-card-config", "welcome-card-preview", "set-welcome-message"]
            ),
        ],
    },
    "roles": {
        "title": "🎭 Logiq Help - Roles",
        "description": "Commands for role menus, colour panels, one-off assignments, and bulk role changes.",
        "fields": [
            (
                "🧷 Self-Assign Panels",
                ["create-role-menu", "create-color-panel", "repair-role-menu"]
            ),
            (
                "👤 Individual Role Changes",
                ["addrole", "removerole"]
            ),
            (
                "👥 Bulk Role Changes",
                ["massrole-add", "massrole-remove", "massrole-add-filter", "massrole-remove-filter"]
            ),
        ],
    },
    "tickets": {
        "title": "🎫 Logiq Help - Tickets",
        "description": "Support ticket configuration, panel deployment, and active ticket management.",
        "fields": [
            (
                "🛠️ Ticket Setup",
                ["ticket-setup", "ticket-panel"]
            ),
            (
                "📂 Ticket Management",
                ["close-ticket", "tickets"]
            ),
            (
                "🧾 Related Tools",
                ["setlogchannel", "config"]
            ),
        ],
    },
    "voice": {
        "title": "🎙️ Logiq Help - Temp Voice",
        "description": "Temporary voice-channel setup plus the member controls for locking, resizing, and claiming channels.",
        "fields": [
            (
                "🛠️ Setup",
                ["setup-tempvoice"]
            ),
            (
                "🔒 Controls",
                ["voice-lock", "voice-unlock", "voice-limit", "voice-rename", "voice-claim"]
            ),
        ],
    },
    "alerts": {
        "title": "📣 Logiq Help - Alerts",
        "description": "Commands for Twitch, Kick, and YouTube alerts plus diagnostics and webhook sync tools.",
        "fields": [
            (
                "📡 Alert Management",
                ["alert add", "alert edit", "alert remove", "alert list", "alert test", "alert run"]
            ),
            (
                "🩺 Diagnostics",
                ["alert debug", "alert eventsub-sync", "alert kick-sync"]
            ),
            (
                "🔗 YouTube Owner Flow",
                ["alert youtube-oauth-connect", "alert youtube-oauth-disconnect"]
            ),
        ],
    },
    "ai": {
        "title": "🤖 Logiq Help - AI",
        "description": "AI chat, summaries, and conversation resets. These commands need the AI module enabled and an API key configured.",
        "fields": [
            (
                "💬 AI Commands",
                ["ask", "summarize", "clear-conversation"]
            ),
            (
                "🧾 Related Admin Checks",
                ["modules", "reload", "sync"]
            ),
        ],
    },
    "community": {
        "title": "🌍 Logiq Help - Community",
        "description": "Server growth, birthdays, analytics, leveling, time lookups, and auto-updating stat channels.",
        "fields": [
            (
                "📈 Analytics & Activity",
                ["analytics", "activity", "serverstats"]
            ),
            (
                "🎂 Birthdays & Time",
                ["birthday-set", "birthday-remove", "birthday-config", "birthday-list", "birthday-test", "time-country"]
            ),
            (
                "📊 Progress & Stats Channels",
                ["setlevelchannel", "setlevel", "resetlevels", "serverstats-channels setup", "serverstats-channels refresh", "serverstats-channels remove"]
            ),
        ],
    },
    "economy_games": {
        "title": "🎮 Logiq Help - Economy & Games",
        "description": "Economy, casual game commands, leaderboards, and giveaway management.",
        "fields": [
            (
                "💎 Economy",
                ["daily", "give", "coinflip-bet", "shop", "addbalance"]
            ),
            (
                "🏆 Games & Progress",
                ["setup-game-panel", "rank", "balance", "leaderboard"]
            ),
            (
                "🎉 Giveaways",
                ["giveaway", "gend", "greroll"]
            ),
        ],
    },
    "music": {
        "title": "🎵 Logiq Help - Music",
        "description": "Music playback commands for servers where the music module is still enabled.",
        "fields": [
            (
                "▶️ Playback",
                ["play", "join", "leave", "queue", "skip", "pause", "resume", "nowplaying"]
            ),
            (
                "🎚️ Control",
                ["volume"]
            ),
        ],
    },
    "moderation": {
        "title": "🛡️ Logiq Help - Moderation",
        "description": "Commands for moderation actions, channel controls, and moderation visibility.",
        "fields": [
            (
                "🔨 Member Actions",
                ["warn", "warnings", "timeout", "kick", "ban", "unban", "nickname"]
            ),
            (
                "💬 Channel Controls",
                ["clear", "purge", "slowmode", "lock", "unlock"]
            ),
            (
                "🧾 Visibility",
                ["setlogchannel", "auditlog-status", "config"]
            ),
        ],
    },
    "utilities": {
        "title": "🧰 Logiq Help - Utilities",
        "description": "General utility, info, analytics, birthday, and time tools that admins commonly use.",
        "fields": [
            (
                "ℹ️ Info & Utility",
                ["botinfo", "serverstats", "userinfo", "avatar", "poll", "remind", "time-country"]
            ),
            (
                "🎂 Birthday Tools",
                ["birthday-set", "birthday-remove", "birthday-config", "birthday-list", "birthday-test"]
            ),
            (
                "📈 Activity & Progress",
                ["analytics", "activity", "setlevelchannel"]
            ),
        ],
    },
}


class PollView(discord.ui.View):
    """Interactive poll view"""

    def __init__(self, question: str, options: list, duration: int):
        super().__init__(timeout=duration)
        self.question = question
        self.options = options
        self.votes = {i: [] for i in range(len(options))}

    def get_results_embed(self) -> discord.Embed:
        """Generate results embed"""
        total_votes = sum(len(voters) for voters in self.votes.values())
        description = f"**{self.question}**\n\n"

        for i, option in enumerate(self.options):
            vote_count = len(self.votes[i])
            percentage = (vote_count / total_votes * 100) if total_votes > 0 else 0
            bar_length = int(percentage / 10)
            bar = "█" * bar_length + "░" * (10 - bar_length)
            description += f"{i + 1}. {option}\n{bar} {vote_count} votes ({percentage:.1f}%)\n\n"

        embed = EmbedFactory.create(
            title="📊 Poll Results",
            description=description,
            color=EmbedColor.INFO
        )
        embed.set_footer(text=f"Total votes: {total_votes}")
        return embed

    @discord.ui.button(label="1", style=discord.ButtonStyle.primary, custom_id="poll_1")
    async def option_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, 0)

    @discord.ui.button(label="2", style=discord.ButtonStyle.primary, custom_id="poll_2")
    async def option_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, 1)

    @discord.ui.button(label="3", style=discord.ButtonStyle.primary, custom_id="poll_3")
    async def option_3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, 2)

    @discord.ui.button(label="4", style=discord.ButtonStyle.primary, custom_id="poll_4")
    async def option_4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, 3)

    async def _vote(self, interaction: discord.Interaction, option_index: int):
        """Handle vote"""
        if option_index >= len(self.options):
            await interaction.response.send_message("Invalid option", ephemeral=True)
            return

        user_id = interaction.user.id

        # Remove previous vote
        for voters in self.votes.values():
            if user_id in voters:
                voters.remove(user_id)

        # Add new vote
        self.votes[option_index].append(user_id)

        # Update message
        await interaction.response.edit_message(embed=self.get_results_embed())


class Utility(commands.Cog):
    """Utility commands cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.reminders_task = self.bot.loop.create_task(self.check_reminders())

    def cog_unload(self):
        """Cleanup on cog unload"""
        self.reminders_task.cancel()

    def _flatten_app_commands(self, commands_list: list, prefix: str = "") -> dict[str, str]:
        """Return a mapping of slash-command paths to their descriptions."""
        flattened: dict[str, str] = {}
        for command in commands_list:
            full_name = f"{prefix}{command.name}"
            description = getattr(command, "description", None) or "No description provided."
            flattened[full_name] = description

            if isinstance(command, app_commands.Group):
                flattened.update(self._flatten_app_commands(list(command.commands), prefix=f"{full_name} "))

        return flattened

    def _get_available_command_map(self, interaction: discord.Interaction) -> dict[str, str]:
        """Collect the currently registered slash commands for this scope."""
        command_map: dict[str, str] = {}

        # Global commands are the normal case for this deployment, so include them first.
        global_commands = list(self.bot.tree.get_commands())
        command_map.update(self._flatten_app_commands(global_commands))

        # If any guild-specific overrides exist, layer them on top.
        if interaction.guild is not None:
            guild_commands = list(self.bot.tree.get_commands(guild=interaction.guild))
            command_map.update(self._flatten_app_commands(guild_commands))

        return command_map

    def _format_help_section(self, command_map: dict[str, str], command_names: list[str]) -> str:
        """Format help lines for the commands that currently exist."""
        lines = []
        for command_name in command_names:
            description = command_map.get(command_name)
            if not description:
                continue
            lines.append(f"`/{command_name}` - {description}")
        return "\n".join(lines) or "No live commands in this section right now."

    def _build_help_embed(self, interaction: discord.Interaction, topic: str) -> discord.Embed:
        """Build the requested help embed."""
        topic_config = HELP_TOPIC_LAYOUTS.get(topic, HELP_TOPIC_LAYOUTS["overview"])
        command_map = self._get_available_command_map(interaction)
        embed = EmbedFactory.create(
            title=topic_config["title"],
            description=topic_config["description"],
            color=EmbedColor.INFO,
            footer="Grouped commands use a space, for example: /alert add"
        )

        for field_name, command_names in topic_config["fields"]:
            embed.add_field(
                name=field_name,
                value=self._format_help_section(command_map, command_names),
                inline=False
            )

        total_live_commands = len(
            [
                name for name in command_map
                if " " not in name or name.count(" ") == 1 or name.startswith("alert ") or name.startswith("serverstats-channels ")
            ]
        )
        embed.add_field(
            name="🧭 Tip",
            value=(
                f"This deployment currently exposes **{total_live_commands}** slash command entries.\n"
                "Use `/help topic:<section>` to narrow the list when you need something specific."
            ),
            inline=False
        )
        return embed

    async def check_reminders(self):
        """Background task to check for due reminders"""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                current_time = datetime.utcnow().timestamp()
                due_reminders = await self.db.get_due_reminders(current_time)

                for reminder in due_reminders:
                    try:
                        channel = self.bot.get_channel(reminder['channel_id'])
                        if channel:
                            user = await self.bot.fetch_user(reminder['user_id'])
                            embed = EmbedFactory.info(
                                "⏰ Reminder",
                                f"{user.mention} {reminder['message']}"
                            )
                            await channel.send(embed=embed)

                        await self.db.complete_reminder(str(reminder['_id']))
                    except Exception as e:
                        logger.error(f"Error sending reminder: {e}", exc_info=True)

                await asyncio.sleep(60)  # Check every minute
            except Exception as e:
                logger.error(f"Error in reminder checker: {e}", exc_info=True)
                await asyncio.sleep(60)

    @app_commands.command(name="help", description="View a guide to Logiq commands and setup")
    @app_commands.describe(topic="Optional help section to open directly")
    @app_commands.choices(topic=HELP_TOPIC_CHOICES)
    async def help_command(self, interaction: discord.Interaction, topic: Optional[app_commands.Choice[str]] = None):
        """Show a slash-command help guide."""
        selected_topic = topic.value if topic else "overview"
        embed = self._build_help_embed(interaction, selected_topic)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="poll", description="Create a poll (Admin)")
    @app_commands.describe(
        question="Poll question",
        option1="Option 1",
        option2="Option 2",
        option3="Option 3 (optional)",
        option4="Option 4 (optional)",
        duration="Duration in minutes (default: 60)"
    )
    @is_admin()
    async def poll(
        self,
        interaction: discord.Interaction,
        question: str,
        option1: str,
        option2: str,
        option3: Optional[str] = None,
        option4: Optional[str] = None,
        duration: int = 60
    ):
        """Create a poll"""
        options = [option1, option2]
        if option3:
            options.append(option3)
        if option4:
            options.append(option4)

        if duration < 1 or duration > 10080:  # Max 1 week
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Duration", "Duration must be between 1 minute and 1 week"),
                ephemeral=True
            )
            return

        view = PollView(question, options, duration * 60)

        # Only show buttons for available options
        for i in range(4):
            if i >= len(options):
                view.children[i].disabled = True

        embed = view.get_results_embed()
        embed.set_footer(text=f"Poll ends in {duration} minutes | Total votes: 0")

        await interaction.response.send_message(embed=embed, view=view)
        logger.info(f"{interaction.user} created poll in {interaction.guild}")

    @app_commands.command(name="remind", description="Set a reminder (Admin)")
    @app_commands.describe(
        duration="When to remind (e.g., 1h, 30m, 1d)",
        message="Reminder message"
    )
    @is_admin()
    async def remind(self, interaction: discord.Interaction, duration: str, message: str):
        """Set a reminder"""
        seconds = TimeConverter.parse(duration)
        if not seconds:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Duration", "Please provide a valid duration (e.g., 1h, 30m, 2d)"),
                ephemeral=True
            )
            return

        if seconds > 31536000:  # Max 1 year
            await interaction.response.send_message(
                embed=EmbedFactory.error("Duration Too Long", "Maximum reminder duration is 1 year"),
                ephemeral=True
            )
            return

        remind_at = datetime.utcnow().timestamp() + seconds

        reminder_data = {
            "user_id": interaction.user.id,
            "guild_id": interaction.guild.id,
            "channel_id": interaction.channel.id,
            "message": message,
            "remind_at": remind_at,
            "completed": False
        }

        await self.db.create_reminder(reminder_data)

        embed = EmbedFactory.success(
            "Reminder Set",
            f"I'll remind you in **{TimeConverter.format_seconds(seconds)}**\n\n"
            f"Message: {message}"
        )
        await interaction.response.send_message(embed=embed)
        logger.info(f"{interaction.user} set reminder in {interaction.guild}")

    @app_commands.command(name="serverstats", description="View server statistics (Admin)")
    @is_admin()
    async def serverstats(self, interaction: discord.Interaction):
        """View server stats"""
        guild = interaction.guild

        # Count various stats
        total_members = guild.member_count
        bots = sum(1 for member in guild.members if member.bot)
        humans = total_members - bots
        text_channels = len(guild.text_channels)
        voice_channels = len(guild.voice_channels)
        roles = len(guild.roles)

        embed = EmbedFactory.create(
            title=f"📊 Server Statistics - {guild.name}",
            color=EmbedColor.INFO,
            thumbnail=guild.icon.url if guild.icon else None,
            fields=[
                {"name": "👥 Total Members", "value": str(total_members), "inline": True},
                {"name": "🙋 Humans", "value": str(humans), "inline": True},
                {"name": "🤖 Bots", "value": str(bots), "inline": True},
                {"name": "💬 Text Channels", "value": str(text_channels), "inline": True},
                {"name": "🔊 Voice Channels", "value": str(voice_channels), "inline": True},
                {"name": "🎭 Roles", "value": str(roles), "inline": True},
                {"name": "👑 Owner", "value": guild.owner.mention if guild.owner else "Unknown", "inline": True},
                {"name": "📅 Created", "value": guild.created_at.strftime("%Y-%m-%d"), "inline": True},
                {"name": "🚀 Boost Level", "value": f"Level {guild.premium_tier}", "inline": True}
            ]
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Get information about a user (Admin)")
    @app_commands.describe(user="User to get info about")
    @is_admin()
    async def userinfo(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        """Get user information"""
        target = user or interaction.user

        roles = [role.mention for role in target.roles[1:]]  # Exclude @everyone
        roles_str = ", ".join(roles[:10]) if roles else "None"
        if len(roles) > 10:
            roles_str += f" (+{len(roles) - 10} more)"

        embed = EmbedFactory.create(
            title=f"User Information - {target.display_name}",
            color=target.color if target.color.value != 0 else EmbedColor.INFO,
            thumbnail=target.display_avatar.url,
            fields=[
                {"name": "Username", "value": str(target), "inline": True},
                {"name": "ID", "value": str(target.id), "inline": True},
                {"name": "Nickname", "value": target.nick or "None", "inline": True},
                {"name": "Account Created", "value": target.created_at.strftime("%Y-%m-%d"), "inline": True},
                {"name": "Joined Server", "value": target.joined_at.strftime("%Y-%m-%d") if target.joined_at else "Unknown", "inline": True},
                {"name": "Top Role", "value": target.top_role.mention, "inline": True},
                {"name": f"Roles ({len(roles)})", "value": roles_str, "inline": False}
            ]
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Get user's avatar (Admin)")
    @app_commands.describe(user="User to get avatar from")
    @is_admin()
    async def avatar(self, interaction: discord.Interaction, user: Optional[discord.Member] = None):
        """Get user avatar"""
        target = user or interaction.user

        embed = EmbedFactory.create(
            title=f"Avatar - {target.display_name}",
            color=EmbedColor.INFO,
            image=target.display_avatar.url
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Utility(bot, bot.db, bot.config))
