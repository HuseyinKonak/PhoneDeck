"""scrcpy v3 wire protocol: control message encoders and stream parsers."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

SERVER_VERSION = "3.3.3"
DEVICE_NAME_LENGTH = 64
CODEC_META_LENGTH = 12
FRAME_HEADER_LENGTH = 12
MAX_TEXT_BYTES = 300
POINTER_ID_FINGER = -2

_FLAG_CONFIG = 1 << 63
_FLAG_KEY_FRAME = 1 << 62
_PTS_MASK = _FLAG_KEY_FRAME - 1
POINTER_ID_VIRTUAL_FINGER = -3
_PRESSURE_MAX = 0xFFFF
_SCROLL_DIVISOR = 16
_I16_SCALE = 0x8000
_I16_MAX = 0x7FFF


class Action(IntEnum):
    DOWN = 0
    UP = 1
    MOVE = 2


class ControlType(IntEnum):
    INJECT_KEYCODE = 0
    INJECT_TEXT = 1
    INJECT_TOUCH = 2
    INJECT_SCROLL = 3
    BACK_OR_SCREEN_ON = 4
    EXPAND_NOTIFICATION_PANEL = 5
    EXPAND_SETTINGS_PANEL = 6
    COLLAPSE_PANELS = 7
    SET_CLIPBOARD = 9
    SET_DISPLAY_POWER = 10
    ROTATE_DEVICE = 11
    RESET_VIDEO = 17


class Key(IntEnum):
    """Android keycodes used by the app."""

    HOME = 3
    BACK = 4
    DPAD_UP = 19
    DPAD_DOWN = 20
    DPAD_LEFT = 21
    DPAD_RIGHT = 22
    VOLUME_UP = 24
    VOLUME_DOWN = 25
    POWER = 26
    TAB = 61
    SPACE = 62
    ENTER = 66
    DEL = 67
    MEDIA_PLAY_PAUSE = 85
    MEDIA_NEXT = 87
    MEDIA_PREVIOUS = 88
    PAGE_UP = 92
    PAGE_DOWN = 93
    FORWARD_DEL = 112
    MOVE_HOME = 122
    MOVE_END = 123
    VOLUME_MUTE = 164
    APP_SWITCH = 187


@dataclass(frozen=True)
class FrameHeader:
    is_config: bool
    is_key_frame: bool
    size: int
    pts_us: int = 0


def parse_frame_header(data: bytes) -> FrameHeader:
    pts_and_flags, size = struct.unpack(">QI", data)
    return FrameHeader(
        is_config=bool(pts_and_flags & _FLAG_CONFIG),
        is_key_frame=bool(pts_and_flags & _FLAG_KEY_FRAME),
        size=size,
        pts_us=pts_and_flags & _PTS_MASK,
    )


def parse_device_name(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("utf-8", "replace")


def keycode(action: Action, key: int, metastate: int = 0) -> bytes:
    return struct.pack(">BBiii", ControlType.INJECT_KEYCODE, action, key, 0, metastate)


def key_press(key: int) -> bytes:
    return keycode(Action.DOWN, key) + keycode(Action.UP, key)


def text(value: str) -> bytes:
    """Encode text injection, split so each message stays under the limit."""
    messages = []
    chunk = b""
    for char in value:
        encoded = char.encode("utf-8")
        if len(chunk) + len(encoded) > MAX_TEXT_BYTES:
            messages.append(chunk)
            chunk = b""
        chunk += encoded
    if chunk:
        messages.append(chunk)
    return b"".join(
        struct.pack(">BI", ControlType.INJECT_TEXT, len(m)) + m for m in messages
    )


def touch(
    action: Action, x: int, y: int, width: int, height: int, pointer_id: int = POINTER_ID_FINGER
) -> bytes:
    pressure = 0 if action == Action.UP else _PRESSURE_MAX
    return struct.pack(
        ">BBqiiHHHII",
        ControlType.INJECT_TOUCH,
        action,
        pointer_id,
        x,
        y,
        width,
        height,
        pressure,
        0,
        0,
    )


def scroll(x: int, y: int, width: int, height: int, horizontal: float, vertical: float) -> bytes:
    return struct.pack(
        ">BiiHHhhI",
        ControlType.INJECT_SCROLL,
        x,
        y,
        width,
        height,
        _to_fixed_point(horizontal / _SCROLL_DIVISOR),
        _to_fixed_point(vertical / _SCROLL_DIVISOR),
        0,
    )


def set_clipboard(value: str, paste: bool) -> bytes:
    encoded = value.encode("utf-8")
    header = struct.pack(">BQBI", ControlType.SET_CLIPBOARD, 0, int(paste), len(encoded))
    return header + encoded


def set_display_power(is_on: bool) -> bytes:
    return struct.pack(">BB", ControlType.SET_DISPLAY_POWER, int(is_on))


def simple(control_type: ControlType) -> bytes:
    """Messages with no payload (panels, rotate)."""
    return struct.pack(">B", control_type)


def _to_fixed_point(value: float) -> int:
    clamped = min(max(value, -1.0), 1.0)
    return min(int(clamped * _I16_SCALE), _I16_MAX)
