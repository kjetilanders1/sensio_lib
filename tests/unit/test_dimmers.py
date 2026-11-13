"""Dimmers: parsing (dim_set + dim_value per subGroupId), the two-step set command, and the level from RSN/SSN.

Messages are the ones the SC-1 sent for LysLoftstueDimmeaktuator on 2026-11-13. Command and state handling
follows sk0zzi/sensio_lib tests/unit/test_dimmers.py, adapted to this fork's parser.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from sensio_lib.dimmer import Dimmer, DimmerState
from sensio_lib.hub import Hub
from sensio_lib.messages import parse_message

FUNCS = {"functions": [
    # A dimmer actuator: everything the controller lists for it
    {"address": 36008, "subType": "analougeNotShow", "name": "S_LysLoftstueDimmeaktuatorLof", "displayName": "LysLoftstueDimmeaktuator", "zoneId": "z-loft", "subGroupId": 60},
    {"address": 36160, "subType": "dim_set",   "name": "B_D_LysLoftstueDimmeaktuatorL", "displayName": "LysLoftstueDimmeaktuator", "zoneId": "z-loft", "subGroupId": 60},
    {"address": 36114, "subType": "dim_value", "name": "M_D_LysLoftstueDimmeaktuatorL", "displayName": "LysLoftstueDimmeaktuator", "zoneId": "z-loft", "subGroupId": 60},
    {"address": 36303, "subType": "light_mem", "name": "M_D_LysLoftstueDimmeaktuatorL", "displayName": "LysLoftstueDimmeaktuator", "zoneId": "z-loft", "subGroupId": 60},
    # Room buttons for the same zone stay a room light
    {"address": 75847, "subType": "LightOnRoom",  "name": "B_LightLoftstue_ON",  "displayName": "LightAllOn",  "zoneId": "z-loft", "subGroupId": 0},
    {"address": 75931, "subType": "LightOffRoom", "name": "B_LightLoftstue_OFF", "displayName": "LightAllOff", "zoneId": "z-loft", "subGroupId": 0},
    # Half a dimmer (no dim_value) is skipped
    {"address": 500, "subType": "dim_set", "name": "B_D_Half", "displayName": "Half", "zoneId": "z1", "subGroupId": 70},
]}


def make_sm():
    sm = MagicMock()
    sm.connect = AsyncMock()
    sm.send_command = AsyncMock()
    return sm


def make_dimmer(sm=None):
    return Dimmer("60", "Loft", "z-loft", set_address=36160, value_address=36114, socket_manager=sm or make_sm())


def sent(sm):
    return [c.args[0] for c in sm.send_command.call_args_list]


async def make_hub():
    sm = make_sm()
    listeners = []
    sm.add_message_listener = lambda cb: listeners.append(cb) or (lambda: None)
    hub = Hub("192.0.2.1", socket_manager=sm)
    await hub.connect(FUNCS)
    return hub, sm, lambda text: [cb(parse_message(text)) for cb in listeners]


@pytest.mark.asyncio
async def test_one_dimmer_per_subgroup_and_room_light_unchanged():
    hub, _, _ = await make_hub()
    [d] = hub.get_dimmers()
    assert (d.unique_id, d.name, d.zone_id, d.set_address, d.value_address) == (
        "60", "LysLoftstueDimmeaktuator", "z-loft", 36160, 36114)
    assert [l.name for l in hub.get_lights()] == ["Loftstue All Lights"]


@pytest.mark.asyncio
async def test_level_from_rsn_and_ssn_wall_switch():
    hub, _, push = await make_hub()
    [d] = hub.get_dimmers()
    changes = []
    d.add_listener(lambda: changes.append(d.brightness))
    assert (d.state, d.brightness, d.is_on) == (DimmerState.UNKNOWN, None, None)

    push("RSN 36114 M_D_LysLoftstueDimmeaktuatorL 23 1 0 100.00")   # wall switch on
    assert (d.state, d.brightness, d.is_on) == (DimmerState.ON, 100, True)
    push("SSN 36114 M_D_LysLoftstueDimmeaktuatorL 23 1 0 100.000")  # d_obj answer, same value: no event
    push("RSN 36008 S_LysLoftstueDimmeaktuatorLof 10 1 0 0.0")      # S_ doesn't follow the light: ignored
    push("RSN 36303 M_D_LysLoftstueDimmeaktuatorL 23 1 0 80.000 60.000 0.000 0.000")  # light_mem: ignored
    assert d.brightness == 100
    push("RSN 36114 M_D_LysLoftstueDimmeaktuatorL 23 1 0 0.00")     # wall switch off
    assert (d.state, d.is_on) == (DimmerState.OFF, False)
    assert changes == [100, 0]


@pytest.mark.asyncio
async def test_query_state_reads_dimmer_level():
    hub, sm, _ = await make_hub()
    await hub.query_state()
    assert sent(sm) == ["d_obj 36114"]


@pytest.mark.asyncio
async def test_set_brightness_sends_set_value_then_button():
    sm = make_sm()
    d = make_dimmer(sm)
    await d.set_brightness(30)
    assert sent(sm) == ["set_value 36114 30", "new_state 36160 0"]
    assert d.brightness == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("given, level", [(150, 100), (-10, 0), (0, 0)])
async def test_set_brightness_clamps(given, level):
    sm = make_sm()
    await make_dimmer(sm).set_brightness(given)
    assert sent(sm)[0] == f"set_value 36114 {level}"


@pytest.mark.asyncio
async def test_turn_on_restores_last_nonzero_level():
    sm = make_sm()
    d = make_dimmer(sm)
    d.handle(parse_message("RSN 36114 M_D_X 23 1 0 40.00"))
    d.handle(parse_message("RSN 36114 M_D_X 23 1 0 0.00"))
    await d.turn_on()
    assert sent(sm)[0] == "set_value 36114 40"


@pytest.mark.asyncio
@pytest.mark.parametrize("known", [None, 0])
async def test_turn_on_defaults_to_100(known):
    sm = make_sm()
    d = make_dimmer(sm)
    if known is not None:
        d.handle(parse_message(f"RSN 36114 M_D_X 23 1 0 {known}.00"))
    await d.turn_on()
    assert sent(sm)[0] == "set_value 36114 100"


@pytest.mark.asyncio
async def test_turn_off_sets_zero():
    sm = make_sm()
    d = make_dimmer(sm)
    await d.turn_off()
    assert sent(sm) == ["set_value 36114 0", "new_state 36160 0"]
    assert d.is_on is False


def test_other_addresses_and_types_are_ignored():
    d = make_dimmer()
    d.handle(parse_message("RSN 36115 M_D_X 23 1 0 50.00"))
    d.handle(parse_message("RSN 36114 M_D_X 8 1 1 0"))
    assert d.brightness is None
