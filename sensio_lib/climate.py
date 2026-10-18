"""Read-only heating data pushed by the controller: thermostat zones and the power meter.

Everything here is filled from RSN notifications; nothing is sent to the
controller. Values are None until the first notification after connecting.

Observed formats (Sensio SC-1):
- Thermostat (type 7): ``RSN <addr> TH_<Room><n> 7 1 <demand> <setpoint> <measured> <x>``
  ``demand`` is 1 while the zone wants heat (the heat relay switches on at the
  same moment). The last field is not understood yet and is ignored.
- Heat relay (type 8): ``RSN <addr> R_VK<Room><n> 8 1 <on> 0``.
- Power meter (type 23, every 10 minutes): ``M_MeterCur`` is the average power
  in W over the last period; ``M_MeterSum`` is cumulative energy in watt-seconds.
"""

from __future__ import annotations

from collections.abc import Callable

from .messages import RsnMessage

WS_PER_KWH = 3_600_000


class _Observable:
    def __init__(self) -> None:
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call ``listener()`` when any value changes. Returns an unsubscribe function."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


def _float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


class ClimateZone(_Observable):
    """One heating zone (thermostat + heat relay). Read-only."""

    def __init__(self, zone_id: str, name: str, thermostat_address: int, relay_address: int | None) -> None:
        super().__init__()
        self.zone_id = zone_id
        self.name = name
        self.thermostat_address = thermostat_address
        self.relay_address = relay_address
        self.measured_temperature: float | None = None
        self.setpoint: float | None = None
        self.heat_demand: bool | None = None
        self.relay_on: bool | None = None

    @property
    def unique_id(self) -> str:
        return f"{self.zone_id}_climate"

    def handle(self, message: RsnMessage) -> None:
        """Update from an RSN message addressed to this zone."""
        changed = False
        if message.address == self.thermostat_address and len(message.fields) >= 4:
            demand, setpoint, measured = message.fields[1], _float(message.fields[2]), _float(message.fields[3])
            new = (demand == "1", setpoint, measured)
            if new != (self.heat_demand, self.setpoint, self.measured_temperature):
                self.heat_demand, self.setpoint, self.measured_temperature = new
                changed = True
        elif message.address == self.relay_address and message.value is not None:
            on = message.value > 0
            if on != self.relay_on:
                self.relay_on = on
                changed = True
        if changed:
            self._notify()

    def __repr__(self) -> str:
        return (f"ClimateZone(name={self.name!r}, measured={self.measured_temperature}, "
                f"setpoint={self.setpoint}, demand={self.heat_demand}, relay={self.relay_on})")


class PowerMeter(_Observable):
    """House power meter. Read-only."""

    def __init__(self, power_address: int | None, energy_address: int | None) -> None:
        super().__init__()
        self.power_address = power_address
        self.energy_address = energy_address
        self.power_w: float | None = None
        self.energy_kwh: float | None = None

    @property
    def addresses(self) -> list[int]:
        return [a for a in (self.power_address, self.energy_address) if a is not None]

    def handle(self, message: RsnMessage) -> None:
        value = message.value
        if value is None:
            return
        if message.address == self.power_address and value != self.power_w:
            self.power_w = value
            self._notify()
        elif message.address == self.energy_address:
            kwh = round(value / WS_PER_KWH, 3)
            if kwh != self.energy_kwh:
                self.energy_kwh = kwh
                self._notify()


def _zone_name(display_name: str) -> str:
    """'Bad 2.etg Thermostat' -> 'Bad 2.etg'."""
    suffix = " Thermostat"
    return display_name[: -len(suffix)] if display_name.endswith(suffix) else display_name


def parse_climate(functions: list[dict]) -> tuple[list[ClimateZone], PowerMeter | None]:
    """Build thermostat zones and the power meter from the project's functions."""
    relays = {f["zoneId"]: f["address"] for f in functions if f.get("subType") == "HeatRelay"}
    zones = [
        ClimateZone(
            zone_id=f["zoneId"],
            name=_zone_name(f.get("displayName") or f["name"]),
            thermostat_address=f["address"],
            relay_address=relays.get(f["zoneId"]),
        )
        for f in functions
        if f.get("subType") == "Thermostat"
    ]
    power = next((f["address"] for f in functions if f.get("subType") == "MeterReader"), None)
    energy = next((f["address"] for f in functions if f.get("subType") == "MeterSum"), None)
    meter = PowerMeter(power, energy) if (power is not None or energy is not None) else None
    return zones, meter
