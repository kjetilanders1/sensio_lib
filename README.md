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
- `hub.force_reconnect()` drops the socket (sends nothing) for diagnostics and tests.

Tested against a Sensio SC-1 controller as well.

## Device Support

This library has been tested with a Sensio X1 controller. Testing is limited, so your experience may vary. Device support is also very limited to only scenes and lights. Light state is read back from the controller's notifications (see above); dimming is not supported yet.

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