"""Parsing of messages pushed by the Sensio controller on the local socket.

The controller frames every message as ``\\x01<text>\\x02`` (often followed by
a ``\\x00`` byte). Observed message kinds:

- ``<connect sn="..." ip="..." fw="..." .../>`` once, right after connecting.
- ``RSN <address> <name> <type> <f1> <f2> <f3>``: a function changed state.
  For relay outputs (type 8) the on/off state is ``f2`` (1/0); for memory
  values (type 23, e.g. ``*_State`` functions) the value is ``f3`` (float).
- ``end <address>``: the controller finished processing a command.
- Other lines (e.g. ``DS2 ...``) are passed through as ``UnknownMessage``.
"""

from __future__ import annotations

from dataclasses import dataclass

FRAME_START = b"\x01"
FRAME_END = b"\x02"

RELAY_TYPE = 8
MEMORY_TYPE = 23


@dataclass(frozen=True)
class RsnMessage:
    """A state notification for one controller function."""

    address: int
    name: str
    type: int
    fields: tuple[str, ...]

    @property
    def value(self) -> float | None:
        """Numeric value of the function, or None if not understood.

        Relays (type 8) report on/off in the second field; memory values
        (type 23) report their value in the last field.
        """
        try:
            if self.type == RELAY_TYPE and len(self.fields) >= 2:
                return float(self.fields[1])
            if self.fields:
                return float(self.fields[-1])
        except ValueError:
            return None
        return None


@dataclass(frozen=True)
class EndMessage:
    """Controller finished processing the command for ``address``."""

    address: int


@dataclass(frozen=True)
class ConnectMessage:
    """Greeting sent by the controller when a client connects."""

    raw: str


@dataclass(frozen=True)
class UnknownMessage:
    """Any other message; kept so callers can log or inspect it."""

    raw: str


Message = RsnMessage | EndMessage | ConnectMessage | UnknownMessage


def parse_message(text: str) -> Message:
    """Parse the text of one frame (without the framing bytes)."""
    text = text.strip("\x00\r\n ")
    if text.startswith("<connect"):
        return ConnectMessage(text)
    parts = text.split()
    try:
        if len(parts) >= 4 and parts[0] == "RSN":
            return RsnMessage(int(parts[1]), parts[2], int(parts[3]), tuple(parts[4:]))
        if len(parts) == 2 and parts[0] == "end":
            return EndMessage(int(parts[1]))
    except ValueError:
        pass
    return UnknownMessage(text)


class FrameBuffer:
    """Accumulates raw bytes and yields complete frames."""

    def __init__(self, max_size: int = 1 << 20) -> None:
        self._buf = bytearray()
        self._max = max_size

    def feed(self, data: bytes) -> list[str]:
        """Add bytes and return the text of every complete frame."""
        self._buf += data
        frames: list[str] = []
        while True:
            start = self._buf.find(FRAME_START)
            if start < 0:
                self._buf.clear()
                break
            end = self._buf.find(FRAME_END, start + 1)
            if end < 0:
                if start:
                    del self._buf[:start]
                break
            frames.append(self._buf[start + 1:end].decode("utf-8", errors="replace"))
            del self._buf[:end + 1]
        if len(self._buf) > self._max:  # garbage without a frame end; never grow unbounded
            self._buf.clear()
        return frames
