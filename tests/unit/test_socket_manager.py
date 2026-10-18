"""SocketManager against a real local TCP server: read loop, dispatch, reconnect."""

import asyncio

import pytest
import pytest_asyncio

from sensio_lib.exceptions import SensioConnectionError
from sensio_lib.messages import EndMessage, RsnMessage
from sensio_lib.socket_manager import SocketManager


class FakeController:
    """Minimal controller: greets, records commands, can push frames or drop clients."""

    def __init__(self):
        self.server = None
        self.port = None
        self.writers = []
        self.received = []
        self.connections = 0

    async def start(self, port=0):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.connections += 1
        self.writers.append(writer)
        writer.write(b'\x01<connect sn="00-000" ip="127.0.0.1" fw="fake"/>\r\n\x02\x00')
        await writer.drain()
        while data := await reader.read(1024):
            self.received.append(data)

    async def push(self, text):
        for w in self.writers:
            if not w.is_closing():
                w.write(b"\x01" + text.encode() + b"\x02\x00")
                await w.drain()

    def drop_clients(self):
        for w in self.writers:
            w.close()
        self.writers.clear()

    async def stop(self):
        self.drop_clients()
        self.server.close()
        await self.server.wait_closed()


async def wait_for(cond, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


@pytest_asyncio.fixture
async def controller():
    c = FakeController()
    await c.start()
    yield c
    await c.stop()


pytestmark = pytest.mark.asyncio


def manager(port, **kw):
    return SocketManager("127.0.0.1", port, backoff_initial=0.05, backoff_max=0.2, **kw)


async def test_reads_and_dispatches_messages(controller):
    sm = manager(controller.port)
    seen = []
    sm.add_message_listener(seen.append)
    await sm.connect()
    await controller.push("RSN 1201 R_LysStue 8 1 1 0")
    await controller.push("end 1201")
    await wait_for(lambda: any(isinstance(m, EndMessage) for m in seen))
    assert RsnMessage(1201, "R_LysStue", 8, ("1", "1", "0")) in seen
    await sm.close()


async def test_send_command_framing(controller):
    sm = manager(controller.port)
    await sm.connect()
    await sm.send_command("new_state 1201 0")
    await wait_for(lambda: controller.received)
    assert b"".join(controller.received) == b"\x01new_state 1201 0\x02"
    await sm.close()


async def test_reconnects_after_controller_drops_connection(controller):
    sm = manager(controller.port)
    events = []
    sm.add_connection_listener(events.append)
    await sm.connect()
    controller.drop_clients()
    await wait_for(lambda: events[-1:] == [True] and controller.connections == 2)
    assert events == [True, False, True]
    await sm.send_command("new_state 5 0")   # works on the new connection
    await wait_for(lambda: b"new_state 5 0" in b"".join(controller.received))
    await sm.close()


async def test_force_reconnect_sends_nothing(controller):
    sm = manager(controller.port)
    await sm.connect()
    await sm.force_reconnect()
    await wait_for(lambda: controller.connections == 2 and sm.connected)
    assert controller.received == []
    await sm.close()


async def test_backoff_until_controller_returns():
    c = FakeController()
    await c.start()
    port = c.port
    sm = manager(port)
    events = []
    sm.add_connection_listener(events.append)
    await sm.connect()
    await c.stop()                      # controller gone entirely
    await wait_for(lambda: events[-1:] == [False])
    await asyncio.sleep(0.4)            # several failed attempts with backoff
    c2 = FakeController()
    await c2.start(port)
    await wait_for(lambda: events[-1:] == [True], timeout=5)
    await sm.close()
    await c2.stop()


async def test_send_waits_briefly_then_fails_when_down():
    c = FakeController()
    await c.start()
    sm = manager(c.port, command_wait_timeout=0.2)
    await sm.connect()
    await c.stop()
    await wait_for(lambda: not sm.connected)
    with pytest.raises(SensioConnectionError):
        await sm.send_command("new_state 1 0")
    await sm.close()


async def test_initial_connect_failure_raises():
    sm = manager(1)  # nothing listens on port 1
    with pytest.raises(SensioConnectionError):
        await sm.connect()


async def test_close_stops_reconnecting(controller):
    sm = manager(controller.port)
    await sm.connect()
    await sm.close()
    controller.drop_clients()
    await asyncio.sleep(0.3)
    assert controller.connections == 1 and not sm.connected
