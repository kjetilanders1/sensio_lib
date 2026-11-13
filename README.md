# Sensio Library

A minimal async library to control Sensio (EOPT Home) smart house systems. Designed for easy integration with Home Assistant.

## Features (v1.2.0)

- **Unity and HA pilot** - Supports getting devices from both unity and ha-pilot. 
- **Local control** — All device commands sent directly to your controller on the LAN
- **Cloud API** — Fetch device configuration from the EOPT cloud (one-time setup)
- **Lights** — Turn individual lights and room-level lights on/off
- **Scenes** — Activate lighting scenes

## Architecture

The library separates cloud access from local control:

- **`SensioApi`** — Authenticates with the Sensio cloud and fetches device data. Used once during setup; the result can be cached.
- **`Hub`** — Connects to your local controller and sends commands. Works entirely offline using cached device data.

## Quick Start

### First-time setup (fetch device data from cloud)

```python
from sensio_lib import SensioApi

async with SensioApi(username, password) as api:
    projects = await api.login()
    project_id = next(iter(projects.values()))
    functions_data = await api.get_devices(project_id)
    # Cache functions_data for future use
```

### Local control (from cached data)

```python
from sensio_lib import Hub

async with Hub("192.168.1.100") as hub:
    await hub.connect(cached_functions_data)

    for light in hub.get_lights():
        await light.turn_on()

    for scene in hub.get_scenes():
        await scene.activate()
```

## Connection handling and state (v1.3.0)

- **Read loop** — the `Hub` continuously reads what the controller pushes, so its send buffer never fills up (a full buffer made the controller drop the connection).
- **Automatic reconnect** — when the connection is lost the hub reconnects with exponential backoff (1 s doubling to 60 s, with jitter). `hub.connected` and `hub.add_connection_listener()` report the connection state; commands sent while disconnected wait briefly for the reconnect, then raise `SensioConnectionError`.
- **Real light state** — the controller pushes `RSN <address> <name> <type> ...` notifications for every change, including wall switches. Lights that have a `light_state` (or `light_relay`) function in the project get their state from these (`light.add_listener()`, `light.has_real_state`). Room-level "All Lights" are on when any light in the room is on. State is unknown until the first notification after connecting.
- **Availability grace** — some controllers close idle connections about every two minutes; the hub reconnects within a second. `hub.available` / `hub.add_availability_listener()` only report unavailable when the connection stays down longer than `availability_grace` (default 15 s).
- `hub.force_reconnect()` drops the socket (sends nothing) for diagnostics and tests.

Tested against a Sensio SC-1 controller as well.

## Heating (read-only, v1.4.0)

`hub.get_climate_zones()` returns one `ClimateZone` per thermostat with `measured_temperature`, `setpoint`, `heat_demand`, `relay_on` and `mode` (the active setpoint slot from `ThermCur`: `comfort`, `saving` or `frost_protection`); `hub.get_power_meter()` returns a `PowerMeter` with `power_w` (average over the controller's 10-minute period) and `energy_kwh` (cumulative). Values come from the controller's notifications and are `None` until the first one; thermostats only report when the temperature changes. `await hub.query_state()` asks for them at once: it sends `d_obj <address>` for every zone's thermostat, relay and mode and for the meter, and the controller answers with `SSN` messages in the RSN format (tested on an SC-1 on 2026-11-08, answers within a second). `d_obj` only reads. There is no heating control yet.

## Dimmers (v1.5.0)

`hub.get_dimmers()` returns one `Dimmer` per dimmer actuator: every `subGroupId` with both a `dim_set` and a `dim_value` function. `await dimmer.set_brightness(0-100)` sends `set_value <dim_value> <level>` and then `new_state <dim_set> 0`; `turn_on()` goes back to the last level above 0 (100 % if none has been seen) and `turn_off()` sets 0. The level comes from type 23 messages on the `dim_value` address (also when the wall switch is used: on 100, off 0), and `hub.query_state()` now also reads every dimmer's level with `d_obj`. `brightness` is `None` and `state` `UNKNOWN` until the first value; above 0 counts as on.

Ported from sk0zzi/sensio_lib (2026-11-10) onto this fork's parser. Checked on an SC-1 on 2026-11-13 (LysLoftstueDimmeaktuator: 20 %, 30 %, 100 %, off, and the wall switch). The actuator's `S_…` analog function stayed 0.0 while the light was on, so it is not used; `light_mem` (scene levels) isn't either. Room buttons (`… All Lights`) are unchanged.

## Device Support

This library has been tested with a Sensio X1 controller. Testing is limited, so your experience may vary. Device support is limited to scenes, lights, dimmers and read-only heating. Light state is read back from the controller's notifications (see above).

## Requirements

- Python 3.10+
- `aiohttp`

## Testing

Example script reads env params for sensitive data and does not store anything. Example for launching it and testing:

```bash
SENSIO_USERNAME="your user name / email" SENSIO_PASSWORD="your password" SENSIO_HUB_IP="your hub IP" python example.py
```

You can also optionally specify `SENSIO_ENV` to target a different cloud environment (defaults to `unity`, can also be `pilot-ha`):

```bash
SENSIO_ENV="pilot-ha" SENSIO_USERNAME="your user name / email" SENSIO_PASSWORD="your password" SENSIO_HUB_IP="your hub IP" python example.py
```