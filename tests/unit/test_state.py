"""Real light state from RSN notifications, and room lights derived from members."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from sensio_lib.hub import Hub
from sensio_lib.light import LightState
from sensio_lib.messages import parse_message

FUNCS = {"functions": [
    # Stue: two individual lights (one with a *_State value, one with only a relay), plus room on/off
    {"address": 101, "subType": "light_on",    "name": "B_R_LysA_ON",   "displayName": "Lys A", "zoneId": "z-stue", "subGroupId": 10},
    {"address": 102, "subType": "light_off",   "name": "B_R_LysA_OFF",  "displayName": "Lys A", "zoneId": "z-stue", "subGroupId": 10},
    {"address": 103, "subType": "light_state", "name": "M_R_LysA_State","displayName": "Lys A", "zoneId": "z-stue", "subGroupId": 10},
    {"address": 104, "subType": "light_relay", "name": "R_LysA",        "displayName": "Lys A", "zoneId": "z-stue", "subGroupId": 10},
    {"address": 111, "subType": "light_on",    "name": "B_R_LysB_ON",   "displayName": "Lys B", "zoneId": "z-stue", "subGroupId": 11},
    {"address": 112, "subType": "light_off",   "name": "B_R_LysB_OFF",  "displayName": "Lys B", "zoneId": "z-stue", "subGroupId": 11},
    {"address": 114, "subType": "light_relay", "name": "R_LysB",        "displayName": "Lys B", "zoneId": "z-stue", "subGroupId": 11},
    {"address": 301, "subType": "LightOnRoom", "name": "B_LightStue_ON",  "displayName": "On",  "zoneId": "z-stue", "subGroupId": 0},
    {"address": 302, "subType": "LightOffRoom","name": "B_LightStue_OFF", "displayName": "Off", "zoneId": "z-stue", "subGroupId": 0},
    # Gang: individual light without any state function -> stays optimistic
    {"address": 201, "subType": "light_on",    "name": "B_R_LysC_ON",   "displayName": "Lys C", "zoneId": "z-gang", "subGroupId": 20},
    {"address": 202, "subType": "light_off",   "name": "B_R_LysC_OFF",  "displayName": "Lys C", "zoneId": "z-gang", "subGroupId": 20},
]}


pytestmark = pytest.mark.asyncio


def make_hub():
    sm = MagicMock()
    sm.connect = AsyncMock()
    listeners = []
    sm.add_message_listener = lambda cb: listeners.append(cb) or (lambda: None)
    hub = Hub("192.0.2.1", socket_manager=sm)
    return hub, listeners


def push(listeners, text):
    for cb in listeners:
        cb(parse_message(text))


async def test_state_addresses_and_rsn_updates():
    hub, listeners = make_hub()
    await hub.connect(FUNCS)
    by_name = {l.name: l for l in hub.get_lights()}
    a, b, c, room = by_name["Lys A"], by_name["Lys B"], by_name["Lys C"], by_name["Stue All Lights"]
    assert (a.state_address, b.state_address, c.state_address) == (103, 114, None)
    assert a.has_real_state and b.has_real_state and not c.has_real_state and room.has_real_state

    changes = []
    a.add_listener(lambda: changes.append(a.state))
    push(listeners, "RSN 103 M_R_LysA_State 23 1 0 1.00")       # wall switch on
    assert a.state == LightState.ON and changes == [LightState.ON]
    push(listeners, "RSN 104 R_LysA 8 1 0 0")                   # relay of A is not A's state address
    assert a.state == LightState.ON
    push(listeners, "RSN 103 M_R_LysA_State 23 1 0 0.00")
    assert a.state == LightState.OFF

    push(listeners, "RSN 114 R_LysB 8 1 1 0")                   # B via relay
    assert b.state == LightState.ON and room.state == LightState.ON
    push(listeners, "RSN 114 R_LysB 8 1 0 0")
    assert room.state == LightState.OFF                          # A and B both off now
    push(listeners, "RSN 999 Something 23 1 0 1.00")             # unknown address ignored
    push(listeners, "end 103")


async def test_room_unknown_until_all_members_known():
    hub, listeners = make_hub()
    await hub.connect(FUNCS)
    room = next(l for l in hub.get_lights() if l.name == "Stue All Lights")
    push(listeners, "RSN 103 M_R_LysA_State 23 1 0 0.00")
    assert room.state == LightState.UNKNOWN                      # B still unknown
    push(listeners, "RSN 114 R_LysB 8 1 0 0")
    assert room.state == LightState.OFF
