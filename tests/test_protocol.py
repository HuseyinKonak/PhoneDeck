import struct

from phonedeck.core.scrcpy import protocol
from phonedeck.core.scrcpy.protocol import Action, ControlType, Key


def test_keycode_message_layout():
    message = protocol.keycode(Action.DOWN, Key.HOME)

    assert message == bytes([0, 0]) + struct.pack(">iii", 3, 0, 0)


def test_key_press_is_down_then_up():
    message = protocol.key_press(Key.BACK)

    assert len(message) == 28
    assert (message[1], message[15]) == (Action.DOWN, Action.UP)


def test_text_message_carries_utf8_length_and_payload():
    message = protocol.text("şa")

    assert message == bytes([1]) + struct.pack(">I", 3) + "şa".encode()


def test_text_splits_long_input_without_breaking_characters():
    message = protocol.text("ş" * 200)

    first_length = struct.unpack(">I", message[1:5])[0]
    assert first_length == protocol.MAX_TEXT_BYTES
    assert message[5 : 5 + first_length].decode() == "ş" * 150
    assert protocol.text("") == b""


def test_touch_message_is_32_bytes_with_position_and_pressure():
    down = protocol.touch(Action.DOWN, 10, 20, 720, 1600)
    up = protocol.touch(Action.UP, 10, 20, 720, 1600)

    assert len(down) == 32
    assert struct.unpack(">BBqiiHHHII", down) == (2, 0, -2, 10, 20, 720, 1600, 0xFFFF, 0, 0)
    assert struct.unpack(">BBqiiHHHII", up)[7] == 0


def test_scroll_message_clamps_to_fixed_point_range():
    message = protocol.scroll(1, 2, 720, 1600, 0, 100)
    fields = struct.unpack(">BiiHHhhI", message)

    assert len(message) == 21
    assert fields[:5] == (3, 1, 2, 720, 1600)
    assert fields[5:7] == (0, 0x7FFF)
    assert struct.unpack(">BiiHHhhI", protocol.scroll(0, 0, 1, 1, 0, -100))[6] == -0x8000


def test_set_clipboard_message_layout():
    message = protocol.set_clipboard("hi", paste=True)

    assert message == bytes([9]) + struct.pack(">QBI", 0, 1, 2) + b"hi"


def test_simple_and_display_power_messages():
    assert protocol.simple(ControlType.ROTATE_DEVICE) == bytes([11])
    assert protocol.set_display_power(False) == bytes([10, 0])


def test_parse_frame_header_reads_flags_and_size():
    config = protocol.parse_frame_header(struct.pack(">QI", 1 << 63, 30))
    key = protocol.parse_frame_header(struct.pack(">QI", (1 << 62) | 1234, 500))

    assert (config.is_config, config.is_key_frame, config.size) == (True, False, 30)
    assert (key.is_config, key.is_key_frame, key.size) == (False, True, 500)


def test_parse_device_name_strips_padding():
    assert protocol.parse_device_name(b"CPH2631" + b"\x00" * 57) == "CPH2631"
