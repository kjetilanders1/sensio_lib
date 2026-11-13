"""Dimmer device for Sensio brightness control.

Ported from sk0zzi/sensio_lib (``sensio_lib/dimmer.py``, 2026-11-10) and
adapted to this fork's message parser and listener pattern.

Setting a level is a two-step command (verified on an SC-1 on 2026-11-13,
LysLoftstueDimmeaktuator):

  1. ``set_value <dim_value address> <0-100>``  -- write the target level
  2. ``new_state <dim_set address> 0``          -- press the apply button

The level comes back as RSN/SSN type 23 messages on the dim_value address
(``M_D_<Device>``), value in the last field. It follows the wall switch too
(on -> 100, off -> 0). The dimmer's ``S_`` analog function does not change
with the light, so it is not used. A level above 0 counts as on.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from .device import Device
from .messages import MEMORY_TYPE, RsnMessage
from .socket_manager import SocketManager


class DimmerState(Enum):
    """Possible states for a dimmer."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"


class Dimmer(Device):
    """A Sensio dimmer actuator with brightness 0-100."""

    def __init__(
        self,
        unique_id: str,
        name: str,
        zone_id: str,
        set_address: int,
        value_address: int,
        socket_manager: SocketManager,
    ) -> None:
        super().__init__(unique_id, name, zone_id, socket_manager)
        self._set_address = set_address
        self._value_address = value_address
        self._brightness: int | None = None
        self._last_brightness = 100
        self._listeners: list[Callable[[], None]] = []

    @property
    def set_address(self) -> int:
        """Address of the ``dim_set`` button that applies the level."""
        return self._set_address

    @property
    def value_address(self) -> int:
        """Address of the ``dim_value`` memory holding the level (and reporting it)."""
        return self._value_address

    @property
    def brightness(self) -> int | None:
        """Current level 0-100, or None until the controller has reported it."""
        return self._brightness

    @property
    def state(self) -> DimmerState:
        """On when the level is above 0."""
        if self._brightness is None:
            return DimmerState.UNKNOWN
        return DimmerState.ON if self._brightness > 0 else DimmerState.OFF

    @property
    def is_on(self) -> bool | None:
        """True/False, or None while unknown."""
        if self._brightness is None:
            return None
        return self._brightness > 0

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener()`` whenever the level changes. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _set_brightness_state(self, level: int) -> None:
        if level > 0:
            self._last_brightness = level
        if level == self._brightness:
            return
        self._brightness = level
        for listener in list(self._listeners):
            listener()

    def handle(self, message: RsnMessage) -> None:
        """Update the level from an RSN/SSN message on the dim_value address."""
        if message.address != self._value_address or message.type != MEMORY_TYPE:
            return
        value = message.value
        if value is None:
            return
        self._set_brightness_state(max(0, min(100, round(value))))

    async def set_brightness(self, level: int) -> None:
        """Set the level (clamped to 0-100). The controller confirms it with an RSN message."""
        level = max(0, min(100, int(level)))
        await self._socket_manager.send_command(f"set_value {self._value_address} {level}")
        await self._socket_manager.send_command(f"new_state {self._set_address} 0")
        self._set_brightness_state(level)

    async def turn_on(self) -> None:
        """Turn on at the last non-zero level seen, or 100 % if none has been seen."""
        level = self._brightness if self._brightness else self._last_brightness
        await self.set_brightness(level)

    async def turn_off(self) -> None:
        """Turn off (level 0)."""
        await self.set_brightness(0)

    def __repr__(self) -> str:
        level = f"{self._brightness}%" if self._brightness is not None else "unknown"
        return f"Dimmer(name={self._name!r}, state={self.state.value}, brightness={level})"
