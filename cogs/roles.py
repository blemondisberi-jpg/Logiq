"""
Roles Cog for Logiq
Self-assignable roles with modal-based setup
"""

import colorsys
import re
import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional, List
import logging

from utils.embeds import EmbedFactory, EmbedColor
from utils.permissions import is_admin, PermissionChecker
from database.db_manager import DatabaseManager

logger = logging.getLogger(__name__)
MAX_COLOR_PANEL_OPTIONS = 125
COLOR_MENU_SIZE = 25
ROLE_MENU_COLLECTION = "role_menus"


def build_prefab_color_palette(count: int = MAX_COLOR_PANEL_OPTIONS) -> List[dict]:
    """Build a wide prefab color palette up to Discord's dropdown limit."""
    palette = []
    for index in range(count):
        hue = index / count
        saturation = 0.55 + (0.25 * ((index % 5) / 4))
        value = 0.72 + (0.18 * (((index // 5) % 5) / 4))
        red, green, blue = colorsys.hsv_to_rgb(hue, saturation, value)
        rgb = (int(red * 255), int(green * 255), int(blue * 255))
        hex_value = "#{:02X}{:02X}{:02X}".format(*rgb)
        palette.append({
            "name": f"Color {index + 1:03d}",
            "label": f"Color {index + 1:03d}",
            "hex": hex_value,
            "rgb": rgb
        })
    return palette


class RoleMenuSetupModal(discord.ui.Modal, title="Create Role Menu"):
    """Modal for creating role menus with custom settings"""

    title_input = discord.ui.TextInput(
        label="Menu Title",
        placeholder="e.g., Choose Your Roles",
        required=True,
        max_length=100
    )

    description_input = discord.ui.TextInput(
        label="Menu Description",
        placeholder="e.g., Select your preferred roles from the dropdown below",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500
    )

    role_mentions = discord.ui.TextInput(
        label="Roles (mention with @)",
        placeholder="Type @ and select roles. Example: @Gamer @Artist @Developer",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000
    )

    exclusive = discord.ui.TextInput(
        label="Exclusive? (yes/no)",
        placeholder="Type 'yes' if users can only pick ONE role",
        required=True,
        max_length=3
    )

    def __init__(self, cog, channel):
        super().__init__()
        self.cog = cog
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        """Handle modal submission"""
        import re
        
        # Parse exclusive setting
        is_exclusive = self.exclusive.value.lower() in ['yes', 'y', 'true']

        # Parse role mentions
        role_list = []
        text = self.role_mentions.value
        role_ids = re.findall(r'<@&(\d+)>', text)

        if not role_ids:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Roles Found", "Please mention roles using @. Type @ and select roles from the list that appears."),
                ephemeral=True
            )
            return

        for role_id in role_ids:
            role = interaction.guild.get_role(int(role_id))
            if role:
                # Skip @everyone and bot integration roles
                if role.is_default() or role.is_integration():
                    continue
                    
                role_emoji = None
                if role.unicode_emoji:
                    role_emoji = role.unicode_emoji
                elif role.icon:
                    role_emoji = str(role.icon)

                role_list.append({
                    'role': role,
                    'emoji': role_emoji or "🎭",
                    'label': role.name
                })

        if not role_list:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Valid Roles", f"Found {len(role_ids)} role mentions but they cannot be used (might be bot roles or @everyone)."),
                ephemeral=True
            )
            return

        if len(role_list) > 25:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Too Many Roles", "Discord allows maximum 25 options per menu."),
                ephemeral=True
            )
            return

        # Create embed
        embed = EmbedFactory.create(
            title=self.title_input.value,
            description=self.description_input.value or "Select your roles from the dropdown below.",
            color=EmbedColor.PRIMARY
        )

        # Add field showing available roles
        roles_text = "\n".join([f"{r['emoji']} {r['role'].mention}" for r in role_list])
        embed.add_field(
            name="Available Roles",
            value=roles_text,
            inline=False
        )

        # Create view
        if is_exclusive:
            view = ExclusiveRoleView(role_list, self.title_input.value)
        else:
            view = MultiRoleView(role_list)

        # Send to channel
        message = await self.channel.send(embed=embed, view=view)
        await self.cog._store_role_menu(
            guild_id=interaction.guild.id,
            channel_id=self.channel.id,
            message_id=message.id,
            menu_type="exclusive" if is_exclusive else "multi",
            category_name=self.title_input.value if is_exclusive else None,
            roles=[
                {
                    "role_id": role_info["role"].id,
                    "emoji": role_info["emoji"],
                    "label": role_info["label"]
                }
                for role_info in role_list
            ]
        )

        # Respond to interaction
        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Role Menu Created!",
                f"{'Exclusive' if is_exclusive else 'Multi-select'} role menu created in {self.channel.mention}"
            ),
            ephemeral=True
        )

        logger.info(f"Role menu created by {interaction.user} with {len(role_list)} roles")


class ExclusiveRoleSelect(discord.ui.Select):
    """Dropdown for exclusive role selection (pick only one)"""

    def __init__(self, role_data: List[dict], category_name: str):
        options = [
            discord.SelectOption(
                label=r['label'],
                description=f"Get the {r['label']} role",
                value=str(r['role'].id),
                emoji=r['emoji']
            )
            for r in role_data[:25]
        ]

        super().__init__(
            placeholder=f"Choose your option...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"exclusive_role_{category_name[:50]}"
        )
        self.role_ids = [r['role'].id for r in role_data]

    async def callback(self, interaction: discord.Interaction):
        """Handle exclusive role selection - LOCKED after first selection"""
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)

            # Check if user already has any role from THIS MENU ONLY
            user_has_role = False
            existing_role = None
            for role_id in self.role_ids:
                role = interaction.guild.get_role(role_id)
                if role and role in interaction.user.roles:
                    user_has_role = True
                    existing_role = role
                    break

            if user_has_role:
                await interaction.followup.send(
                    embed=EmbedFactory.error(
                        "🔒 Role Already Selected",
                        f"You already have **{existing_role.name}**. You cannot select another role from this menu."
                    ),
                    ephemeral=True
                )
                return

            selected_role_id = int(self.values[0])
            selected_role = interaction.guild.get_role(selected_role_id)

            if not selected_role:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Error", "Role not found"),
                    ephemeral=True
                )
                return

            # Give the selected role (only this one, no removing others)
            await interaction.user.add_roles(selected_role, reason="Exclusive role menu selection")

            embed = EmbedFactory.success(
                "✅ Role Selected!",
                f"You now have the **{selected_role.name}** role!\n\n"
                f"**Note:** You cannot select another role from this menu."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

            logger.info(f"{interaction.user} selected exclusive role {selected_role.name}")

        except discord.Forbidden:
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Error", "I don't have permission to manage your roles. Please contact an admin."),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in role selection: {e}", exc_info=True)
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Error", f"Failed to assign role: {str(e)}"),
                ephemeral=True
            )


class MultiRoleSelect(discord.ui.Select):
    """Dropdown menu for multiple role selection"""

    def __init__(self, role_data: List[dict]):
        options = [
            discord.SelectOption(
                label=r['label'],
                description=f"Toggle {r['label']} role",
                value=str(r['role'].id),
                emoji=r['emoji']
            )
            for r in role_data[:25]
        ]

        super().__init__(
            placeholder="Select roles to add/remove...",
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id="multi_role_select"
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle role selection"""
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)

            selected_role_ids = {int(value) for value in self.values}
            current_role_ids = {role.id for role in interaction.user.roles}

            roles_to_add = []
            roles_to_remove = []

            available_role_ids = {int(option.value) for option in self.options}

            for role_id in available_role_ids:
                role = interaction.guild.get_role(role_id)
                if not role:
                    continue

                if role_id in selected_role_ids and role_id not in current_role_ids:
                    roles_to_add.append(role)
                elif role_id not in selected_role_ids and role_id in current_role_ids:
                    roles_to_remove.append(role)

            if roles_to_add:
                await interaction.user.add_roles(*roles_to_add, reason="Role menu selection")
            if roles_to_remove:
                await interaction.user.remove_roles(*roles_to_remove, reason="Role menu deselection")

            changes = []
            if roles_to_add:
                changes.append(f"**Added:** {', '.join([r.name for r in roles_to_add])}")
            if roles_to_remove:
                changes.append(f"**Removed:** {', '.join([r.name for r in roles_to_remove])}")

            if not changes:
                changes.append("No changes made")

            embed = EmbedFactory.success(
                "✅ Roles Updated!",
                "\n".join(changes)
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

        except discord.Forbidden:
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Error", "I don't have permission to manage your roles. Please contact an admin."),
                ephemeral=True
            )
        except Exception as e:
            logger.error(f"Error in multi-role selection: {e}", exc_info=True)
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Error", f"Failed to update roles: {str(e)}"),
                ephemeral=True
            )


class ExclusiveRoleView(discord.ui.View):
    """View for exclusive role selection"""

    def __init__(self, role_data: List[dict], category_name: str):
        super().__init__(timeout=None)
        self.add_item(ExclusiveRoleSelect(role_data, category_name))


class MultiRoleView(discord.ui.View):
    """View for multi role selection"""

    def __init__(self, role_data: List[dict]):
        super().__init__(timeout=None)
        self.add_item(MultiRoleSelect(role_data))


class ColorRoleSelect(discord.ui.Select):
    """Dropdown for switching between generated colour roles."""

    def __init__(self, role_data: List[dict], all_role_ids: List[int], menu_index: int):
        options = [
            discord.SelectOption(
                label=role_info["label"],
                description=role_info["hex"],
                value=str(role_info["role"].id)
            )
            for role_info in role_data[:COLOR_MENU_SIZE]
        ]

        super().__init__(
            placeholder=f"Choose a colour ({menu_index + 1}/5)",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"color_role_select_{menu_index}"
        )
        self.all_role_ids = all_role_ids

    async def callback(self, interaction: discord.Interaction):
        """Switch the user's generated colour role."""
        try:
            await interaction.response.defer(ephemeral=True, thinking=False)

            selected_role_id = int(self.values[0])
            selected_role = interaction.guild.get_role(selected_role_id)
            if not selected_role:
                await interaction.followup.send(
                    embed=EmbedFactory.error("Error", "That colour role no longer exists."),
                    ephemeral=True
                )
                return

            current_color_roles = []
            for role_id in self.all_role_ids:
                role = interaction.guild.get_role(role_id)
                if role and role in interaction.user.roles:
                    current_color_roles.append(role)

            if len(current_color_roles) == 1 and current_color_roles[0].id == selected_role_id:
                await interaction.followup.send(
                    embed=EmbedFactory.info("Already Selected", f"You're already using **{selected_role.name}**."),
                    ephemeral=True
                )
                return

            roles_to_remove = [role for role in current_color_roles if role.id != selected_role_id]
            if roles_to_remove:
                await interaction.user.remove_roles(*roles_to_remove, reason="Color role panel switch")

            if selected_role not in interaction.user.roles:
                await interaction.user.add_roles(selected_role, reason="Color role panel selection")

            removed_text = ", ".join(role.name for role in roles_to_remove) if roles_to_remove else "None"
            embed = EmbedFactory.success(
                "Colour Updated",
                f"**Added:** {selected_role.name}\n**Removed:** {removed_text}"
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except discord.Forbidden:
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Error", "I don't have permission to manage your colour roles."),
                ephemeral=True
            )
        except Exception as error:
            logger.error("Error in colour role selection: %s", error, exc_info=True)
            sender = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await sender(
                embed=EmbedFactory.error("Error", f"Failed to update your colour role: {error}"),
                ephemeral=True
            )


class ColorRolePanelView(discord.ui.View):
    """Multi-dropdown panel for a large prefabricated colour palette."""

    def __init__(self, role_data: List[dict]):
        super().__init__(timeout=None)
        all_role_ids = [role_info["role"].id for role_info in role_data]
        for menu_index, start in enumerate(range(0, len(role_data), COLOR_MENU_SIZE)):
            chunk = role_data[start:start + COLOR_MENU_SIZE]
            if chunk:
                self.add_item(ColorRoleSelect(chunk, all_role_ids, menu_index))


class Roles(commands.Cog):
    """Role management cog"""

    def __init__(self, bot: commands.Bot, db: DatabaseManager, config: dict):
        self.bot = bot
        self.db = db
        self.config = config
        self.module_config = config.get('modules', {}).get('roles', {})
        # Register persistent views on startup
        self.bot.loop.create_task(self._register_persistent_views())
    
    async def _register_persistent_views(self):
        """Register persistent views for role menus"""
        await self.bot.wait_until_ready()
        stored_menus = await self.db.db[ROLE_MENU_COLLECTION].find({}).to_list(length=1000)
        restored = 0

        for menu in stored_menus:
            guild = self.bot.get_guild(menu.get("guild_id"))
            if guild is None:
                continue

            menu_type = menu.get("menu_type")
            if menu_type in {"exclusive", "multi"}:
                role_data = []
                for item in menu.get("roles", []):
                    role = guild.get_role(item.get("role_id"))
                    if role is None:
                        continue
                    role_data.append({
                        "role": role,
                        "emoji": item.get("emoji") or "🎭",
                        "label": item.get("label") or role.name
                    })

                if not role_data:
                    continue

                if menu_type == "exclusive":
                    view = ExclusiveRoleView(role_data, menu.get("category_name") or "role-menu")
                else:
                    view = MultiRoleView(role_data)
            elif menu_type == "color":
                role_data = []
                for item in menu.get("roles", []):
                    role = guild.get_role(item.get("role_id"))
                    if role is None:
                        continue
                    role_data.append({
                        "role": role,
                        "label": item.get("label") or role.name,
                        "hex": item.get("hex") or "#000000",
                        "rgb": tuple(item.get("rgb", (0, 0, 0)))
                    })

                if not role_data:
                    continue

                view = ColorRolePanelView(role_data)
            else:
                continue

            try:
                self.bot.add_view(view, message_id=int(menu["message_id"]))
                restored += 1
            except Exception as error:
                logger.warning("Failed to restore persistent role menu %s: %s", menu.get("message_id"), error)

        logger.info("Role menu persistent views ready (%s restored)", restored)

    async def _store_role_menu(
        self,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        menu_type: str,
        roles: List[dict],
        category_name: Optional[str] = None
    ) -> None:
        """Persist a role menu so its view survives bot restarts."""
        payload = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "message_id": message_id,
            "menu_type": menu_type,
            "roles": roles
        }
        if category_name is not None:
            payload["category_name"] = category_name

        await self.db.db[ROLE_MENU_COLLECTION].update_one(
            {"message_id": message_id},
            {"$set": payload},
            upsert=True
        )

    def _extract_role_ids_from_embed(self, embed: discord.Embed) -> List[int]:
        """Extract mentioned role IDs from a role-menu embed."""
        role_ids = []
        texts = []
        if embed.description:
            texts.append(embed.description)
        for field in embed.fields:
            texts.append(field.value or "")

        for text in texts:
            for role_id in re.findall(r"<@&(\d+)>", text):
                role_ids.append(int(role_id))

        deduped = []
        seen = set()
        for role_id in role_ids:
            if role_id not in seen:
                deduped.append(role_id)
                seen.add(role_id)
        return deduped

    def _build_color_role_regex(self, role_prefix: str) -> re.Pattern:
        """Return the generated colour-role naming pattern for a prefix."""
        return re.compile(rf"^{re.escape(role_prefix)} Color \d{{3}} #[0-9A-F]{{6}}$")

    async def _repair_role_menu_message(
        self,
        guild: discord.Guild,
        message: discord.Message
    ) -> tuple[bool, str]:
        """Re-bind an existing role menu or colour panel message."""
        if not message.components:
            return False, "That message has no interactive components to repair."
        if not message.embeds:
            return False, "That message has no embed content I can use to rebuild the menu."

        custom_ids = []
        for row in message.components:
            for child in getattr(row, "children", []):
                custom_id = getattr(child, "custom_id", None)
                if custom_id:
                    custom_ids.append(custom_id)

        if not custom_ids:
            return False, "I couldn't find any dropdown custom IDs on that message."

        embed = message.embeds[0]

        if any(custom_id.startswith("color_role_select_") for custom_id in custom_ids):
            description = embed.description or ""
            prefix_match = re.search(r"\*\*Role Prefix:\*\*\s*([^\n]+)", description)
            role_prefix = prefix_match.group(1).strip() if prefix_match else "Color"
            pattern = self._build_color_role_regex(role_prefix)
            role_data = []
            for role in sorted(guild.roles, key=lambda item: item.name):
                if pattern.match(role.name):
                    hex_match = re.search(r"(#[0-9A-F]{6})$", role.name)
                    role_data.append({
                        "role": role,
                        "label": role.name.split(" ")[1] + " " + role.name.split(" ")[2] if len(role.name.split(" ")) >= 3 else role.name,
                        "hex": hex_match.group(1) if hex_match else "#{:02X}{:02X}{:02X}".format(
                            role.colour.r, role.colour.g, role.colour.b
                        ),
                        "rgb": (role.colour.r, role.colour.g, role.colour.b)
                    })

            if not role_data:
                return False, f"I couldn't find any generated colour roles for prefix `{role_prefix}`."

            self.bot.add_view(ColorRolePanelView(role_data), message_id=message.id)
            await self._store_role_menu(
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                menu_type="color",
                roles=[
                    {
                        "role_id": role_info["role"].id,
                        "label": role_info["label"],
                        "hex": role_info["hex"],
                        "rgb": list(role_info["rgb"])
                    }
                    for role_info in role_data
                ]
            )
            return True, f"Repaired colour panel for prefix `{role_prefix}` with {len(role_data)} roles."

        role_ids = self._extract_role_ids_from_embed(embed)
        role_data = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            if role is None:
                continue
            role_data.append({
                "role": role,
                "emoji": "🎭",
                "label": role.name
            })

        if not role_data:
            return False, "I couldn't recover any roles from that menu message."

        exclusive_id = next((custom_id for custom_id in custom_ids if custom_id.startswith("exclusive_role_")), None)
        if exclusive_id:
            category_name = exclusive_id.removeprefix("exclusive_role_") or "role-menu"
            self.bot.add_view(ExclusiveRoleView(role_data, category_name), message_id=message.id)
            await self._store_role_menu(
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                menu_type="exclusive",
                category_name=category_name,
                roles=[
                    {
                        "role_id": role_info["role"].id,
                        "emoji": role_info["emoji"],
                        "label": role_info["label"]
                    }
                    for role_info in role_data
                ]
            )
            return True, f"Repaired exclusive role menu with {len(role_data)} roles."

        if "multi_role_select" in custom_ids:
            self.bot.add_view(MultiRoleView(role_data), message_id=message.id)
            await self._store_role_menu(
                guild_id=guild.id,
                channel_id=message.channel.id,
                message_id=message.id,
                menu_type="multi",
                roles=[
                    {
                        "role_id": role_info["role"].id,
                        "emoji": role_info["emoji"],
                        "label": role_info["label"]
                    }
                    for role_info in role_data
                ]
            )
            return True, f"Repaired multi-select role menu with {len(role_data)} roles."

        return False, "I couldn't determine what kind of role menu that message is."

    def _get_bot_member(self, guild: discord.Guild) -> Optional[discord.Member]:
        """Get the bot's guild member object."""
        return guild.me or guild.get_member(self.bot.user.id)

    def _can_manage_role(self, actor: discord.Member, role: discord.Role) -> tuple[bool, Optional[str]]:
        """Check whether a member can manage a specific role."""
        if role.is_default():
            return False, "The `@everyone` role cannot be mass-managed."
        if role.is_integration() or role.managed:
            return False, "Managed or integration roles cannot be mass-managed."
        if actor.guild.owner_id == actor.id:
            return True, None
        if actor.top_role <= role:
            return False, "That role is higher than or equal to your highest role."
        return True, None

    async def _build_or_fetch_color_roles(
        self,
        guild: discord.Guild,
        *,
        role_prefix: str
    ) -> List[dict]:
        """Create or reuse a prefabricated palette of colour roles."""
        palette = build_prefab_color_palette()
        role_data = []

        for entry in palette:
            role_name = f"{role_prefix} {entry['label']} {entry['hex']}"
            existing_role = discord.utils.get(guild.roles, name=role_name)
            role = existing_role

            if role is None:
                role = await guild.create_role(
                    name=role_name,
                    colour=discord.Colour.from_rgb(*entry["rgb"]),
                    mentionable=False,
                    reason="Prefabricated colour role panel setup"
                )
            role_data.append({
                "role": role,
                "label": entry["label"],
                "hex": entry["hex"],
                "rgb": entry["rgb"]
            })

        return role_data

    async def _mass_role_update(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        *,
        mode: str,
        skip_bots: bool,
        confirm: bool
    ):
        """Add or remove a role for many members with safety checks."""
        if not confirm:
            action = "add" if mode == "add" else "remove"
            await interaction.response.send_message(
                embed=EmbedFactory.warning(
                    "Confirmation Required",
                    f"This command affects many members. Re-run it with `confirm:true` to {action} {role.mention} {'to' if mode == 'add' else 'from'} all eligible members."
                ),
                ephemeral=True
            )
            return

        actor = interaction.user
        bot_member = self._get_bot_member(interaction.guild)
        if bot_member is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I couldn't resolve my bot member in this server."),
                ephemeral=True
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Missing Permissions", "I need the **Manage Roles** permission to do that."),
                ephemeral=True
            )
            return

        actor_can_manage, actor_error = self._can_manage_role(actor, role)
        if not actor_can_manage:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Role Hierarchy", actor_error),
                ephemeral=True
            )
            return

        bot_can_manage, bot_error = self._can_manage_role(bot_member, role)
        if not bot_can_manage:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Role Hierarchy", f"I can't manage {role.mention}. {bot_error}"),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        stats = {
            "processed": 0,
            "changed": 0,
            "already": 0,
            "missing": 0,
            "skipped_bots": 0,
            "skipped_hierarchy": 0,
            "failed": 0
        }

        reason = f"Mass role {mode} by {interaction.user} ({interaction.user.id})"
        for member in interaction.guild.members:
            if member.bot and skip_bots:
                stats["skipped_bots"] += 1
                continue

            if not PermissionChecker.check_hierarchy(bot_member, member):
                stats["skipped_hierarchy"] += 1
                continue

            has_role = role in member.roles
            if mode == "add" and has_role:
                stats["already"] += 1
                continue
            if mode == "remove" and not has_role:
                stats["missing"] += 1
                continue

            try:
                if mode == "add":
                    await member.add_roles(role, reason=reason)
                else:
                    await member.remove_roles(role, reason=reason)
                stats["changed"] += 1
                stats["processed"] += 1
            except discord.Forbidden:
                stats["failed"] += 1
            except discord.HTTPException:
                stats["failed"] += 1

        title = "Mass Role Add Complete" if mode == "add" else "Mass Role Remove Complete"
        verb = "added to" if mode == "add" else "removed from"
        embed = EmbedFactory.create(
            title=f"✅ {title}",
            color=EmbedColor.SUCCESS,
            fields=[
                {"name": "Role", "value": role.mention, "inline": True},
                {"name": "Action", "value": verb, "inline": True},
                {"name": "Changed", "value": str(stats["changed"]), "inline": True},
                {"name": "Already Had Role", "value": str(stats["already"]), "inline": True},
                {"name": "Didn't Have Role", "value": str(stats["missing"]), "inline": True},
                {"name": "Skipped Bots", "value": str(stats["skipped_bots"]), "inline": True},
                {"name": "Skipped Hierarchy", "value": str(stats["skipped_hierarchy"]), "inline": True},
                {"name": "Failed", "value": str(stats["failed"]), "inline": True}
            ]
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(
            "%s ran mass role %s for %s in %s: %s",
            interaction.user,
            mode,
            role,
            interaction.guild,
            stats
        )

    @app_commands.command(name="create-role-menu", description="Create a role menu (Admin)")
    @app_commands.describe(
        title="Title of the role menu",
        description="Description of the role menu",
        role1="First role",
        role2="Second role (optional)",
        role3="Third role (optional)",
        role4="Fourth role (optional)",
        role5="Fifth role (optional)",
        role6="Sixth role (optional)",
        role7="Seventh role (optional)",
        role8="Eighth role (optional)",
        role9="Ninth role (optional)",
        role10="Tenth role (optional)",
        exclusive="Can users only pick ONE role? (yes/no)",
        channel="Channel to send menu (optional)"
    )
    @is_admin()
    async def create_role_menu(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str,
        role1: discord.Role,
        exclusive: str,
        role2: Optional[discord.Role] = None,
        role3: Optional[discord.Role] = None,
        role4: Optional[discord.Role] = None,
        role5: Optional[discord.Role] = None,
        role6: Optional[discord.Role] = None,
        role7: Optional[discord.Role] = None,
        role8: Optional[discord.Role] = None,
        role9: Optional[discord.Role] = None,
        role10: Optional[discord.Role] = None,
        channel: Optional[discord.TextChannel] = None
    ):
        """Create role menu directly with slash command"""
        target_channel = channel or interaction.channel
        is_exclusive = exclusive.lower() in ['yes', 'y', 'true']
        
        # Collect all roles
        roles = [role1]
        if role2:
            roles.append(role2)
        if role3:
            roles.append(role3)
        if role4:
            roles.append(role4)
        if role5:
            roles.append(role5)
        if role6:
            roles.append(role6)
        if role7:
            roles.append(role7)
        if role8:
            roles.append(role8)
        if role9:
            roles.append(role9)
        if role10:
            roles.append(role10)
        
        # Build role list
        role_list = []
        for role in roles:
            if role.is_default() or role.is_integration():
                continue
            
            role_emoji = None
            if role.unicode_emoji:
                role_emoji = role.unicode_emoji
            elif role.icon:
                role_emoji = str(role.icon)
            
            role_list.append({
                'role': role,
                'emoji': role_emoji or "🎭",
                'label': role.name
            })
        
        if not role_list:
            await interaction.response.send_message(
                embed=EmbedFactory.error("No Valid Roles", "Please select valid roles."),
                ephemeral=True
            )
            return
        
        # Create embed
        embed = EmbedFactory.create(
            title=title,
            description=description,
            color=EmbedColor.PRIMARY
        )
        
        # Add field showing available roles
        roles_text = "\n".join([f"{r['emoji']} {r['role'].mention}" for r in role_list])
        embed.add_field(
            name="Available Roles",
            value=roles_text,
            inline=False
        )
        
        # Create view
        if is_exclusive:
            view = ExclusiveRoleView(role_list, title)
        else:
            view = MultiRoleView(role_list)
        
        # Send to channel
        message = await target_channel.send(embed=embed, view=view)
        await self._store_role_menu(
            guild_id=interaction.guild.id,
            channel_id=target_channel.id,
            message_id=message.id,
            menu_type="exclusive" if is_exclusive else "multi",
            category_name=title if is_exclusive else None,
            roles=[
                {
                    "role_id": role_info["role"].id,
                    "emoji": role_info["emoji"],
                    "label": role_info["label"]
                }
                for role_info in role_list
            ]
        )
        
        await interaction.response.send_message(
            embed=EmbedFactory.success(
                "Role Menu Created!",
                f"{'Exclusive' if is_exclusive else 'Multi-select'} role menu created in {target_channel.mention}"
            ),
            ephemeral=True
        )
        
        logger.info(f"Role menu created by {interaction.user} with {len(role_list)} roles")

    @app_commands.command(name="addrole", description="Add a role to a user (Admin)")
    @app_commands.describe(user="User to add role to", role="Role to add")
    @is_admin()
    async def add_role(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        """Add role to user"""
        if role in user.roles:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Already Has Role", f"{user.mention} already has {role.mention}"),
                ephemeral=True
            )
            return

        try:
            await user.add_roles(role)
            embed = EmbedFactory.success("Role Added", f"Added {role.mention} to {user.mention}")
            await interaction.response.send_message(embed=embed)
            logger.info(f"{interaction.user} added role {role} to {user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to manage roles"),
                ephemeral=True
            )

    @app_commands.command(name="massrole-add", description="Add a role to all eligible members (Admin)")
    @app_commands.describe(
        role="Role to add",
        skip_bots="Skip bot accounts (recommended)",
        confirm="Must be true to actually run"
    )
    @is_admin()
    async def massrole_add(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        skip_bots: bool = True,
        confirm: bool = False
    ):
        """Add a role to all eligible members."""
        await self._mass_role_update(
            interaction,
            role,
            mode="add",
            skip_bots=skip_bots,
            confirm=confirm
        )

    @app_commands.command(name="removerole", description="Remove a role from a user (Admin)")
    @app_commands.describe(user="User to remove role from", role="Role to remove")
    @is_admin()
    async def remove_role(self, interaction: discord.Interaction, user: discord.Member, role: discord.Role):
        """Remove role from user"""
        if role not in user.roles:
            await interaction.response.send_message(
                embed=EmbedFactory.info("Doesn't Have Role", f"{user.mention} doesn't have {role.mention}"),
                ephemeral=True
            )
            return

        try:
            await user.remove_roles(role)
            embed = EmbedFactory.success("Role Removed", f"Removed {role.mention} from {user.mention}")
            await interaction.response.send_message(embed=embed)
            logger.info(f"{interaction.user} removed role {role} from {user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I don't have permission to manage roles"),
                ephemeral=True
            )

    @app_commands.command(name="massrole-remove", description="Remove a role from all eligible members (Admin)")
    @app_commands.describe(
        role="Role to remove",
        skip_bots="Skip bot accounts (recommended)",
        confirm="Must be true to actually run"
    )
    @is_admin()
    async def massrole_remove(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        skip_bots: bool = True,
        confirm: bool = False
    ):
        """Remove a role from all eligible members."""
        await self._mass_role_update(
            interaction,
            role,
            mode="remove",
            skip_bots=skip_bots,
            confirm=confirm
        )

    @app_commands.command(name="create-color-panel", description="Create a prefabricated multi-dropdown colour role panel (Admin)")
    @app_commands.describe(
        channel="Channel to send the colour panel to",
        title="Panel title",
        description="Panel description",
        role_prefix="Prefix used when creating colour roles"
    )
    @is_admin()
    async def create_color_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        title: str = "Choose Your Colour",
        description: str = "Pick one colour role from the dropdowns below. You can switch colours any time.",
        role_prefix: str = "Color"
    ):
        """Create a prefabricated colour role panel up to Discord's hard dropdown limit."""
        target_channel = channel or interaction.channel
        bot_member = self._get_bot_member(interaction.guild)
        if bot_member is None:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Error", "I couldn't resolve my bot member in this server."),
                ephemeral=True
            )
            return

        if not bot_member.guild_permissions.manage_roles:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Missing Permissions", "I need the **Manage Roles** permission to create colour roles."),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            role_data = await self._build_or_fetch_color_roles(
                interaction.guild,
                role_prefix=role_prefix.strip() or "Color"
            )
        except discord.Forbidden:
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", "I don't have permission to create roles in this server."),
                ephemeral=True
            )
            return
        except discord.HTTPException as error:
            await interaction.followup.send(
                embed=EmbedFactory.error("Error", f"Discord rejected the colour role setup: {error}"),
                ephemeral=True
            )
            return

        embed = EmbedFactory.create(
            title=title,
            description=(
                f"{description}\n\n"
                f"**Palette Size:** {len(role_data)} colours across {len(role_data) // COLOR_MENU_SIZE} dropdowns\n"
                f"**Role Prefix:** {role_prefix.strip() or 'Color'}\n\n"
                "Each member can hold one generated colour role at a time."
            ),
            color=EmbedColor.PREMIUM
        )
        embed.set_footer(
            text="If another higher coloured role overrides these, move the generated colour roles higher in your role list."
        )

        view = ColorRolePanelView(role_data)
        message = await target_channel.send(embed=embed, view=view)
        await self._store_role_menu(
            guild_id=interaction.guild.id,
            channel_id=target_channel.id,
            message_id=message.id,
            menu_type="color",
            roles=[
                {
                    "role_id": role_info["role"].id,
                    "label": role_info["label"],
                    "hex": role_info["hex"],
                    "rgb": list(role_info["rgb"])
                }
                for role_info in role_data
            ]
        )

        await interaction.followup.send(
            embed=EmbedFactory.success(
                "Colour Panel Created",
                f"Created or reused **{len(role_data)}** colour roles and posted the panel in {target_channel.mention}."
            ),
            ephemeral=True
        )
        logger.info(
            "%s created a prefabricated colour panel with %s roles in %s",
            interaction.user,
            len(role_data),
            interaction.guild
        )

    @app_commands.command(name="repair-role-menu", description="Repair an existing role menu or colour panel message (Admin)")
    @app_commands.describe(
        channel="Channel containing the broken menu message",
        message_id="Message ID of the broken dropdown message"
    )
    @is_admin()
    async def repair_role_menu(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message_id: str
    ):
        """Re-bind a previously posted role menu or colour panel after restart/deploy issues."""
        try:
            parsed_message_id = int(message_id.strip())
        except ValueError:
            await interaction.response.send_message(
                embed=EmbedFactory.error("Invalid Message ID", "Please provide a numeric Discord message ID."),
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=False)

        try:
            message = await channel.fetch_message(parsed_message_id)
        except discord.NotFound:
            await interaction.followup.send(
                embed=EmbedFactory.error("Not Found", "I couldn't find a message with that ID in the selected channel."),
                ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                embed=EmbedFactory.error("Missing Permissions", "I can't read message history in that channel."),
                ephemeral=True
            )
            return
        except discord.HTTPException as error:
            await interaction.followup.send(
                embed=EmbedFactory.error("Fetch Failed", f"Discord rejected the message lookup: {error}"),
                ephemeral=True
            )
            return

        repaired, detail = await self._repair_role_menu_message(interaction.guild, message)
        if not repaired:
            await interaction.followup.send(
                embed=EmbedFactory.error("Repair Failed", detail),
                ephemeral=True
            )
            return

        await interaction.followup.send(
            embed=EmbedFactory.success("Role Menu Repaired", detail),
            ephemeral=True
        )
        logger.info("%s repaired role menu message %s in %s", interaction.user, message.id, interaction.guild)


async def setup(bot: commands.Bot):
    """Setup function for cog loading"""
    await bot.add_cog(Roles(bot, bot.db, bot.config))
