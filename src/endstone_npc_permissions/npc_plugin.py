"""
NPC Permissions Plugin for Endstone
Allows non-operator players in creative mode with specific permissions
to configure NPCs via temporary operator grant with command blocking.
"""

import json
import time
from pathlib import Path
from typing import Dict, Set

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin
from endstone.event import (
    event_handler,
    PlayerInteractActorEvent,
    PlayerCommandEvent,
    PlayerMoveEvent,
    PlayerQuitEvent,
)


class NpcPermissionsPlugin(Plugin):
    
    api_version = "0.11"

    # Command definitions
    commands = {
        "npc": {
            "description": "NPC Permissions management",
            "usages": [
                "/npc grant <player: str>",
                "/npc revoke <player: str>",
                "/npc list"
            ],
            "permissions": ["npcperm.admin"]
        }
    }

    # Permission definitions
    permissions = {
        "npcperm.admin": {
            "description": "Manage NPC permissions",
            "default": "op"
        }
    }

    # Configuration
    TIMEOUT_SECONDS = 120  # Auto-revoke after this many seconds
    MOVE_THRESHOLD = 6     # Revoke if player moves this many blocks from start position
    DATA_DIR = "plugins/npc_permissions_data"

    def __init__(self):
        super().__init__()
        # Track active temp-op sessions: player_name -> session_data
        self.active_sessions: Dict[str, dict] = {}
        # Track players with NPC permission
        self.permitted_players: Set[str] = set()

    def on_load(self) -> None:
        # Ensure data directory exists
        Path(self.DATA_DIR).mkdir(parents=True, exist_ok=True)
        self._load_permitted_players()

    def on_enable(self) -> None:
        self.register_events(self)
        self.logger.info("NPC Permissions enabled")
        self.logger.info(f"Permitted players: {len(self.permitted_players)}")

    def on_disable(self) -> None:
        # Cleanup: revoke all active sessions
        for player_name in list(self.active_sessions.keys()):
            self._revoke_temp_op(player_name)
        self._save_permitted_players()
        self.logger.info("NPC Permissions disabled")

    # -------------------------------------------------------------------------
    # Data Persistence
    # -------------------------------------------------------------------------

    def _load_permitted_players(self) -> None:
        """Load permitted players from JSON file."""
        data_file = Path(self.DATA_DIR) / "permitted_players.json"
        if data_file.exists():
            try:
                with open(data_file, "r") as f:
                    self.permitted_players = set(json.load(f))
            except Exception as e:
                self.logger.warning(f"Failed to load permitted players: {e}")
                self.permitted_players = set()
        else:
            self.permitted_players = set()

    def _save_permitted_players(self) -> None:
        """Save permitted players to JSON file."""
        data_file = Path(self.DATA_DIR) / "permitted_players.json"
        try:
            with open(data_file, "w") as f:
                json.dump(list(self.permitted_players), f, indent=2)
        except Exception as e:
            self.logger.error(f"Failed to save permitted players: {e}")

    # -------------------------------------------------------------------------
    # Command Handler
    # -------------------------------------------------------------------------

    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name.lower() != "npc":
            return False

        if not args:
            sender.send_message("§eUsage: /npc <grant|revoke|list> [player]")
            return True

        subcommand = args[0].lower()

        if subcommand == "grant":
            if len(args) < 2:
                sender.send_message("§cUsage: /npc grant <player>")
                return True
            player_name = args[1]
            self.permitted_players.add(player_name)
            self._save_permitted_players()
            sender.send_message(f"§aGranted NPC permission to {player_name}")

            # Notify player if online
            player = self.server.get_player(player_name)
            if player:
                player.send_message("§aYou now have permission to configure NPCs!")
            return True

        elif subcommand == "revoke":
            if len(args) < 2:
                sender.send_message("§cUsage: /npc revoke <player>")
                return True
            player_name = args[1]
            if player_name in self.permitted_players:
                self.permitted_players.discard(player_name)
                self._save_permitted_players()
                sender.send_message(f"§eRevoked NPC permission from {player_name}")
            else:
                sender.send_message(f"§c{player_name} doesn't have NPC permissions")
            return True

        elif subcommand == "list":
            if not self.permitted_players:
                sender.send_message("§7No players have NPC permissions.")
            else:
                sender.send_message("§aPlayers with NPC permissions:")
                for name in sorted(self.permitted_players):
                    sender.send_message(f"§7- {name}")
            return True

        else:
            sender.send_message("§eUsage: /npc <grant|revoke|list> [player]")
            return True

    # -------------------------------------------------------------------------
    # Event Handlers
    # -------------------------------------------------------------------------

    @event_handler
    def on_player_interact_actor(self, event: PlayerInteractActorEvent) -> None:
        """Grant temp op when a permitted creative player interacts with an NPC."""
        player = event.player
        actor = event.actor

        # Check if the actor is an NPC
        actor_type = str(actor.type).lower()
        if "npc" not in actor_type:
            return

        # Already an operator - allow normal access
        if player.is_op and player.name not in self.active_sessions:
            return

        # Already in an active session - allow continued access
        if player.name in self.active_sessions:
            return

        # Not in creative mode - let vanilla NPC dialog pass through normally
        game_mode = str(player.game_mode).lower()
        if "creative" not in game_mode:
            return

        # In creative but not permitted - let vanilla NPC dialog pass through
        # (they'll see the normal NPC menu, just can't edit it)
        if player.name not in self.permitted_players:
            return

        # Grant temporary operator status
        npc_loc = actor.location
        self._grant_temp_op(player, npc_loc)

        # Cancel this event - the op grant happens too late for the current action
        # Player needs to click again (now that they're op, it will work)
        event.cancel()
        player.send_message("§7Click again to configure the NPC.")

    @event_handler
    def on_player_command(self, event: PlayerCommandEvent) -> None:
        """Block ALL commands during an active temp-op session."""
        if event.player.name in self.active_sessions:
            event.cancel()
            event.player.send_message("§cCommands are disabled while configuring NPCs.")

    @event_handler
    def on_player_move(self, event: PlayerMoveEvent) -> None:
        """Revoke temp op if player moves too far from their start position."""
        player = event.player
        if player.name not in self.active_sessions:
            return

        session = self.active_sessions[player.name]
        start_pos = session["start_pos"]

        # Get current foot position
        loc = player.location
        cur_x, cur_y, cur_z = loc.x, loc.y, loc.z

        # Skip if only head rotation (feet haven't moved since last check)
        last = session.get("last_pos")
        if last and abs(cur_x - last[0]) < 0.01 and abs(cur_y - last[1]) < 0.01 and abs(cur_z - last[2]) < 0.01:
            return  # Head rotation only — ignore
        session["last_pos"] = (cur_x, cur_y, cur_z)

        # Calculate distance from where they started the session
        dx = abs(cur_x - start_pos[0])
        dy = abs(cur_y - start_pos[1])
        dz = abs(cur_z - start_pos[2])

        # Revoke if moved beyond threshold
        if dx > self.MOVE_THRESHOLD or dy > self.MOVE_THRESHOLD or dz > self.MOVE_THRESHOLD:
            player.send_message("§eMoved away from NPC — access revoked.")
            self._revoke_temp_op(player.name)

    @event_handler
    def on_player_quit(self, event: PlayerQuitEvent) -> None:
        """Clean up session when player disconnects."""
        player_name = event.player.name
        if player_name in self.active_sessions:
            self._revoke_temp_op(player_name)

    # -------------------------------------------------------------------------
    # Session Management
    # -------------------------------------------------------------------------

    def _grant_temp_op(self, player, npc_loc) -> None:
        """Grant temporary operator status to a player using /op command."""
        player_loc = player.location
        self.active_sessions[player.name] = {
            "original_op": player.is_op,
            "granted_at": time.time(),
            "npc_pos": (int(npc_loc.x), int(npc_loc.y), int(npc_loc.z)),
            "start_pos": (player_loc.x, player_loc.y, player_loc.z),
            "last_pos": (player_loc.x, player_loc.y, player_loc.z),
        }

        # Use /op command to grant server's configured permission level (usually 2)
        # is_op property only grants level 1, but NPC configuration needs level 2
        self.server.dispatch_command(
            self.server.command_sender,
            f'op "{player.name}"'
        )

        player.send_message("§aTemporary NPC access granted.")
        player.send_message("§7Commands are disabled. Move away or wait to end session.")

        # Schedule auto-revoke
        self.server.scheduler.run_task(
            self,
            lambda: self._timeout_revoke(player.name),
            delay=self.TIMEOUT_SECONDS * 20  # Convert seconds to ticks
        )

        self.logger.info(f"Granted temp-op to {player.name} for NPC access")

    def _timeout_revoke(self, player_name: str) -> None:
        """Called by scheduler - revoke if session still active."""
        if player_name in self.active_sessions:
            player = self.server.get_player(player_name)
            if player:
                player.send_message("§eNPC session timed out.")
            self._revoke_temp_op(player_name)

    def _revoke_temp_op(self, player_name: str) -> None:
        """Revoke temporary operator status from a player using /deop command."""
        if player_name not in self.active_sessions:
            return

        session = self.active_sessions.pop(player_name)
        player = self.server.get_player(player_name)

        # Only deop if they weren't originally an op
        if not session["original_op"]:
            self.server.dispatch_command(
                self.server.command_sender,
                f'deop "{player_name}"'
            )

        if player:
            player.send_message("§eNPC access revoked.")

        self.logger.info(f"Revoked temp-op from {player_name}")
