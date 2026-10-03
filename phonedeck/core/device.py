"""High-level operations on one connected Android device."""

from __future__ import annotations

import posixpath
import re
import shlex
from pathlib import Path

from .adb import Adb, AdbError
from .models import (
    AppInfo,
    CallEntry,
    DeviceInfo,
    FileEntry,
    Notification,
    SmsMessage,
    ToggleState,
)
from .parsers import (
    parse_app_list,
    parse_call_log,
    parse_contacts,
    parse_notifications,
    parse_sms,
    parse_audio_volume,
    parse_brightness,
    parse_battery,
    parse_df,
    parse_ip,
    parse_is_awake,
    parse_is_muted,
    parse_is_playing,
    parse_ls,
    parse_meminfo,
    parse_packages,
    parse_setting_enabled,
    parse_volume,
    parse_wm_size,
)

SEPARATOR = "@@phonedeck@@"
MUSIC_STREAM = 3
TRANSFER_TIMEOUT = 600
TCPIP_PORT = 5555
APP_LISTER_ASSET = Path(__file__).resolve().parents[1] / "assets" / "applister.jar"
REMOTE_APP_LISTER = "/data/local/tmp/phonedeck-applister.jar"
REMOTE_ICON_DIR = "/data/local/tmp/phonedeck-icons"
ICON_SIZE = 96
APP_LIST_TIMEOUT = 120
REMOTE_STATE_DIR = "/data/local/tmp/phonedeck-state"
DEFAULT_ROW_LIMIT = 200
KEYCODE_CALL = 5
KEYCODE_ENDCALL = 6
_PHONE_NUMBER = re.compile(r"\+?[0-9*#]{1,20}")
_PACKAGE = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)+$")
_PROTECTED_PATHS = frozenset({"", "/", "/sdcard", "/storage", "/storage/emulated", "/storage/emulated/0", "/data", "/system"})
_INFO_COMMANDS = (
    "getprop ro.product.model",
    "getprop ro.product.brand",
    "getprop ro.build.version.release",
    "getprop ro.build.version.sdk",
    "wm size",
    "ip -f inet addr show wlan0",
    "dumpsys battery",
    "df -k /data",
    "cat /proc/meminfo",
)


_TOGGLE_COMMANDS = (
    "settings get global wifi_on",
    "settings get global bluetooth_on",
    "settings get global mobile_data",
    "settings get system accelerometer_rotation",
    "settings get global zen_mode",
    "dumpsys power | grep mWakefulness=",
    "dumpsys media_session | grep 'state=PlaybackState'",
    "dumpsys audio | grep -A1 'STREAM_MUSIC:'",
    f"cmd media_session volume --stream {MUSIC_STREAM} --get",
    f"cmd audio get-stream-volume {MUSIC_STREAM}",
    f"cmd audio get-max-volume {MUSIC_STREAM}",
    "settings get system screen_brightness",
    "dumpsys display | grep -E 'mScreenBrightnessRangeMinimum|mScreenBrightnessNormalMaximum'",
)


def validate_package(package: str) -> str:
    if not _PACKAGE.fullmatch(package):
        raise ValueError(f"Geçersiz paket adı: {package}")
    return package


def validate_phone_number(number: str) -> str:
    cleaned = re.sub(r"[\s()\-]", "", number)
    if not _PHONE_NUMBER.fullmatch(cleaned):
        raise ValueError(f"Geçersiz telefon numarası: {number}")
    return cleaned


def normalize_path(path: str) -> str:
    cleaned = posixpath.normpath(path.strip() or "/")
    return cleaned if cleaned.startswith("/") else "/" + cleaned


def is_protected_path(path: str) -> bool:
    """Storage roots that must never be deleted, moved or renamed."""
    return normalize_path(path) in _PROTECTED_PATHS


class DeviceService:
    def __init__(self, adb: Adb, serial: str) -> None:
        self._adb = adb
        self.serial = serial

    def _shell(self, command: str, timeout: float = 30) -> str:
        return self._adb.shell(self.serial, command, timeout)

    def _run_sections(self, commands: tuple[str, ...]) -> list[str]:
        """Run commands concurrently on the phone and return their outputs in order.

        Each Android command starts its own process (~0.2 s), so running them in
        parallel is several times faster than chaining them.
        """
        directory = f"{REMOTE_STATE_DIR}/$$"
        jobs = " ".join(
            f"({command}) >{directory}/{index} 2>/dev/null &"
            for index, command in enumerate(commands)
        )
        reads = f"; echo {SEPARATOR}; ".join(
            f"cat {directory}/{index}" for index in range(len(commands))
        )
        script = f"mkdir -p {directory}; {jobs} wait; {reads}; rm -rf {directory}"
        parts = [part.strip() for part in self._shell(script).split(SEPARATOR)]
        if len(parts) != len(commands):
            raise AdbError("Cihaz durumu okunamadı.")
        return parts

    # --- info -------------------------------------------------------------
    def info(self) -> DeviceInfo:
        model, brand, release, sdk, size, ip, battery, storage, memory = self._run_sections(
            _INFO_COMMANDS
        )
        return DeviceInfo(
            model=model,
            brand=brand,
            android_version=release,
            sdk=sdk,
            resolution=parse_wm_size(size),
            ip_address=parse_ip(ip),
            battery=parse_battery(battery),
            storage=parse_df(storage),
            memory=parse_meminfo(memory),
        )

    def toggles(self) -> ToggleState:
        (wifi, bluetooth, data, rotate, zen, power, media, audio,
         session_volume, audio_volume, audio_maximum, brightness, brightness_range,
         ) = self._run_sections(_TOGGLE_COMMANDS)
        return ToggleState(
            wifi=parse_setting_enabled(wifi),
            bluetooth=parse_setting_enabled(bluetooth),
            mobile_data=parse_setting_enabled(data),
            auto_rotate=parse_setting_enabled(rotate),
            do_not_disturb=parse_setting_enabled(zen),
            is_awake=parse_is_awake(power),
            is_playing=parse_is_playing(media),
            is_muted=parse_is_muted(audio),
            # `cmd audio` (Android 14+) is authoritative; media_session is the fallback.
            volume=parse_audio_volume(audio_volume, audio_maximum) or parse_volume(session_volume),
            brightness=parse_brightness(brightness, brightness_range),
        )

    # --- controls ---------------------------------------------------------
    def set_wifi(self, enabled: bool) -> None:
        self._shell(f"svc wifi {'enable' if enabled else 'disable'}")

    def set_bluetooth(self, enabled: bool) -> None:
        self._shell(f"svc bluetooth {'enable' if enabled else 'disable'}")

    def set_mobile_data(self, enabled: bool) -> None:
        self._shell(f"svc data {'enable' if enabled else 'disable'}")

    def set_auto_rotate(self, enabled: bool) -> None:
        self._shell(f"settings put system accelerometer_rotation {int(enabled)}")

    def set_do_not_disturb(self, enabled: bool) -> None:
        self._shell(f"cmd notification set_dnd {'on' if enabled else 'off'}")

    def set_brightness(self, level: int) -> None:
        """Set the raw `screen_brightness` value (range comes from `toggles()`).

        Switches to manual mode first; with auto-brightness on the value would not stick.
        """
        self._shell(
            "settings put system screen_brightness_mode 0; "
            f"settings put system screen_brightness {max(int(level), 1)}"
        )

    def set_volume(self, level: int) -> None:
        index = max(int(level), 0)
        self._shell(
            f"cmd audio set-volume {MUSIC_STREAM} {index} || "
            f"cmd media_session volume --stream {MUSIC_STREAM} --set {index}"
        )

    def keyevent(self, keycode: int) -> None:
        self._shell(f"input keyevent {int(keycode)}")

    def open_url(self, url: str) -> None:
        self._shell(f"am start -a android.intent.action.VIEW -d {shlex.quote(url)}")

    def reboot(self) -> None:
        self._adb.run(["reboot"], self.serial)

    def enable_wireless(self, ip_address: str) -> str:
        """Switch adbd to TCP mode and connect to it over Wi-Fi."""
        self._adb.run(["tcpip", str(TCPIP_PORT)], self.serial)
        return self._adb.connect(f"{ip_address}:{TCPIP_PORT}")

    def screenshot(self) -> bytes:
        return self._adb.run_bytes(["exec-out", "screencap", "-p"], self.serial)

    def run_command(self, command: str) -> str:
        """Run a user-typed shell command; output includes stderr text."""
        try:
            return self._shell(command, timeout=60)
        except AdbError as exc:
            return str(exc)

    # --- notifications, calls, messages -------------------------------------
    def notifications(self) -> tuple[Notification, ...]:
        return parse_notifications(
            self._shell(
                "dumpsys notification --noredact | "
                "grep -E 'NotificationRecord\\(|android\\.title=|android\\.text=' "
                "|| true"  # grep reports "no match" as a failure; no notifications is not an error
            )
        )

    def call_log(self, limit: int = DEFAULT_ROW_LIMIT) -> tuple[CallEntry, ...]:
        return parse_call_log(
            self._shell(
                "content query --uri content://call_log/calls "
                "--projection number:type:date:duration:name "
                f"--sort 'date DESC LIMIT {int(limit)}'"
            )
        )

    def sms(self, limit: int = DEFAULT_ROW_LIMIT) -> tuple[SmsMessage, ...]:
        return parse_sms(
            self._shell(
                "content query --uri content://sms "
                "--projection address:date:type:read:body "
                f"--sort 'date DESC LIMIT {int(limit)}'"
            )
        )

    def contacts(self) -> dict[str, str]:
        """Phone-number key -> contact name (see `parsers.phone_key`)."""
        return parse_contacts(
            self._shell(
                "content query --uri content://com.android.contacts/data/phones "
                "--projection data1:display_name"
            )
        )

    def dial(self, number: str) -> None:
        """Place a phone call right away."""
        target = validate_phone_number(number).replace("#", "%23")
        self._shell(f"am start -a android.intent.action.CALL -d tel:{target}")

    def answer_call(self) -> None:
        self.keyevent(KEYCODE_CALL)

    def end_call(self) -> None:
        self.keyevent(KEYCODE_ENDCALL)

    def compose_sms(self, number: str, text: str) -> None:
        """Open the phone's SMS app with the message filled in; sending stays manual."""
        target = validate_phone_number(number).replace("#", "%23")
        self._shell(
            f"am start -a android.intent.action.SENDTO -d sms:{target} "
            f"--es sms_body {shlex.quote(text)}"
        )

    # --- apps -------------------------------------------------------------
    def packages(self, include_system: bool = False) -> tuple[str, ...]:
        flag = "" if include_system else " -3"
        return parse_packages(self._shell(f"pm list packages{flag}"))

    def apps(self, include_system: bool = False) -> tuple[AppInfo, ...]:
        """Real app labels via the on-device helper; it also renders missing icons."""
        if not APP_LISTER_ASSET.is_file():
            raise AdbError(f"applister.jar dosyası eksik: {APP_LISTER_ASSET}")
        self._adb.run(["push", str(APP_LISTER_ASSET), REMOTE_APP_LISTER], self.serial, 60)
        scope = "all" if include_system else "user"
        output = self._shell(
            f"CLASSPATH={REMOTE_APP_LISTER} app_process / phonedeck.AppLister "
            f"{REMOTE_ICON_DIR} {ICON_SIZE} {scope}",
            timeout=APP_LIST_TIMEOUT,
        )
        apps = parse_app_list(output)
        if not apps:
            raise AdbError("Uygulama listesi okunamadı.")
        return apps

    def pull_icons(self, local_dir: str) -> None:
        """Copy the rendered `<package>.png` icons into `local_dir`."""
        self._adb.run(["pull", f"{REMOTE_ICON_DIR}/.", local_dir], self.serial, TRANSFER_TIMEOUT)

    def launch_app(self, package: str) -> None:
        validate_package(package)
        self._shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

    def stop_app(self, package: str) -> None:
        self._shell(f"am force-stop {validate_package(package)}")

    def uninstall_app(self, package: str) -> None:
        self._adb.run(["uninstall", validate_package(package)], self.serial, TRANSFER_TIMEOUT)

    def install_apk(self, local_path: str) -> None:
        self._adb.run(["install", "-r", local_path], self.serial, TRANSFER_TIMEOUT)

    # --- files ------------------------------------------------------------
    def list_dir(self, path: str) -> tuple[FileEntry, ...]:
        target = normalize_path(path).rstrip("/") + "/"
        return parse_ls(self._shell(f"ls -la {shlex.quote(target)}"))

    def pull(self, remote_path: str, local_path: str) -> None:
        self._adb.run(["pull", normalize_path(remote_path), local_path], self.serial, TRANSFER_TIMEOUT)

    def push(self, local_path: str, remote_dir: str) -> None:
        self._adb.run(["push", local_path, normalize_path(remote_dir) + "/"], self.serial, TRANSFER_TIMEOUT)

    def make_dir(self, path: str) -> None:
        self._shell(f"mkdir -p {shlex.quote(normalize_path(path))}")

    def delete(self, path: str) -> None:
        target = normalize_path(path)
        if target in _PROTECTED_PATHS:
            raise ValueError(f"Korunan yol silinemez: {target}")
        self._shell(f"rm -rf -- {shlex.quote(target)}")
