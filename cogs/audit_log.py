"""
Audit Log Cog for Logiq
Carl-bot-style audit logging for server activity and changes
"""

import asyncio
import logging
from collections import OrderedDict
from typing import Optional, Any

import discord
from discord import app_commands
from discord.ext import commands

from database.db_manager import DatabaseManager
from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin

logger = logging.getLogger(__name__)


class AuditLog(commands.Cog):
    """Audit logging for joins, leaves, server changes, and message activity"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get("modules", {}).get("audit_log", {})
        self.message_cache: OrderedDict[int, dict[str, Any]] = OrderedDict()
        self.cache_limit = 3000

    async def _get_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        """Resolve the configured audit/moderation log channel for a guild"""
        guild_config = await self.db.get_guild(guild.id)
        if not guild_config:
            return None

        log_channel_id = guild_config.get("log_channel")
        if not log_channel_id:
            return None

        channel = guild.get_channel(log_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        return None

    async def _send_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """Send an audit log embed if logging is configured"""
        if not self.module_config.get("enabled", True):
            return

        channel = await self._get_log_channel(guild)
        if not channel:
            return

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot send audit log embed in %s", guild)

    def _cache_message(self, message: discord.Message) -> None:
        """Store recent message details so deletion logs have useful context"""
        if not message.guild:
            return

        self.message_cache[message.id] = {
            "guild_id": message.guild.id,
            "channel_id": message.channel.id,
            "author_id": message.author.id,
            "author_name": str(message.author),
            "author_mention": message.author.mention,
            "content": message.content or "",
            "attachments": [attachment.url for attachment in message.attachments],
            "created_at": discord.utils.utcnow(),
        }

        while len(self.message_cache) > self.cache_limit:
            self.message_cache.popitem(last=False)

    def _truncate(self, value: Optional[str], limit: int = 900) -> str:
        """Trim long field values so they fit inside embeds"""
        if not value:
            return "None"
        value = value.strip()
        if not value:
            return "None"
        if len(value) <= limit:
            return value
        return f"{value[:limit - 3]}..."

    def _format_channel(self, channel: discord.abc.GuildChannel) -> str:
        """Human-friendly channel description"""
        channel_type = channel.__class__.__name__.replace("Channel", "")
        mention = getattr(channel, "mention", f"#{channel.name}")
        return f"{mention} ({channel_type})"

    def _format_target(self, target: Any) -> str:
        """Human-friendly target label for permission overwrites"""
        if isinstance(target, discord.Role):
            return f"Role: @{target.name}"
        if isinstance(target, (discord.Member, discord.User)):
            return f"Member: {target}"
        return str(target)

    def _format_permission_names(self, before: discord.Permissions, after: discord.Permissions) -> str:
        """Summarize changed role permissions"""
        changes = []
        for perm_name, before_value in before:
            after_value = getattr(after, perm_name, before_value)
            if before_value != after_value:
                state = "enabled" if after_value else "disabled"
                changes.append(f"`{perm_name}` {state}")
        return ", ".join(changes[:12]) if changes else "No permission changes"

    def _format_overwrite_changes(
        self,
        before_overwrites: dict[Any, discord.PermissionOverwrite],
        after_overwrites: dict[Any, discord.PermissionOverwrite],
    ) -> str:
        """Summarize overwrite changes on channel/category updates"""
        lines = []
        all_targets = list(dict.fromkeys(list(before_overwrites.keys()) + list(after_overwrites.keys())))

        for target in all_targets:
            before_pairs = dict(before_overwrites.get(target, discord.PermissionOverwrite()))
            after_pairs = dict(after_overwrites.get(target, discord.PermissionOverwrite()))

            changed = []
            all_perm_names = sorted(set(before_pairs.keys()) | set(after_pairs.keys()))
            for perm_name in all_perm_names:
                before_value = before_pairs.get(perm_name)
                after_value = after_pairs.get(perm_name)
                if before_value != after_value:
                    changed.append(f"{perm_name}: {before_value} -> {after_value}")

            if changed:
                lines.append(f"{self._format_target(target)}: {', '.join(changed[:5])}")

        if not lines:
            return "No overwrite changes detected"

        summary = "\n".join(lines[:6])
        return self._truncate(summary, 1000)

    def _format_actor(self, entry: Optional[discord.AuditLogEntry]) -> str:
        """Format actor and reason from an audit log entry"""
        if not entry:
            return "Unknown"

        actor = entry.user.mention if entry.user else "Unknown"
        if entry.reason:
            return f"{actor}\nReason: {self._truncate(entry.reason, 250)}"
        return actor

    async def _find_audit_entry(
        self,
        guild: discord.Guild,
        action_name: str,
        target_id: Optional[int] = None,
        retry_count: int = 3,
    ) -> Optional[discord.AuditLogEntry]:
        """Find the most recent matching audit log entry for a target"""
        me = guild.me or guild.get_member(self.bot.user.id)
        if not me or not me.guild_permissions.view_audit_log:
            return None

        action = getattr(discord.AuditLogAction, action_name, None)
        if action is None:
            return None

        for attempt in range(retry_count):
            try:
                async for entry in guild.audit_logs(limit=6, action=action):
                    entry_target_id = getattr(entry.target, "id", None)
                    if target_id is not None and entry_target_id != target_id:
                        continue

                    if (discord.utils.utcnow() - entry.created_at).total_seconds() <= 20:
                        return entry
            except discord.Forbidden:
                return None
            except Exception:
                logger.exception("Failed to inspect audit logs for %s in %s", action_name, guild)
                return None

            if attempt < retry_count - 1:
                await asyncio.sleep(0.6)

        return None

    @app_commands.command(name="auditlog-status", description="Show current audit logging coverage")
    @is_admin()
    async def auditlog_status(self, interaction: discord.Interaction):
        """Show whether the current guild has an audit log channel set"""
        guild_config = await self.db.get_guild(interaction.guild.id)
        log_channel_value = "Not set"
        if guild_config and guild_config.get("log_channel"):
            log_channel_value = f"<#{guild_config['log_channel']}>"

        embed = EmbedFactory.create(
            title="🧾 Audit Log Status",
            color=EmbedColor.INFO,
            description=(
                "Audit logging uses the existing server log channel configured with `/setlogchannel`."
            ),
            fields=[
                {"name": "Log Channel", "value": log_channel_value, "inline": False},
                {
                    "name": "Coverage",
                    "value": (
                        "Member joins/leaves, kick/ban detection, message edits/deletions, "
                        "channel/category changes, role changes, member updates, and bot auto-mod actions"
                    ),
                    "inline": False,
                },
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Cache messages so delete logs retain useful context"""
        if message.author.bot or not message.guild:
            return
        self._cache_message(message)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Log member joins"""
        embed = EmbedFactory.create(
            title="📥 Member Joined",
            description=f"{member.mention} joined the server",
            color=EmbedColor.SUCCESS,
            thumbnail=member.display_avatar.url,
            fields=[
                {"name": "Member", "value": f"{member} ({member.id})", "inline": False},
                {
                    "name": "Account Created",
                    "value": discord.utils.format_dt(member.created_at, style="F"),
                    "inline": False,
                },
            ],
        )
        await self._send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Log leaves, but classify recent kicks as moderation events"""
        kick_entry = await self._find_audit_entry(member.guild, "kick", member.id)
        if kick_entry:
            embed = EmbedFactory.create(
                title="🥾 Member Kicked",
                description=f"{member} was kicked from the server",
                color=EmbedColor.WARNING,
                thumbnail=member.display_avatar.url,
                fields=[
                    {"name": "Member", "value": f"{member} ({member.id})", "inline": False},
                    {"name": "Actor", "value": self._format_actor(kick_entry), "inline": False},
                ],
            )
            await self._send_log(member.guild, embed)
            return

        embed = EmbedFactory.create(
            title="📤 Member Left",
            description=f"{member} left the server",
            color=EmbedColor.WARNING,
            thumbnail=member.display_avatar.url,
            fields=[
                {"name": "Member", "value": f"{member} ({member.id})", "inline": False},
                {
                    "name": "Joined Server",
                    "value": discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "Unknown",
                    "inline": False,
                },
            ],
        )
        await self._send_log(member.guild, embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        """Log bans, including bans not triggered by this bot"""
        entry = await self._find_audit_entry(guild, "ban", user.id)
        embed = EmbedFactory.create(
            title="🔨 Member Banned",
            description=f"{user} was banned",
            color=EmbedColor.ERROR,
            fields=[
                {"name": "User", "value": f"{user} ({user.id})", "inline": False},
                {"name": "Actor", "value": self._format_actor(entry), "inline": False},
            ],
        )
        await self._send_log(guild, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        """Log unbans, including unbans not triggered by this bot"""
        entry = await self._find_audit_entry(guild, "unban", user.id)
        embed = EmbedFactory.create(
            title="🔓 Member Unbanned",
            description=f"{user} was unbanned",
            color=EmbedColor.SUCCESS,
            fields=[
                {"name": "User", "value": f"{user} ({user.id})", "inline": False},
                {"name": "Actor", "value": self._format_actor(entry), "inline": False},
            ],
        )
        await self._send_log(guild, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Log meaningful message edits"""
        if before.author.bot or not before.guild:
            return

        if before.content == after.content:
            return

        self._cache_message(after)

        embed = EmbedFactory.create(
            title="✏️ Message Edited",
            color=EmbedColor.INFO,
            fields=[
                {"name": "Author", "value": before.author.mention, "inline": True},
                {"name": "Channel", "value": before.channel.mention, "inline": True},
                {
                    "name": "Jump",
                    "value": f"[Open Message]({after.jump_url})",
                    "inline": True,
                },
                {"name": "Before", "value": self._truncate(before.content), "inline": False},
                {"name": "After", "value": self._truncate(after.content), "inline": False},
            ],
        )
        await self._send_log(before.guild, embed)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Log message deletions, even when the message is no longer cached by Discord"""
        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        cached = self.message_cache.pop(payload.message_id, None)
        channel = guild.get_channel(payload.channel_id)
        log_channel = await self._get_log_channel(guild)
        if log_channel and channel and channel.id == log_channel.id and cached and cached.get("author_id") == self.bot.user.id:
            return

        fields = [
            {
                "name": "Channel",
                "value": channel.mention if isinstance(channel, discord.TextChannel) else f"<#{payload.channel_id}>",
                "inline": True,
            },
            {"name": "Message ID", "value": str(payload.message_id), "inline": True},
        ]

        if cached:
            fields.insert(0, {"name": "Author", "value": f"{cached['author_mention']} ({cached['author_id']})", "inline": False})
            fields.append({"name": "Content", "value": self._truncate(cached.get("content")), "inline": False})
            attachments = cached.get("attachments") or []
            if attachments:
                fields.append({"name": "Attachments", "value": "\n".join(attachments[:4]), "inline": False})
        else:
            fields.insert(0, {"name": "Author", "value": "Unknown (message was not cached)", "inline": False})

        entry = await self._find_audit_entry(guild, "message_delete")
        if entry and getattr(entry.extra, "channel", None) and entry.extra.channel.id == payload.channel_id:
            fields.append({"name": "Deleted By", "value": self._format_actor(entry), "inline": False})

        embed = EmbedFactory.create(
            title="🗑️ Message Deleted",
            color=EmbedColor.WARNING,
            fields=fields,
        )
        await self._send_log(guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        """Log channel and category creation"""
        entry = await self._find_audit_entry(channel.guild, "channel_create", channel.id)
        embed = EmbedFactory.create(
            title="🆕 Channel Created",
            description=self._format_channel(channel),
            color=EmbedColor.SUCCESS,
            fields=[
                {"name": "Actor", "value": self._format_actor(entry), "inline": False},
            ],
        )
        await self._send_log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        """Log channel and category deletion"""
        entry = await self._find_audit_entry(channel.guild, "channel_delete", channel.id)
        embed = EmbedFactory.create(
            title="🗑️ Channel Deleted",
            description=self._format_channel(channel),
            color=EmbedColor.ERROR,
            fields=[
                {"name": "Actor", "value": self._format_actor(entry), "inline": False},
            ],
        )
        await self._send_log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        """Log channel/category edits and overwrite changes"""
        changes = []

        if before.name != after.name:
            changes.append(f"Name: `{before.name}` -> `{after.name}`")
        if getattr(before, "category_id", None) != getattr(after, "category_id", None):
            changes.append(f"Category: `{getattr(before.category, 'name', 'None')}` -> `{getattr(after.category, 'name', 'None')}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append("Topic updated")
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            changes.append(f"Slowmode: `{getattr(before, 'slowmode_delay', 0)}` -> `{getattr(after, 'slowmode_delay', 0)}` seconds")
        if getattr(before, "nsfw", None) != getattr(after, "nsfw", None):
            changes.append(f"NSFW: `{getattr(before, 'nsfw', False)}` -> `{getattr(after, 'nsfw', False)}`")
        if getattr(before, "position", None) != getattr(after, "position", None):
            changes.append(f"Position: `{before.position}` -> `{after.position}`")

        overwrite_summary = None
        if before.overwrites != after.overwrites:
            overwrite_summary = self._format_overwrite_changes(before.overwrites, after.overwrites)
            changes.append("Permission overwrites changed")

        if not changes:
            return

        entry = await self._find_audit_entry(after.guild, "channel_update", after.id)
        fields = [
            {"name": "Channel", "value": self._format_channel(after), "inline": False},
            {"name": "Actor", "value": self._format_actor(entry), "inline": False},
            {"name": "Changes", "value": "\n".join(changes[:8]), "inline": False},
        ]
        if overwrite_summary:
            fields.append({"name": "Overwrite Details", "value": overwrite_summary, "inline": False})

        embed = EmbedFactory.create(
            title="🛠️ Channel Updated",
            color=EmbedColor.INFO,
            fields=fields,
        )
        await self._send_log(after.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        """Log role creation"""
        entry = await self._find_audit_entry(role.guild, "role_create", role.id)
        embed = EmbedFactory.create(
            title="🆕 Role Created",
            color=EmbedColor.SUCCESS,
            fields=[
                {"name": "Role", "value": f"{role.mention} ({role.id})", "inline": False},
                {"name": "Actor", "value": self._format_actor(entry), "inline": False},
            ],
        )
        await self._send_log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        """Log role deletion"""
        entry = await self._find_audit_entry(role.guild, "role_delete", role.id)
        embed = EmbedFactory.create(
            title="🗑️ Role Deleted",
            color=EmbedColor.ERROR,
            fields=[
                {"name": "Role", "value": f"@{role.name} ({role.id})", "inline": False},
                {"name": "Actor", "value": self._format_actor(entry), "inline": False},
            ],
        )
        await self._send_log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        """Log role updates, especially permission changes"""
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` -> `{after.name}`")
        if before.color != after.color:
            changes.append(f"Color: `{before.color}` -> `{after.color}`")
        if before.hoist != after.hoist:
            changes.append(f"Display separately: `{before.hoist}` -> `{after.hoist}`")
        if before.mentionable != after.mentionable:
            changes.append(f"Mentionable: `{before.mentionable}` -> `{after.mentionable}`")
        if before.position != after.position:
            changes.append(f"Position: `{before.position}` -> `{after.position}`")
        permission_changes = None
        if before.permissions != after.permissions:
            permission_changes = self._format_permission_names(before.permissions, after.permissions)
            changes.append("Role permissions changed")

        if not changes:
            return

        entry = await self._find_audit_entry(after.guild, "role_update", after.id)
        fields = [
            {"name": "Role", "value": after.mention, "inline": False},
            {"name": "Actor", "value": self._format_actor(entry), "inline": False},
            {"name": "Changes", "value": "\n".join(changes[:8]), "inline": False},
        ]
        if permission_changes:
            fields.append({"name": "Permission Details", "value": self._truncate(permission_changes, 1000), "inline": False})

        embed = EmbedFactory.create(
            title="🛡️ Role Updated",
            color=EmbedColor.INFO,
            fields=fields,
        )
        await self._send_log(after.guild, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Log role, nickname, and timeout changes on members"""
        changes = []
        added_roles = [role for role in after.roles if role not in before.roles and role != after.guild.default_role]
        removed_roles = [role for role in before.roles if role not in after.roles and role != before.guild.default_role]

        if added_roles:
            changes.append("Added roles: " + ", ".join(role.mention for role in added_roles[:8]))
        if removed_roles:
            changes.append("Removed roles: " + ", ".join(role.mention for role in removed_roles[:8]))
        if before.nick != after.nick:
            changes.append(f"Nickname: `{before.nick or before.name}` -> `{after.nick or after.name}`")
        if before.timed_out_until != after.timed_out_until:
            changes.append(
                "Timeout: "
                f"`{discord.utils.format_dt(before.timed_out_until, style='R') if before.timed_out_until else 'None'}` -> "
                f"`{discord.utils.format_dt(after.timed_out_until, style='R') if after.timed_out_until else 'None'}`"
            )

        if not changes:
            return

        entry = await self._find_audit_entry(after.guild, "member_update", after.id)
        if entry is None and (added_roles or removed_roles):
            entry = await self._find_audit_entry(after.guild, "member_role_update", after.id)

        embed = EmbedFactory.create(
            title="👤 Member Updated",
            color=EmbedColor.INFO,
            thumbnail=after.display_avatar.url,
            fields=[
                {"name": "Member", "value": f"{after.mention} ({after.id})", "inline": False},
                {"name": "Actor", "value": self._format_actor(entry), "inline": False},
                {"name": "Changes", "value": "\n".join(changes[:8]), "inline": False},
            ],
        )
        await self._send_log(after.guild, embed)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(AuditLog(bot, bot.db, bot.config))
