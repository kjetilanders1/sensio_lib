"""Async socket manager for communicating with the Sensio controller.

Keeps one TCP connection to the controller, continuously reads what the
controller pushes (so its send buffer never fills up), dispatches parsed
messages to listeners, and reconnects with exponential backoff when the
connection is lost.
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
from collections.abc import Callable

from .const import COMMAND_POSTFIX, COMMAND_PREFIX
from .exceptions import SensioCommandError, SensioConnectionError
from .messages import FrameBuffer, Message, parse_message

logger = logging.getLogger(__name__)

MessageListener = Callable[[Message], None]
ConnectionListener = Callable[[bool], None]


def _enable_keepalive(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError as err:  # not fatal
        logger.debug("Could not enable TCP keepalive: %s", err)


class SocketManager:
    """Manages the TCP socket connection to the Sensio controller.

    Commands are sent sequentially (asyncio lock), matching the controller's
    sequential processing model. A background task reads incoming frames and
    reconnects automatically after a disconnect.
    """

    def __init__(
        self,
        server_address: str,
        port: int = 10023,
        *,
        reconnect: bool = True,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        command_wait_timeout: float = 5.0,
    ) -> None:
        self._server_address = server_address
        self._port = port
        self._reconnect = reconnect
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._command_wait_timeout = command_wait_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._connected_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._closing = False
        self._message_listeners: list[MessageListener] = []
        self._connection_listeners: list[ConnectionListener] = []

    # ── listeners ─────────────────────────────────────────────────────────

    def add_message_listener(self, listener: MessageListener) -> Callable[[], None]:
        """Call ``listener`` for every message from the controller. Returns an unsubscribe function."""
        self._message_listeners.append(listener)
        return lambda: self._message_listeners.remove(listener)

    def add_connection_listener(self, listener: ConnectionListener) -> Callable[[], None]:
        """Call ``listener(True/False)`` when the connection comes up or goes down."""
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    def _notify_connection(self, connected: bool) -> None:
        for listener in list(self._connection_listeners):
            try:
                listener(connected)
            except Exception:  # a listener must never kill the read loop
                logger.exception("Sensio connection listener failed")

    def _dispatch(self, message: Message) -> None:
        for listener in list(self._message_listeners):
            try:
                listener(message)
            except Exception:
                logger.exception("Sensio message listener failed")

    # ── connection ────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """Return True if the socket connection is established."""
        return self._writer is not None and not self._writer.is_closing()

    async def _open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._server_address, self._port),
                timeout=15,
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise SensioConnectionError(
                f"Failed to connect to {self._server_address}:{self._port}: {err}"
            ) from err
        _enable_keepalive(self._writer)
        self._connected_event.set()
        self._notify_connection(True)

    async def connect(self) -> None:
        """Establish the connection and start the background read loop.

        Raises SensioConnectionError if the first connection attempt fails.
        """
        self._closing = False
        await self._open()
        self._task = asyncio.create_task(self._run(), name=f"sensio-{self._server_address}")

    def _drop(self) -> None:
        """Mark the current connection as gone (idempotent)."""
        was_connected = self._connected_event.is_set()
        self._connected_event.clear()
        if self._writer is not None:
            self._writer.close()
        self._reader = None
        self._writer = None
        if was_connected:
            self._notify_connection(False)

    async def _run(self) -> None:
        """Read frames forever; reconnect with backoff when the connection drops."""
        delay = self._backoff_initial
        while not self._closing:
            reader = self._reader
            if reader is not None:
                frames = FrameBuffer()
                try:
                    while True:
                        data = await reader.read(4096)
                        if not data:
                            raise ConnectionError("connection closed by controller")
                        for text in frames.feed(data):
                            self._dispatch(parse_message(text))
                        delay = self._backoff_initial
                except asyncio.CancelledError:
                    raise
                except (OSError, ConnectionError) as err:
                    if self._closing:
                        return
                    logger.warning("Lost connection to Sensio controller %s: %s",
                                   self._server_address, err)
                self._drop()
            if self._closing or not self._reconnect:
                return
            wait = delay * random.uniform(0.8, 1.2)
            logger.info("Reconnecting to Sensio controller %s in %.1f s", self._server_address, wait)
            await asyncio.sleep(wait)
            try:
                await self._open()
                logger.info("Reconnected to Sensio controller %s", self._server_address)
            except SensioConnectionError as err:
                logger.debug("Reconnect failed: %s", err)
                delay = min(delay * 2, self._backoff_max)

    async def force_reconnect(self) -> None:
        """Close the current socket so the read loop reconnects (for diagnostics/tests).

        Sends nothing to the controller.
        """
        if self._writer is not None:
            self._writer.close()

    async def send_command(self, command: str) -> None:
        """Send a command to the Sensio controller.

        If the connection is down, waits up to ``command_wait_timeout`` seconds
        for the background task to reconnect before giving up.
        """
        if not self.connected:
            try:
                await asyncio.wait_for(self._connected_event.wait(), self._command_wait_timeout)
            except asyncio.TimeoutError:
                raise SensioConnectionError("Not connected to controller") from None

        async with self._lock:
            writer = self._writer
            if writer is None or writer.is_closing():
                raise SensioConnectionError("Not connected to controller")
            try:
                full_command = f"{COMMAND_PREFIX}{command}{COMMAND_POSTFIX}"
                writer.write(full_command.encode("utf-8"))
                await writer.drain()
            except OSError as err:
                writer.close()  # the read loop notices and reconnects
                raise SensioCommandError(f"Failed to send command: {err}") from err

    async def close(self) -> None:
        """Stop reconnecting and close the socket connection."""
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._writer is not None and not self._writer.is_closing():
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = None
        self._writer = None
        self._connected_event.clear()
