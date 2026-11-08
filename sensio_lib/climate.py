"""Read-only heating data: thermostat zones and the power meter.

Filled from RSN notifications, and from SSN answers to ``d_obj`` queries
(``Hub.query_state``), which read a function without changing it. Nothing
else is sent to the controller. Values are None until the first message.

Observed formats (Sensio SC-1):
- Thermostat (type 7): ``RSN <addr> TH_<Room><n> 7 1 <demand> <setpoint> <measured> <adjust>``
  ``demand`` is 1 while the zone wants heat (the heat relay switches on at the
  same moment). ``adjust`` is the manual offset set on the thermostat itself;
  the zone regulates to ``setpoint + adjust`` (living room on 2026-11-08:
  22.0 - 2.3 = 19.7 at 20.4 measured, relay off).
- Heat relay (type 8): ``RSN <addr> R_VK<Room><n> 8 1 <on> 0``.
- Active mode (``ThermCur``, ``M_<Room><n>_Cur``, type 23): the index of the
  setpoint slot in use, 0 = comfort, 1 = saving, 2 = frost protection (as in
  sk0zzi/sensio_lib, which found the protocol on an X1).
- Power meter (type 23, every 10 minutes): ``M_MeterCur`` is the average power
  in W over the last period; ``M_MeterSum`` is cumulative energy in watt-seconds.
"""

from __future__ import annotations

from collections.abc import Callable

from .messages import RsnMessage

WS_PER_KWH = 3_600_000
MODES = ("comfort", "saving", "frost_protection")  # ThermCur value -> name


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

    def __init__(self, zone_id: str, name: str, thermostat_address: int, relay_address: int | None,
                 mode_address: int | None = None) -> None:
        super().__init__()
        self.zone_id = zone_id
        self.name = name
        self.thermostat_address = thermostat_address
        self.relay_address = relay_address
        self.mode_address = mode_address
        self.measured_temperature: float | None = None
        self.setpoint: float | None = None
        self.adjustment: float | None = None
        self.heat_demand: bool | None = None
        self.relay_on: bool | None = None
        self.mode_index: int | None = None

    @property
    def unique_id(self) -> str:
        return f"{self.zone_id}_climate"

    @property
    def effective_setpoint(self) -> float | None:
        """What the zone regulates to: the setpoint plus the thermostat's own adjustment."""
        if self.setpoint is None:
            return None
        return round(self.setpoint + (self.adjustment or 0.0), 1)

    @property
    def mode(self) -> str | None:
        """Active setpoint slot: comfort, saving or frost_protection; None if unknown."""
        if self.mode_index is None or not 0 <= self.mode_index < len(MODES):
            return None
        return MODES[self.mode_index]

    @property
    def addresses(self) -> list[int]:
        """Addresses whose messages update this zone (and that ``d_obj`` reads)."""
        return [a for a in (self.thermostat_address, self.relay_address, self.mode_address) if a is not None]

    def handle(self, message: RsnMessage) -> None:
        """Update from an RSN message addressed to this zone."""
        changed = False
        if message.address == self.thermostat_address and len(message.fields) >= 4:
            demand, setpoint, measured = message.fields[1], _float(message.fields[2]), _float(message.fields[3])
            adjustment = _float(message.fields[4]) if len(message.fields) >= 5 else None
            new = (demand == "1", setpoint, measured, adjustment)
            if new != (self.heat_demand, self.setpoint, self.measured_temperature, self.adjustment):
                self.heat_demand, self.setpoint, self.measured_temperature, self.adjustment = new
                changed = True
        elif message.address == self.relay_address and message.value is not None:
            on = message.value > 0
            if on != self.relay_on:
                self.relay_on = on
                changed = True
        elif message.address == self.mode_address and message.value is not None:
            index = int(round(message.value))
            if index != self.mode_index:
                self.mode_index = index
                changed = True
        if changed:
            self._notify()

    def __repr__(self) -> str:
        return (f"ClimateZone(name={self.name!r}, measured={self.measured_temperature}, "
                f"setpoint={self.setpoint}, adjustment={self.adjustment}, "
                f"effective={self.effective_setpoint}, demand={self.heat_demand}, relay={self.relay_on}, "
                f"mode={self.mode or self.mode_index})")


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
    modes = {f["zoneId"]: f["address"] for f in functions if f.get("subType") == "ThermCur"}
    zones = [
        ClimateZone(
            zone_id=f["zoneId"],
            name=_zone_name(f.get("displayName") or f["name"]),
            thermostat_address=f["address"],
            relay_address=relays.get(f["zoneId"]),
            mode_address=modes.get(f["zoneId"]),
        )
        for f in functions
        if f.get("subType") == "Thermostat"
    ]
    power = next((f["address"] for f in functions if f.get("subType") == "MeterReader"), None)
    energy = next((f["address"] for f in functions if f.get("subType") == "MeterSum"), None)
    meter = PowerMeter(power, energy) if (power is not None or energy is not None) else None
    return zones, meter
