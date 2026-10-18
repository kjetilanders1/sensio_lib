"""Hub for local control of the Sensio smart house system."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from .light import Light, LightState
from .messages import Message, RsnMessage
from .scene import Scene
from .socket_manager import SocketManager


class Hub:
    """Local controller for a Sensio smart house system.

    Manages the socket connection to the physical controller and provides
    access to lights and scenes. Device data is supplied via connect(),
    typically from cached data originally fetched through SensioApi.
    """

    def __init__(
        self,
        server_address: str,
        socket_manager: SocketManager | None = None,
        availability_grace: float = 15.0,
    ) -> None:
        self._socket_manager = socket_manager or SocketManager(server_address)
        self._lights: list[Light] = []
        self._scenes: list[Scene] = []
        self._state_index: dict[int, Light] = {}
        self._unsubscribe: Callable[[], None] | None = None
        # Some controllers close idle connections every ~2 minutes and the hub
        # reconnects within a second. Only report the hub unavailable when the
        # connection stays down longer than this grace period.
        self._availability_grace = availability_grace
        self._available = False
        self._grace_handle: asyncio.TimerHandle | None = None
        self._availability_listeners: list[Callable[[bool], None]] = []
        self._unsubscribe_conn: Callable[[], None] | None = None

    async def connect(self, functions_data: dict) -> None:
        """Parse device data and connect to the local controller.

        Args:
            functions_data: Functions JSON dict as returned by
                SensioApi.get_devices(). Can be cached and replayed.
        """
        self._parse_devices(functions_data)
        if self._unsubscribe is None:
            self._unsubscribe = self._socket_manager.add_message_listener(self._handle_message)
        if self._unsubscribe_conn is None:
            self._unsubscribe_conn = self._socket_manager.add_connection_listener(self._handle_connection)
        await self._socket_manager.connect()
        self._set_available(True)

    @property
    def available(self) -> bool:
        """True while connected, and during short reconnects (within the grace period)."""
        return self._available

    def add_availability_listener(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``listener(True/False)`` when ``available`` changes. Returns an unsubscribe function."""
        self._availability_listeners.append(listener)
        return lambda: self._availability_listeners.remove(listener)

    def _set_available(self, available: bool) -> None:
        if available == self._available:
            return
        self._available = available
        for listener in list(self._availability_listeners):
            listener(available)

    def _handle_connection(self, connected: bool) -> None:
        if connected:
            if self._grace_handle is not None:
                self._grace_handle.cancel()
                self._grace_handle = None
            self._set_available(True)
        elif self._grace_handle is None:
            if self._availability_grace <= 0:
                self._set_available(False)
                return
            loop = asyncio.get_running_loop()
            self._grace_handle = loop.call_later(self._availability_grace, self._grace_expired)

    def _grace_expired(self) -> None:
        self._grace_handle = None
        if not self._socket_manager.connected:
            self._set_available(False)

    @property
    def connected(self) -> bool:
        """Return True while the connection to the controller is up."""
        return self._socket_manager.connected

    def add_connection_listener(self, listener: Callable[[bool], None]) -> Callable[[], None]:
        """Call ``listener(True/False)`` when the controller connection comes up or goes down."""
        return self._socket_manager.add_connection_listener(listener)

    def add_message_listener(self, listener: Callable[[Message], None]) -> Callable[[], None]:
        """Call ``listener(message)`` for every message pushed by the controller."""
        return self._socket_manager.add_message_listener(listener)

    async def force_reconnect(self) -> None:
        """Drop the current socket so it reconnects; sends nothing to the controller."""
        await self._socket_manager.force_reconnect()

    def _handle_message(self, message: Message) -> None:
        """Update light state from RSN notifications."""
        if not isinstance(message, RsnMessage):
            return
        light = self._state_index.get(message.address)
        value = message.value
        if light is None or value is None:
            return
        light.set_state(LightState.ON if value > 0 else LightState.OFF)

    def get_lights(self) -> list[Light]:
        """Return all discovered lights."""
        return list(self._lights)

    def get_scenes(self) -> list[Scene]:
        """Return all discovered scenes."""
        return list(self._scenes)

    def _parse_devices(self, functions_data: dict) -> None:
        """Parse the functions JSON into Light and Scene objects."""
        functions = functions_data.get("functions", [])

        self._lights = self._parse_lights(functions)
        self._scenes = self._parse_scenes(functions)
        self._state_index = {
            light.state_address: light for light in self._lights if light.state_address is not None
        }

    def _parse_lights(self, functions: list[dict]) -> list[Light]:
        """Parse individual lights and room-level light controls."""
        lights: list[Light] = []

        # --- Individual lights (subGroupId > 0) ---
        # Group by subGroupId to pair on/off addresses.
        groups: dict[int, dict] = {}
        for func in functions:
            sub_type = func["subType"]
            sub_group_id = func["subGroupId"]

            if sub_type not in ("light_on", "light_off", "light_state", "light_relay") or sub_group_id == 0:
                continue

            if sub_group_id not in groups:
                groups[sub_group_id] = {
                    "name": func["displayName"],
                    "zone_id": func["zoneId"],
                    "on_address": None,
                    "off_address": None,
                    "state_address": None,
                    "relay_address": None,
                }

            if sub_type == "light_on":
                groups[sub_group_id]["on_address"] = func["address"]
            elif sub_type == "light_off":
                groups[sub_group_id]["off_address"] = func["address"]
            elif sub_type == "light_state":
                groups[sub_group_id]["state_address"] = func["address"]
            elif sub_type == "light_relay":
                groups[sub_group_id]["relay_address"] = func["address"]

        for sub_group_id, info in groups.items():
            if info["on_address"] is not None and info["off_address"] is not None:
                lights.append(
                    Light(
                        unique_id=str(sub_group_id),
                        name=info["name"],
                        zone_id=info["zone_id"],
                        on_address=info["on_address"],
                        off_address=info["off_address"],
                        socket_manager=self._socket_manager,
                        # Prefer the *_State memory value; fall back to the relay output.
                        state_address=info["state_address"] or info["relay_address"],
                    )
                )

        # --- Room-level lights (LightOnRoom / LightOffRoom, subGroupId == 0) ---
        # Group by zoneId to pair on/off.
        room_groups: dict[str, dict] = {}
        for func in functions:
            sub_type = func["subType"]
            if sub_type not in ("LightOnRoom", "LightOffRoom"):
                continue

            zone_id = func["zoneId"]
            if zone_id not in room_groups:
                # Derive a friendly name from the internal name field
                # e.g. "B_LightStue_ON" -> "Stue"
                room_name = self._extract_room_name(func["name"])
                room_groups[zone_id] = {
                    "name": f"{room_name} All Lights",
                    "zone_id": zone_id,
                    "on_address": None,
                    "off_address": None,
                }

            if sub_type == "LightOnRoom":
                room_groups[zone_id]["on_address"] = func["address"]
            elif sub_type == "LightOffRoom":
                room_groups[zone_id]["off_address"] = func["address"]

        individual = list(lights)
        for zone_id, info in room_groups.items():
            if info["on_address"] is not None and info["off_address"] is not None:
                members = [m for m in individual if m.zone_id == zone_id and m.has_real_state]
                room = Light(
                    unique_id=f"{zone_id}_room",
                    name=info["name"],
                    zone_id=info["zone_id"],
                    on_address=info["on_address"],
                    off_address=info["off_address"],
                    socket_manager=self._socket_manager,
                    derived_state=bool(members),
                )
                for member in members:
                    member.add_listener(self._room_updater(room, members))
                lights.append(room)

        return lights

    @staticmethod
    def _room_updater(room: Light, members: list[Light]) -> Callable[[], None]:
        """Room light is on if any member is on, off if all known members are off."""
        def update() -> None:
            states = [m.state for m in members]
            if LightState.ON in states:
                room.set_state(LightState.ON)
            elif states and all(st == LightState.OFF for st in states):
                room.set_state(LightState.OFF)
        return update

    def _parse_scenes(self, functions: list[dict]) -> list[Scene]:
        """Parse scene functions into Scene objects."""
        room_scene_sub_types = {
            "LigthSc1Room", "LigthSc2Room", "LigthSc3Room", "LigthSc4Room",
        }
        house_scene_sub_types = {
            "house_in": "Home",
            "house_away": "Away",
            "house_night": "Night",
            "house_vacation": "Vacation",
        }
        scenes: list[Scene] = []

        for func in functions:
            sub_type = func["subType"]
            zone_id = func["zoneId"]
            address = func["address"]

            if sub_type in room_scene_sub_types:
                # Room light scene: e.g. "B_LightStue_Sc1" -> "Stue Scene 1"
                room_name = self._extract_room_name(func["name"])
                scene_number = sub_type.replace("LigthSc", "").replace("Room", "")
                display_name = f"{room_name} Scene {scene_number}"
            elif sub_type in house_scene_sub_types:
                # House-level scene: e.g. house_in -> "House Home"
                display_name = f"House {house_scene_sub_types[sub_type]}"
            else:
                continue

            scenes.append(
                Scene(
                    unique_id=f"{zone_id}_{address}",
                    name=display_name,
                    zone_id=zone_id,
                    address=address,
                    socket_manager=self._socket_manager,
                )
            )

        return scenes

    @staticmethod
    def _extract_room_name(internal_name: str) -> str:
        """Extract a room name from the Sensio internal naming convention.

        Examples:
            "B_LightStue_ON"    -> "Stue"
            "B_LightKontor_OFF" -> "Kontor"
            "B_LightStue_Sc1"   -> "Stue"
        """
        parts = internal_name.split("_")
        # Typically: B_Light<Room>_<Suffix> or B_Light<Room>_<Suffix>
        for part in parts:
            if part.startswith("Light") and len(part) > 5:
                return part[5:]  # Strip "Light" prefix
        # Fallback: return the middle part
        if len(parts) >= 3:
            return parts[1]
        return internal_name

    async def close(self) -> None:
        """Close the socket connection and clean up resources."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._unsubscribe_conn is not None:
            self._unsubscribe_conn()
            self._unsubscribe_conn = None
        if self._grace_handle is not None:
            self._grace_handle.cancel()
            self._grace_handle = None
        await self._socket_manager.close()
        self._set_available(False)

    async def __aenter__(self) -> Hub:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()