"""Hub availability with a grace period for short reconnects."""

import asyncio

import pytest

from sensio_lib.hub import Hub
from sensio_lib.socket_manager import SocketManager

from .test_socket_manager import FakeController, wait_for

pytestmark = pytest.mark.asyncio


async def make(controller, grace):
    sm = SocketManager("127.0.0.1", controller.port, backoff_initial=0.05, backoff_max=0.1)
    hub = Hub("127.0.0.1", socket_manager=sm, availability_grace=grace)
    events = []
    hub.add_availability_listener(events.append)
    await hub.connect({"functions": []})
    return hub, events


async def test_short_reconnect_is_invisible():
    c = FakeController(); await c.start()
    hub, events = await make(c, grace=0.5)
    c.drop_clients()                                   # controller closes the socket
    await wait_for(lambda: c.connections == 2 and hub._socket_manager.connected)
    await asyncio.sleep(0.6)                           # grace period passes while connected
    assert hub.available and events == [True]
    await hub.close(); await c.stop()


async def test_long_outage_reports_unavailable_then_available():
    c = FakeController(); await c.start()
    port = c.port
    hub, events = await make(c, grace=0.2)
    await c.stop()                                     # controller gone
    await wait_for(lambda: events[-1:] == [False], timeout=2)
    assert not hub.available
    c2 = FakeController(); await c2.start(port)
    await wait_for(lambda: events[-1:] == [True], timeout=3)
    assert events == [True, False, True]
    await hub.close(); await c2.stop()


async def test_zero_grace_is_immediate():
    c = FakeController(); await c.start()
    hub, events = await make(c, grace=0)
    c.drop_clients()
    await wait_for(lambda: False in events)
    await wait_for(lambda: events[-1:] == [True])
    await hub.close(); await c.stop()
