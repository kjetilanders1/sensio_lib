"""Read-only heating zones and power meter from RSN (synthetic data in the controller's format)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from sensio_lib.climate import parse_climate
from sensio_lib.hub import Hub
from sensio_lib.messages import parse_message

FUNCS = {"functions": [
    {"address": 1001, "subType": "Thermostat", "name": "TH_Stue001", "displayName": "Stue Thermostat", "zoneId": "z-stue", "subGroupId": 0},
    {"address": 1002, "subType": "HeatRelay",  "name": "R_VKStue001", "displayName": "Stue Temperature Relay", "zoneId": "z-stue", "subGroupId": 0},
    {"address": 1003, "subType": "TempCurAvg", "name": "M_StueTempCurAvg", "displayName": "Stue Zone Temp Cur Avg", "zoneId": "z-stue", "subGroupId": 0},
    {"address": 1004, "subType": "ThermCur",   "name": "M_Stue001_Cur", "displayName": "Stue Temperature Scene", "zoneId": "z-stue", "subGroupId": 0},
    {"address": 2001, "subType": "Thermostat", "name": "TH_Bad002", "displayName": "Bad 2.etg Thermostat", "zoneId": "z-bad", "subGroupId": 0},
    {"address": 3001, "subType": "MeterReader", "name": "M_MeterCur", "displayName": "Meter value", "zoneId": "z-house", "subGroupId": 0},
    {"address": 3002, "subType": "MeterSum", "name": "M_MeterSum", "displayName": "Meter Sum", "zoneId": "z-house", "subGroupId": 0},
]}

pytestmark = pytest.mark.asyncio


def test_parse_zones_and_meter():
    zones, meter = parse_climate(FUNCS["functions"])
    assert [(z.name, z.thermostat_address, z.relay_address) for z in zones] == [
        ("Stue", 1001, 1002), ("Bad 2.etg", 2001, None)]
    assert (meter.power_address, meter.energy_address) == (3001, 3002)
    assert parse_climate([]) == ([], None)


async def make_hub():
    sm = MagicMock(); sm.connect = AsyncMock()
    listeners = []
    sm.add_message_listener = lambda cb: listeners.append(cb) or (lambda: None)
    sm.add_connection_listener = lambda cb: (lambda: None)
    hub = Hub("192.0.2.1", socket_manager=sm)
    await hub.connect(FUNCS)
    return hub, lambda text: [cb(parse_message(text)) for cb in listeners]


async def test_thermostat_relay_and_meter_updates():
    hub, push = await make_hub()
    stue = hub.get_climate_zones()[0]
    assert (stue.measured_temperature, stue.setpoint, stue.heat_demand, stue.relay_on) == (None, None, None, None)
    changes = []
    stue.add_listener(lambda: changes.append(1))

    push("RSN 1001 TH_Stue001 7 1 1 22.0 21.4 -0.3")      # wants heat; last field ignored
    assert (stue.setpoint, stue.measured_temperature, stue.heat_demand) == (22.0, 21.4, True)
    push("RSN 1002 R_VKStue001 8 1 1 0")
    assert stue.relay_on is True
    push("RSN 1001 TH_Stue001 7 1 0 22.0 22.1 0.4")
    assert (stue.measured_temperature, stue.heat_demand) == (22.1, False)
    push("RSN 1001 TH_Stue001 7 1 0 22.0 22.1 0.5")       # only the unknown field changed -> no update
    assert len(changes) == 3

    meter = hub.get_power_meter()
    push("RSN 3001 M_MeterCur 23 1 0 1500.00")
    push("RSN 3002 M_MeterSum 23 1 0 36000000.00")         # watt-seconds
    assert meter.power_w == 1500.0 and meter.energy_kwh == 10.0


async def test_query_state_reads_with_d_obj_and_ssn_updates():
    hub, push = await make_hub()
    sm = hub._socket_manager
    sm.send_command = AsyncMock()
    await hub.query_state()
    assert [c.args[0] for c in sm.send_command.call_args_list] == [
        "d_obj 1001", "d_obj 1002", "d_obj 1004", "d_obj 2001", "d_obj 3001", "d_obj 3002"]
    assert not any(c.args[0].startswith(("new_state", "set_value")) for c in sm.send_command.call_args_list)

    stue = hub.get_climate_zones()[0]
    push("SSN 1001 TH_Stue001 7 1 0 22.0 20.6 0.1")       # the answer to d_obj 1001
    push("SSN 1004 M_Stue001_Cur 23 1 0 1.00")
    assert (stue.setpoint, stue.measured_temperature, stue.mode_index, stue.mode) == (22.0, 20.6, 1, "saving")


def test_ssn_parses_like_rsn():
    rsn, ssn = parse_message("RSN 1 X 23 1 0 2.00"), parse_message("SSN 1 X 23 1 0 2.00")
    assert (ssn.address, ssn.type, ssn.value, ssn.queried, rsn.queried) == (1, 23, 2.0, True, False)


async def test_unknown_mode_index_is_kept_but_unnamed():
    hub, push = await make_hub()
    stue = hub.get_climate_zones()[0]
    push("RSN 1004 M_Stue001_Cur 23 1 0 7.00")
    assert (stue.mode_index, stue.mode) == (7, None)


async def test_malformed_thermostat_is_ignored():
    hub, push = await make_hub()
    stue = hub.get_climate_zones()[0]
    push("RSN 1001 TH_Stue001 7 1 1")                     # too few fields
    push("RSN 1001 TH_Stue001 7 1 1 x y")                 # not numbers -> None values
    assert stue.measured_temperature is None and stue.setpoint is None
