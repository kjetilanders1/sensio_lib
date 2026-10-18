"""Light device for Sensio on/off control."""

from collections.abc import Callable
from enum import Enum

from .device import Device
from .socket_manager import SocketManager


class LightState(Enum):
    """Possible states for a light."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class Light(Device):
    """Represents a Sensio light with on/off control.

    State comes from the controller's RSN notifications when the light has a
    known state address (``light_state`` / ``light_relay`` function); otherwise,
    and until the first notification, it is tracked optimistically after commands.
    """

    def __init__(
        self,
        unique_id: str,
        name: str,
        zone_id: str,
        on_address: int,
        off_address: int,
        socket_manager: SocketManager,
        state_address: int | None = None,
        derived_state: bool = False,
    ) -> None:
        super().__init__(unique_id, name, zone_id, socket_manager)
        self._on_address = on_address
        self._off_address = off_address
        self._state_address = state_address
        self._derived_state = derived_state
        self._state = LightState.UNKNOWN
        self._listeners: list[Callable[[], None]] = []

    @property
    def state(self) -> LightState:
        """Return the current state of the light."""
        return self._state

    @property
    def is_on(self) -> bool:
        """Return True if the light is considered on."""
        return self._state == LightState.ON

    @property
    def state_address(self) -> int | None:
        """Address whose RSN notifications report this light's real state, if known."""
        return self._state_address

    @property
    def has_real_state(self) -> bool:
        """True if the state is reported by the controller (not just assumed).

        Room-level lights derive their state from the room's individual lights.
        """
        return self._state_address is not None or self._derived_state

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener()`` whenever the state changes. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def set_state(self, state: LightState) -> None:
        """Update the state (from a controller notification) and notify listeners."""
        if state == self._state:
            return
        self._state = state
        for listener in list(self._listeners):
            listener()

    async def turn_on(self) -> None:
        """Turn the light on."""
        await self._socket_manager.send_command(f"new_state {self._on_address} 0")
        self.set_state(LightState.ON)

    async def turn_off(self) -> None:
        """Turn the light off."""
        await self._socket_manager.send_command(f"new_state {self._off_address} 0")
        self.set_state(LightState.OFF)

    def __repr__(self) -> str:
        return f"Light(name={self._name!r}, state={self._state.value})"
