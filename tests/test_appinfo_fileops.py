import struct
from pathlib import Path

import pytest

from phonedeck.core import appinfo, fileops
from phonedeck.core.adb import AdbError
from phonedeck.core.device import SEPARATOR, is_protected_path
from phonedeck.core.scrcpy import protocol
from phonedeck.core.scrcpy.client import ScrcpyOptions
from phonedeck.core.shell import ShellSession

PACKAGE_DUMP = """Packages:
  Package [com.example.app] (abc):
    userId=10321
    codePath=/data/app/~~x==/com.example.app-y==
    versionCode=1606100 minSdk=30 targetSdk=36
    versionName=16.61.0
    pkgFlags=[ HAS_CODE ALLOW_CLEAR_USER_DATA ]
    lastUpdateTime=2026-10-02 10:56:29
    installerPackageName=com.android.vending
    requested permissions:
      android.permission.INTERNET
    install permissions:
      android.permission.INTERNET: granted=true
      android.permission.SYSTEM_ALERT_WINDOW: granted=false
    User 0: ceDataInode=1 installed=true hidden=false stopped=false enabled=0 ofs=0
      dataDir=/data/user/0/com.example.app
      firstInstallTime=2025-06-11 15:10:33
      runtime permissions:
        android.permission.RECORD_AUDIO: granted=true, flags=[ USER_SET ]
        android.permission.ACCESS_COARSE_LOCATION: granted=false, flags=[ ]

      disabledComponents:
        android.permission.FAKE: granted=true
"""
PATHS = "package:/data/app/x/base.apk\npackage:/data/app/x/split_config.tr.apk\n"
DISKSTATS = (
    'Package Names: ["com.other","com.example.app"]\n'
    "App Sizes: [10,2000]\nApp Data Sizes: [20,300]\nCache Sizes: [30,40]\n"
)


class FakeAdb:
    def __init__(self, shell_output: str = "") -> None:
        self.shell_output = shell_output
        self.shell_calls: list[str] = []
        self.run_calls: list[list[str]] = []

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.shell_calls.append(command)
        return self.shell_output

    def run(self, args: list[str], serial: str | None = None, timeout: float = 30) -> str:
        self.run_calls.append(args)
        return ""

    def run_bytes(self, args: list[str], serial: str | None = None, timeout: float = 30) -> bytes:
        self.run_calls.append(args)
        return "içerik".encode()


def test_parse_details_reads_versions_dates_and_state():
    details = appinfo.parse_details("com.example.app", PACKAGE_DUMP, PATHS, DISKSTATS)

    assert (details.version_name, details.version_code) == ("16.61.0", "1606100")
    assert (details.min_sdk, details.target_sdk) == ("30", "36")
    assert details.first_install == "2025-06-11 15:10:33"
    assert details.installer == "com.android.vending"
    assert (details.is_enabled, details.is_system) == (True, False)
    assert details.apk_paths == ("/data/app/x/base.apk", "/data/app/x/split_config.tr.apk")
    assert (details.sizes.app, details.sizes.data, details.sizes.cache, details.sizes.total) == (2000, 300, 40, 2340)


def test_parse_permissions_separates_runtime_from_install_time():
    permissions = appinfo.parse_permissions(PACKAGE_DUMP)

    assert [(p.short_name, p.is_granted, p.is_runtime) for p in permissions] == [
        ("ACCESS_COARSE_LOCATION", False, True),
        ("RECORD_AUDIO", True, True),
        ("INTERNET", True, False),
        ("SYSTEM_ALERT_WINDOW", False, False),
    ]


def test_parse_details_detects_disabled_and_system_apps():
    dump = PACKAGE_DUMP.replace("enabled=0", "enabled=3").replace("[ HAS_CODE", "[ SYSTEM HAS_CODE")

    details = appinfo.parse_details("com.example.app", dump, "", "garbage")

    assert (details.is_enabled, details.is_system) == (False, True)
    assert details.sizes.total == 0


def test_read_details_validates_package_and_output():
    adb = FakeAdb(f"{PACKAGE_DUMP}{SEPARATOR}{PATHS}{SEPARATOR}{DISKSTATS}")

    details = appinfo.read_details(adb, "serial", "com.example.app")

    assert details.version_name == "16.61.0"
    with pytest.raises(ValueError):
        appinfo.read_details(adb, "serial", "com.app; reboot")
    with pytest.raises(AdbError):
        appinfo.read_details(FakeAdb("incomplete"), "serial", "com.example.app")


def test_app_actions_build_expected_commands():
    adb = FakeAdb()

    appinfo.set_permission(adb, "s", "com.a.b", "android.permission.CAMERA", True)
    appinfo.set_permission(adb, "s", "com.a.b", "android.permission.CAMERA", False)
    appinfo.set_enabled(adb, "s", "com.a.b", False)
    appinfo.set_enabled(adb, "s", "com.a.b", True)
    appinfo.clear_data(adb, "s", "com.a.b")
    appinfo.open_settings(adb, "s", "com.a.b")

    assert adb.shell_calls == [
        "pm grant com.a.b android.permission.CAMERA",
        "pm revoke com.a.b android.permission.CAMERA",
        "pm disable-user --user 0 com.a.b",
        "pm enable com.a.b",
        "pm clear com.a.b",
        "am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d package:com.a.b",
    ]


def test_set_permission_rejects_injection():
    adb = FakeAdb()

    with pytest.raises(ValueError):
        appinfo.set_permission(adb, "s", "com.a.b", "android.permission.X; reboot", True)
    assert adb.shell_calls == []


def test_extract_apks_pulls_every_split(tmp_path: Path):
    adb = FakeAdb()
    details = appinfo.parse_details("com.example.app", PACKAGE_DUMP, PATHS, DISKSTATS)

    saved = appinfo.extract_apks(adb, "s", details, str(tmp_path))

    assert [path.name for path in saved] == [
        "com.example.app-16.61.0-base.apk",
        "com.example.app-16.61.0-split_config.tr.apk",
    ]
    assert adb.run_calls[0][:2] == ["pull", "/data/app/x/base.apk"]
    with pytest.raises(AdbError):
        appinfo.extract_apks(adb, "s", appinfo.AppDetails("com.x.y"), str(tmp_path))


def test_rename_refuses_overwrite_and_bad_names():
    adb = FakeAdb()

    assert fileops.rename(adb, "s", "/sdcard/a b.txt", "c'd.txt") == "/sdcard/c'd.txt"
    assert "mv -- '/sdcard/a b.txt' '/sdcard/c'\"'\"'d.txt'" in adb.shell_calls[0]
    for bad in ("", "..", "x/y"):
        with pytest.raises(ValueError):
            fileops.rename(adb, "s", "/sdcard/a.txt", bad)
    with pytest.raises(ValueError):
        fileops.rename(adb, "s", "/sdcard", "other")
    with pytest.raises(AdbError):
        fileops.rename(FakeAdb(fileops.EXISTS_MARKER), "s", "/sdcard/a.txt", "b.txt")


def test_copy_and_move_quote_paths_and_block_self_nesting():
    adb = FakeAdb()

    fileops.copy_into(adb, "s", ["/sdcard/a b", "/sdcard/c"], "/sdcard/Download")
    fileops.move_into(adb, "s", ["/sdcard/c"], "/sdcard/Download/")
    fileops.copy_into(adb, "s", [], "/sdcard")

    assert adb.shell_calls == [
        "cp -r -- '/sdcard/a b' /sdcard/c /sdcard/Download/",
        "mv -- /sdcard/c /sdcard/Download/",
    ]
    with pytest.raises(ValueError):
        fileops.copy_into(adb, "s", ["/sdcard/dir"], "/sdcard/dir/inner")
    with pytest.raises(ValueError):
        fileops.move_into(adb, "s", ["/sdcard"], "/data/local/tmp")


def test_read_text_and_disk_usage():
    adb = FakeAdb("1.2G\t/sdcard/DCIM\n")

    assert fileops.read_text(adb, "s", "/sdcard/a b.txt", limit=50) == "içerik"
    assert adb.run_calls[0] == ["exec-out", "head -c 50 -- '/sdcard/a b.txt'"]
    assert fileops.disk_usage(adb, "s", "/sdcard/DCIM") == "1.2G"
    assert is_protected_path("/sdcard/") and not is_protected_path("/sdcard/DCIM")


def test_stream_options_become_server_arguments():
    default = ScrcpyOptions().server_arguments()
    custom = ScrcpyOptions(max_size=0, bit_rate=2_000_000, max_fps=30, stay_awake=True, show_touches=True)

    assert default == ["audio=false", "control=true", "max_size=1920", "video_bit_rate=8000000", "max_fps=60"]
    assert custom.server_arguments() == [
        "audio=false", "control=true", "max_size=0", "video_bit_rate=2000000", "max_fps=30",
        "stay_awake=true", "show_touches=true",
    ]


def test_touch_supports_second_pointer_and_header_carries_pts():
    message = protocol.touch(protocol.Action.DOWN, 1, 2, 3, 4, pointer_id=protocol.POINTER_ID_VIRTUAL_FINGER)
    header = protocol.parse_frame_header(struct.pack(">QI", (1 << 62) | 123456, 9))

    assert struct.unpack(">BBqiiHHHII", message)[2] == -3
    assert (header.pts_us, header.is_key_frame, header.size) == (123456, True, 9)
    assert protocol.simple(protocol.ControlType.RESET_VIDEO) == bytes([17])


class FakeProcess:
    def __init__(self, chunks: list[bytes]) -> None:
        self.stdout = self
        self.stdin = self
        self._chunks = list(chunks)
        self.written = b""
        self.is_terminated = False

    def read1(self, size: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def write(self, data: bytes) -> None:
        self.written += data

    def flush(self) -> None:
        pass

    def poll(self):
        return 0 if self.is_terminated else None

    def terminate(self) -> None:
        self.is_terminated = True

    def wait(self, timeout: float) -> int:
        return 0


def test_shell_session_streams_output_and_sends_commands():
    import threading

    encoded = "çıktı\r\n".encode()
    process = FakeProcess([encoded[:2], encoded[2:]])  # split inside a multi-byte character
    output: list[str] = []
    exited = threading.Event()

    class Adb:
        def popen(self, args, serial, with_stdin=False):
            assert (args, with_stdin) == (["shell"], True)
            return process

    session = ShellSession(Adb(), "s", output.append, exited.set)
    session.start()

    assert exited.wait(2)
    assert "".join(output) == "çıktı\n"
    assert session.send("ls -la") is True and process.written == b"ls -la\n"
    session.stop()
    assert process.is_terminated and session.send("x") is False
