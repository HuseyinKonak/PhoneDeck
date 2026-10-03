import subprocess

import pytest

from phonedeck.core import adb as adb_module
from phonedeck.core.adb import Adb, AdbError
from phonedeck.core.device import SEPARATOR, DeviceService, normalize_path, validate_package


class FakeAdb:
    """Records calls and replies with canned shell output."""

    def __init__(self, shell_output: str = "") -> None:
        self.shell_output = shell_output
        self.shell_calls: list[str] = []
        self.run_calls: list[list[str]] = []

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.shell_calls.append(command)
        return self.shell_output

    def run(self, args: list[str], serial: str | None = None, timeout: float = 30) -> str:
        self.run_calls.append(args)
        return "connected"

    def run_bytes(self, args: list[str], serial: str | None = None, timeout: float = 30) -> bytes:
        self.run_calls.append(args)
        return b"png"

    def connect(self, address: str) -> str:
        return f"connected to {address}"


def make_service(output: str = "") -> tuple[DeviceService, FakeAdb]:
    fake = FakeAdb(output)
    return DeviceService(fake, "serial"), fake


def test_info_parses_all_sections():
    sections = [
        "CPH2631", "OPPO", "16", "36", "Physical size: 720x1604", "inet 192.168.1.7/24",
        "level: 80\nUSB powered: true\ntemperature: 300",
        "Filesystem 1K-blocks Used Available\n/dev 1000 500 500",
        "MemTotal: 2000 kB\nMemAvailable: 500 kB",
    ]
    service, _ = make_service(f"\n{SEPARATOR}\n".join(sections))

    info = service.info()

    assert (info.model, info.brand, info.android_version) == ("CPH2631", "OPPO", "16")
    assert (info.resolution, info.ip_address) == ("720x1604", "192.168.1.7")
    assert (info.battery.level, info.storage.percent, info.memory.percent) == (80, 50, 75)


def test_info_raises_when_output_is_incomplete():
    service, _ = make_service("only one section")

    with pytest.raises(AdbError):
        service.info()


def test_toggles_reads_every_reported_state():
    sections = [
        "1", "0", "1", "0", "2",
        "mWakefulness=Asleep",
        "state=PlaybackState {state=PLAYING(3), position=0}",
        "- STREAM_MUSIC:\n   Muted: true",
        "volume is 5 in range [0..16]",
        "AudioManager.getStreamVolume(3) -> 103",
        "AudioManager.getStreamMaxVolume(3) -> 160",
        "1600",
        "mScreenBrightnessRangeMinimum=8.0\nmScreenBrightnessNormalMaximum=3319.0\n"
        "mScreenBrightnessRangeMinimum=0.0\nmScreenBrightnessNormalMaximum=0.0",
    ]
    service, _ = make_service(f"\n{SEPARATOR}\n".join(sections))

    state = service.toggles()

    assert (state.wifi, state.bluetooth, state.mobile_data) == (True, False, True)
    assert (state.auto_rotate, state.do_not_disturb) == (False, True)
    assert (state.is_awake, state.is_playing, state.is_muted) == (False, True, True)
    assert (state.volume.current, state.volume.maximum) == (103, 160)
    assert (state.brightness.current, state.brightness.minimum, state.brightness.maximum) == (1600, 8, 3319)


def test_toggles_falls_back_when_audio_command_is_unsupported():
    sections = ["1", "1", "1", "1", "0", "", "", "", "volume is 5 in range [0..16]",
                "No shell command implementation.", "", "null", ""]
    service, _ = make_service(f"\n{SEPARATOR}\n".join(sections))

    state = service.toggles()

    assert (state.volume.current, state.volume.maximum) == (5, 16)
    assert (state.brightness.current, state.brightness.minimum, state.brightness.maximum) == (128, 1, 255)


def test_toggles_raises_when_output_is_incomplete():
    service, _ = make_service("1")

    with pytest.raises(AdbError):
        service.toggles()


def test_switch_setters_build_expected_shell_lines():
    service, fake = make_service()

    service.set_mobile_data(True)
    service.set_auto_rotate(False)
    service.set_do_not_disturb(True)

    assert fake.shell_calls == [
        "svc data enable",
        "settings put system accelerometer_rotation 0",
        "cmd notification set_dnd on",
    ]


def test_control_commands_build_expected_shell_lines():
    service, fake = make_service()

    service.set_wifi(False)
    service.set_bluetooth(True)
    service.set_brightness(250)
    service.set_volume(7)
    service.keyevent(26)
    service.open_url("https://example.com/?a=1&b=2; rm -rf /")

    assert fake.shell_calls[:5] == [
        "svc wifi disable",
        "svc bluetooth enable",
        "settings put system screen_brightness_mode 0; settings put system screen_brightness 250",
        "cmd audio set-volume 3 7 || cmd media_session volume --stream 3 --set 7",
        "input keyevent 26",
    ]
    assert fake.shell_calls[5].endswith("'https://example.com/?a=1&b=2; rm -rf /'")


def test_app_commands_reject_invalid_package_names():
    service, fake = make_service()

    for action in (service.launch_app, service.stop_app, service.uninstall_app):
        with pytest.raises(ValueError):
            action("com.app; reboot")
    assert fake.shell_calls == [] and fake.run_calls == []


def test_app_commands_use_validated_package():
    service, fake = make_service("package:com.a.b\n")

    service.launch_app("com.a.b")
    service.stop_app("com.a.b")
    service.uninstall_app("com.a.b")
    service.install_apk("C:/x.apk")

    assert service.packages() == ("com.a.b",)
    assert "monkey -p com.a.b" in fake.shell_calls[0]
    assert fake.shell_calls[1] == "am force-stop com.a.b"
    assert fake.run_calls == [["uninstall", "com.a.b"], ["install", "-r", "C:/x.apk"]]
    assert fake.shell_calls[-1] == "pm list packages -3"


def test_apps_pushes_helper_and_parses_labels():
    service, fake = make_service("APP\tcom.whatsapp\t0\tWhatsApp\n")

    apps = service.apps(include_system=True)

    assert [(a.package, a.label) for a in apps] == [("com.whatsapp", "WhatsApp")]
    assert fake.run_calls[0][0] == "push" and fake.run_calls[0][2] == "/data/local/tmp/phonedeck-applister.jar"
    assert fake.shell_calls[0].endswith("phonedeck.AppLister /data/local/tmp/phonedeck-icons 96 all")


def test_apps_raises_when_helper_prints_nothing_usable():
    service, _ = make_service("java.lang.RuntimeException: boom")

    with pytest.raises(AdbError):
        service.apps()


def test_pull_icons_copies_remote_icon_folder():
    service, fake = make_service()

    service.pull_icons("C:/cache/icons")

    assert fake.run_calls == [["pull", "/data/local/tmp/phonedeck-icons/.", "C:/cache/icons"]]


def test_sections_run_in_parallel_and_keep_order():
    service, fake = make_service(f"one\n{SEPARATOR}\ntwo")

    assert service._run_sections(("echo 1", "echo 2 | cat")) == ["one", "two"]
    script = fake.shell_calls[0]
    assert "(echo 1) >/data/local/tmp/phonedeck-state/$$/0 2>/dev/null &" in script
    assert "(echo 2 | cat) >/data/local/tmp/phonedeck-state/$$/1 2>/dev/null &" in script
    assert script.index("wait;") < script.index("cat /data/local/tmp/phonedeck-state/$$/0")
    assert script.endswith("rm -rf /data/local/tmp/phonedeck-state/$$")


def test_message_and_call_queries_limit_rows():
    service, fake = make_service("Row: 0 number=1, type=1, date=5, duration=2, name=NULL\n")

    calls = service.call_log(limit=10)
    service.sms(limit=5)
    service.contacts()
    service.notifications()

    assert calls[0].number == "1"
    assert "content://call_log/calls" in fake.shell_calls[0] and "LIMIT 10'" in fake.shell_calls[0]
    assert "content://sms" in fake.shell_calls[1] and "LIMIT 5'" in fake.shell_calls[1]
    assert "content://com.android.contacts/data/phones" in fake.shell_calls[2]
    assert fake.shell_calls[3].startswith("dumpsys notification --noredact | grep -E")


def test_dial_and_compose_validate_and_quote():
    service, fake = make_service()

    service.dial("+90 (505) 111-22 33")
    service.dial("*100#")
    service.compose_sms("05051112233", "selam; reboot 'now'")
    service.answer_call()
    service.end_call()

    assert fake.shell_calls == [
        "am start -a android.intent.action.CALL -d tel:+905051112233",
        "am start -a android.intent.action.CALL -d tel:*100%23",
        "am start -a android.intent.action.SENDTO -d sms:05051112233 --es sms_body "
        "'selam; reboot '\"'\"'now'\"'\"''",
        "input keyevent 5",
        "input keyevent 6",
    ]


@pytest.mark.parametrize("number", ["", "abc", "123; reboot", "1" * 25, "$(id)"])
def test_dial_rejects_invalid_numbers(number):
    service, fake = make_service()

    with pytest.raises(ValueError):
        service.dial(number)
    with pytest.raises(ValueError):
        service.compose_sms(number, "x")
    assert fake.shell_calls == []


def test_file_commands_quote_paths():
    service, fake = make_service()

    service.list_dir("/sdcard/My Files")
    service.make_dir("/sdcard/new dir")
    service.delete("/sdcard/a'b")
    service.pull("/sdcard/a.txt", "C:/a.txt")
    service.push("C:/a.txt", "/sdcard/Download")

    assert fake.shell_calls == [
        "ls -la '/sdcard/My Files/'",
        "mkdir -p '/sdcard/new dir'",
        "rm -rf -- '/sdcard/a'\"'\"'b'",
    ]
    assert fake.run_calls == [
        ["pull", "/sdcard/a.txt", "C:/a.txt"],
        ["push", "C:/a.txt", "/sdcard/Download/"],
    ]


@pytest.mark.parametrize("path", ["/", "/sdcard", "/sdcard/", "/sdcard/../sdcard", "", "/storage/emulated/0"])
def test_delete_refuses_protected_paths(path):
    service, fake = make_service()

    with pytest.raises(ValueError):
        service.delete(path)
    assert fake.shell_calls == []


def test_misc_device_commands():
    service, fake = make_service("out")

    assert service.screenshot() == b"png"
    assert service.run_command("ls") == "out"
    assert service.enable_wireless("10.0.0.2") == "connected to 10.0.0.2:5555"
    service.reboot()

    assert fake.run_calls == [["exec-out", "screencap", "-p"], ["tcpip", "5555"], ["reboot"]]


def test_run_command_returns_error_text_instead_of_raising():
    service, fake = make_service()
    fake.shell = lambda *args, **kwargs: (_ for _ in ()).throw(AdbError("not found"))

    assert service.run_command("nope") == "not found"


def test_normalize_path_and_validate_package():
    assert normalize_path("sdcard//a/../b/") == "/sdcard/b"
    assert normalize_path("  ") == "/"
    assert validate_package("com.example_app.x1") == "com.example_app.x1"


def _completed(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_adb_run_builds_command_and_decodes_output(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _completed(0, "çıktı".encode())

    monkeypatch.setattr(adb_module.subprocess, "run", fake_run)
    adb = Adb("adb")

    assert adb.shell("abc", "echo hi") == "çıktı"
    assert calls == [["adb", "-s", "abc", "shell", "echo hi"]]


def test_adb_run_raises_on_failure_and_timeout(monkeypatch):
    adb = Adb("adb")
    monkeypatch.setattr(adb_module.subprocess, "run", lambda *a, **k: _completed(1, b"", b"device offline"))
    with pytest.raises(AdbError, match="device offline"):
        adb.run(["shell", "ls"])

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("adb", 1)

    monkeypatch.setattr(adb_module.subprocess, "run", raise_timeout)
    with pytest.raises(AdbError, match="zaman aşımı"):
        adb.run(["shell", "ls"])


def test_adb_devices_and_connect(monkeypatch):
    output = b"List of devices attached\nabc device model:Pixel\n"
    monkeypatch.setattr(adb_module.subprocess, "run", lambda *a, **k: _completed(0, output))
    adb = Adb("adb")

    assert adb.devices()[0].model == "Pixel"
    assert adb.connect("1.2.3.4:5555").startswith("List of devices")
    assert adb.disconnect("1.2.3.4:5555").startswith("List of devices")


def test_find_adb_raises_when_missing(monkeypatch):
    monkeypatch.setattr(adb_module.shutil, "which", lambda name, path=None: None)
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", "Z:/does-not-exist")

    with pytest.raises(AdbError):
        adb_module.find_adb()


def test_adb_timeout_message_never_contains_the_shell_command(monkeypatch):
    adb = Adb("adb")

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("adb", 1)

    monkeypatch.setattr(adb_module.subprocess, "run", raise_timeout)

    with pytest.raises(AdbError) as error:
        adb.shell("s", "cmd wifi connect-network Home wpa2 'gizli-parola'")
    assert "gizli-parola" not in str(error.value) and "(shell)" in str(error.value)


def test_find_adb_ignores_an_executable_in_the_current_folder(monkeypatch, tmp_path):
    (tmp_path / "adb.exe").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path / "missing"))

    with pytest.raises(AdbError, match="bulunamadı"):
        adb_module.find_adb()
