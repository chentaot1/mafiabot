"""Discord UI: wills and leaderboard views."""

from __future__ import annotations

from bot_app.imports import *  # noqa: F403
from bot_app.instance import ALLOWED_GUILD_ID, bot, only_during_night_gameplay
from bot_app.shared import _GUILD_UNAVAILABLE_MSG, _build_leaderboard_embed, _build_server_stats_embed

# ==========================================
# LAST WILL (DM MODAL)
# ==========================================
class WillModal(discord.ui.Modal, title="Edit your Last Will"):
    will = discord.ui.TextInput(
        label="Last Will",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1800,
        placeholder="Type your will here...",
    )

    def __init__(self, *, current_text: str) -> None:
        super().__init__()
        self.will.default = (current_text or "")[:1800]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        game = get_game_by_player_id(interaction.user.id)
        if not game or not game.in_progress:
            return await interaction.response.send_message("No active game found for you.", ephemeral=True)
        guild = await resolve_game_guild(interaction.client, int(game.guild_id))
        if guild is None:
            return await interaction.response.send_message(
                _GUILD_UNAVAILABLE_MSG,
                ephemeral=True,
            )
        await game.sync_living_players(guild)
        living_ids = await game.get_living_ids(guild)
        if interaction.user.id not in living_ids:
            return await interaction.response.send_message(
                "You are not alive in this game — your will was not changed.",
                ephemeral=True,
            )
        state = game.role_states.setdefault(interaction.user.id, {})
        state["will"] = str(self.will.value or "")[:1800]
        await game.persist_flush()
        await interaction.response.send_message(tos_msg.will_saved(), ephemeral=True)


class WillView(discord.ui.View):
    def __init__(self, *, owner_id: int, current_text: str) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.current_text = current_text

    @discord.ui.button(label="Edit Will", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("This isn't your will editor.", ephemeral=True)
        game = get_game_by_player_id(interaction.user.id)
        if not game or not game.in_progress:
            return await interaction.response.send_message("No active game found for you.", ephemeral=True)
        guild = await resolve_game_guild(interaction.client, int(game.guild_id))
        if guild is None:
            return await interaction.response.send_message(
                _GUILD_UNAVAILABLE_MSG,
                ephemeral=True,
            )
        await game.sync_living_players(guild)
        living_ids = await game.get_living_ids(guild)
        if interaction.user.id not in living_ids:
            return await interaction.response.send_message(
                "You are not alive in this game — wills are locked.",
                ephemeral=True,
            )
        # Pull latest will text at click time (avoid overwriting with a stale prefill).
        latest = str(game.role_states.get(interaction.user.id, {}).get("will", "") or "")
        await interaction.response.send_modal(WillModal(current_text=latest))


# ==========================================
# LEADERBOARD UI (SLASH)
# ==========================================
class LeaderboardView(discord.ui.View):
    def __init__(self, *, invoker_id: int, guild_id: int, db: Database) -> None:
        super().__init__(timeout=300)
        self.invoker_id = int(invoker_id)
        self.guild_id = int(guild_id)
        self.db = db
        self.message: Optional[discord.Message] = None
        self.select = LeaderboardSelect(view=self)
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            try:
                await interaction.response.send_message("Only the command invoker can use these controls.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class LeaderboardSelect(discord.ui.Select):
    def __init__(self, *, view: LeaderboardView) -> None:
        self._lb_view = view
        options = [
            discord.SelectOption(label="Total wins (incl. personal)", value="total", description="Top total wins"),
            discord.SelectOption(label="Winrate (min 5 games)", value="winrate", description="Wins / games"),
            discord.SelectOption(label="Town wins", value="town", description="Final Town wins"),
            discord.SelectOption(label="Mafia wins", value="mafia", description="Final Mafia wins"),
            discord.SelectOption(label="Arsonist wins", value="arsonist", description="Final Arsonist wins"),
            discord.SelectOption(label="Pirate (personal)", value="pirate_win", description="2 kill-gated plunder wins"),
            discord.SelectOption(label="Executioner (personal)", value="exe_win", description="Target lynched"),
            discord.SelectOption(label="Jester (personal)", value="jester_win", description="Lynched"),
            discord.SelectOption(label="Survivor (personal)", value="survivor_survived", description="Survived to end"),
            discord.SelectOption(label="Chaos (personal)", value="chaos_survived", description="Survived to end"),
            discord.SelectOption(
                label="Witch (personal)",
                value="witch_town_loses",
                description="Alive when Town/Mafia/Arso/SK wins",
            ),
            discord.SelectOption(label="Arsonist (personal)", value="arsonist_win", description="Won as Arsonist"),
            discord.SelectOption(label="Guardian Angel (personal)", value="guardian_angel_win", description="Joint bind win or stalemate override"),
            discord.SelectOption(label="Serial Killer (personal)", value="serial_killer_win", description="Won as SK"),
        ]
        super().__init__(placeholder="Choose leaderboard…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        # Acknowledge quickly to avoid "interaction failed" under slow disk/locked DB.
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return
        key = str(self.values[0])
        v = self._lb_view
        embed = await _build_leaderboard_embed(db=v.db, guild_id=v.guild_id, page=key)
        try:
            await interaction.edit_original_response(embed=embed, view=v)
        except discord.HTTPException:
            # Audit #11: don't silently swallow — if Discord rate-limits or the
            # embed becomes invalid the user sees stale data with no log trail.
            logging.exception(
                "LeaderboardSelect edit_original_response failed (page=%s guild_id=%s)",
                key,
                v.guild_id,
            )


# ==========================================
# SERVER STATS UI (SLASH)
# ==========================================
class ServerStatsView(discord.ui.View):
    def __init__(self, *, invoker_id: int, guild_id: int, db: Database) -> None:
        super().__init__(timeout=300)
        self.invoker_id = int(invoker_id)
        self.guild_id = int(guild_id)
        self.db = db
        self.message: Optional[discord.Message] = None
        self.select = ServerStatsSelect(view=self)
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            try:
                await interaction.response.send_message("Only the command invoker can use these controls.", ephemeral=True)
            except discord.HTTPException:
                pass
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ServerStatsSelect(discord.ui.Select):
    def __init__(self, *, view: ServerStatsView) -> None:
        self._stats_view = view
        options = [
            discord.SelectOption(label="Overview", value="overview", description="Games, outcomes, top roles"),
            discord.SelectOption(label="Factions", value="factions", description="Town / Mafia / Neutral winrate"),
            discord.SelectOption(label="Roles", value="roles", description="Winrate for every role"),
            discord.SelectOption(label="Death causes", value="deaths", description="How players died"),
            discord.SelectOption(label="Personal wins", value="personal", description="Special win totals"),
        ]
        super().__init__(placeholder="Choose stats page…", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        try:
            await interaction.response.defer()
        except discord.HTTPException:
            return
        key = str(self.values[0])
        v = self._stats_view
        try:
            embed = await _build_server_stats_embed(db=v.db, guild_id=v.guild_id, page=key)
        except Exception:
            logging.exception(
                "ServerStatsSelect build embed failed (page=%s guild_id=%s)",
                key,
                v.guild_id,
            )
            await interaction.followup.send(
                "🛑 Could not load that stats page — try again or use `!refreshstatsboard`.",
                ephemeral=True,
            )
            return
        try:
            await interaction.edit_original_response(embed=embed, view=v)
        except discord.HTTPException:
            logging.exception(
                "ServerStatsSelect edit_original_response failed (page=%s guild_id=%s)",
                key,
                v.guild_id,
            )
            await interaction.followup.send(
                "🛑 Could not update the stats message (Discord limit or permissions).",
                ephemeral=True,
            )
