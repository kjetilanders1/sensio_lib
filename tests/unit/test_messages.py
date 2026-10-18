"""Frame splitting and message parsing (synthetic data in the controller's format)."""

from sensio_lib.messages import (
    ConnectMessage,
    EndMessage,
    FrameBuffer,
    RsnMessage,
    UnknownMessage,
    parse_message,
)


def frame(text: str) -> bytes:
    return b"\x01" + text.encode() + b"\x02\x00"


def test_parse_relay_and_memory_values():
    relay_on = parse_message("RSN 1201 R_LysStue 8 1 1 0")
    relay_off = parse_message("RSN 1201 R_LysStue 8 1 0 0")
    state = parse_message("RSN 1202 M_R_LysStue_State 23 1 0 1.00")
    assert isinstance(relay_on, RsnMessage) and relay_on.address == 1201 and relay_on.name == "R_LysStue"
    assert (relay_on.value, relay_off.value, state.value) == (1.0, 0.0, 1.0)
    assert parse_message("RSN 1203 M_HouseScene 23 1 0 -1.00").value == -1.0


def test_parse_other_messages():
    assert parse_message("end 1201") == EndMessage(1201)
    assert isinstance(parse_message('<connect sn="00-000" ip="192.0.2.1" fw="test"/>\r\n'), ConnectMessage)
    assert isinstance(parse_message("DS2 1 0 1 16 62"), UnknownMessage)
    assert isinstance(parse_message("RSN notanumber x 8"), UnknownMessage)


def test_frame_buffer_splits_and_keeps_partial_frames():
    buf = FrameBuffer()
    data = frame("RSN 1 A 8 1 1 0") + frame("end 1") + frame("RSN 2 B 23 1 0 0.00")
    first = len(frame("RSN 1 A 8 1 1 0"))
    assert buf.feed(data[:10]) == []
    assert buf.feed(data[10:first + 3]) == ["RSN 1 A 8 1 1 0"]   # second frame only partly received
    assert buf.feed(data[first + 3:]) == ["end 1", "RSN 2 B 23 1 0 0.00"]


def test_frame_buffer_drops_garbage():
    buf = FrameBuffer(max_size=16)
    assert buf.feed(b"garbage without frames") == []
    assert buf.feed(b"\x01" + b"x" * 40) == []   # oversized partial frame is discarded
    assert buf.feed(frame("end 7")) == ["end 7"]
