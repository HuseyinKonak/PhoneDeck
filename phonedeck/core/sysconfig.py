"""System settings of the phone: connectivity, sound, screen, battery, date, privacy.

Every function takes `(adb, serial)`. Values are validated against enumerations, integer
ranges or a hostname pattern before they reach a shell command, and the shell
commands themselves are built only from constants and those validated values.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Mapping

from .adb import Adb, AdbError
from .device import DeviceService
from .devtools import get_setting_command, parse_float, parse_int, put_setting_command, validate_ranged_int

MIN_BATTERY_LEVEL, MAX_BATTERY_LEVEL = 0, 100
MIN_ROTATION, MAX_ROTATION = 0, 3
MAX_HOSTNAME_LENGTH = 253
TENTHS_PER_DEGREE = 10
ON_WORDS = frozenset({"1", "true", "enabled", "on"})
OFF_WORDS = frozenset({"0", "false", "disabled", "off"})
DNS_OFF, DNS_AUTOMATIC, DNS_HOSTNAME = "off", "opportunistic", "hostname"
DNS_MODES = (DNS_OFF, DNS_AUTOMATIC, DNS_HOSTNAME)
RINGER_MODES = ("NORMAL", "VIBRATE", "SILENT")
REFRESH_KEYS = {"peak": "peak_refresh_rate", "min": "min_refresh_rate"}
NAVIGATION_MODES = {0: "3 düğmeli gezinme", 1: "2 düğmeli gezinme", 2: "Hareketle gezinme"}
BATTERY_HEALTH = {
    1: "Bilinmiyor",
    2: "İyi",
    3: "Aşırı ısınmış",
    4: "Bozuk",
    5: "Aşırı gerilim",
    6: "Belirtilmemiş hata",
    7: "Soğuk",
}
BATTERY_STATUS = {
    1: "Bilinmiyor",
    2: "Şarj oluyor",
    3: "Şarj olmuyor (deşarj)",
    4: "Şarj olmuyor",
    5: "Dolu",
}
BATTERY_PLUGGED_STATUSES = (2, 5)  # charging and full only happen on a charger
BATTERY_ACTIONS = ("level", "status", "unplug", "reset")
_ERROR_MARKERS = ("Error", "Exception", "Permission denied", "Unknown command", "Can't find service")

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")
_VOLUME = re.compile(r"->\s*(-?\d+)")
_RINGER = re.compile(r"=\s*(NORMAL|VIBRATE|SILENT)\b")
_RATE_LIST = re.compile(r"supportedRefreshRates \[([^\]]*)\]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_HOTSPOT_LINK = re.compile(r"^\d+:\s+(?:ap0|swlan0|softap0)\s", re.MULTILINE)
_CYCLES = re.compile(r"^\s*m?cycle[ _]?count[:=]\s*(\d+)", re.MULTILINE | re.IGNORECASE)


# --- data -------------------------------------------------------------------
@dataclass(frozen=True)
class Toggle:
    """An on/off switch backed by one read command and two write commands.

    `default` is what an unset value (`null`) means; `None` means the phone does not
    support the switch when the read gives nothing usable. `inverted` flips the
    displayed state (for example "rotation lock" over `accelerometer_rotation`).
    """

    key: str
    label: str
    read: str
    on_command: str
    off_command: str
    default: bool | None = None
    inverted: bool = False
    on_word: str = "1"
    off_word: str = "0"
    tooltip: str = ""


@dataclass(frozen=True)
class AudioStream:
    key: str
    label: str
    stream_id: int


@dataclass(frozen=True)
class StreamVolume:
    key: str
    minimum: int
    maximum: int
    current: int


@dataclass(frozen=True)
class ConnectivityState:
    toggles: Mapping[str, bool | None]
    dns_mode: str = DNS_AUTOMATIC
    dns_host: str = ""
    bluetooth_name: str = ""
    usb_tethering: bool = False
    hotspot: bool = False


@dataclass(frozen=True)
class SoundState:
    toggles: Mapping[str, bool | None]
    streams: tuple[StreamVolume, ...] = ()
    ringer: str = ""


@dataclass(frozen=True)
class ScreenState:
    toggles: Mapping[str, bool | None]
    rotation: int | None = None
    rates: tuple[int, ...] = ()
    peak_rate: int | None = None
    min_rate: int | None = None
    navigation: str = ""


@dataclass(frozen=True)
class BatteryState:
    toggles: Mapping[str, bool | None]
    health: str = ""
    status: str = ""
    technology: str = ""
    level: int | None = None
    voltage_mv: int | None = None
    temperature_c: float | None = None
    cycles: int | None = None
    is_simulated: bool = False


@dataclass(frozen=True)
class DateState:
    toggles: Mapping[str, bool | None]
    timezone: str = ""
    locale: str = ""


@dataclass(frozen=True)
class PrivacyState:
    toggles: Mapping[str, bool | None]
    usb_debugging: bool | None = None
    wireless_debugging: bool | None = None
    developer_options: bool | None = None
    adb_secure: bool | None = None


# --- switch definitions -----------------------------------------------------
def setting_toggle(
    namespace: str,
    key: str,
    label: str,
    default: bool | None,
    *,
    on: str = "1",
    off: str = "0",
    inverted: bool = False,
    tooltip: str = "",
) -> Toggle:
    """A switch stored as a plain `settings` value."""
    shown_on, shown_off = (off, on) if inverted else (on, off)
    return Toggle(
        key,
        label,
        get_setting_command(namespace, key),
        put_setting_command(namespace, key, shown_on),
        put_setting_command(namespace, key, shown_off),
        default,
        inverted,
        on,
        off,
        tooltip,
    )


def command_toggle(key: str, label: str, read: str, on_command: str, off_command: str) -> Toggle:
    """A switch driven by a service command; unreadable means unsupported."""
    return Toggle(key, label, read, on_command, off_command, None, on_word="enabled", off_word="disabled")


CONNECTIVITY_TOGGLES = (
    command_toggle(
        "airplane",
        "Uçak modu",
        "cmd connectivity airplane-mode",
        "cmd connectivity airplane-mode enable",
        "cmd connectivity airplane-mode disable",
    ),
    command_toggle(
        "location",
        "Konum",
        "cmd location is-location-enabled",
        "cmd location set-location-enabled true",
        "cmd location set-location-enabled false",
    ),
    command_toggle(
        "nfc", "NFC", "dumpsys nfc | grep -m1 mState=", "svc nfc enable", "svc nfc disable"
    ),
    setting_toggle("global", "data_roaming", "Mobil veri dolaşımı", False),
)
SOUND_TOGGLES = (
    setting_toggle("system", "sound_effects_enabled", "Dokunma sesleri", True),
    setting_toggle("system", "haptic_feedback_enabled", "Dokununca titreşim", True),
    setting_toggle("system", "dtmf_tone", "Tuş takımı sesleri", True),
    setting_toggle("secure", "charging_sounds_enabled", "Şarj sesleri", True),
)
SCREEN_TOGGLES = (
    setting_toggle("system", "screen_brightness_mode", "Otomatik parlaklık", False),
    setting_toggle(
        "system",
        "accelerometer_rotation",
        "Döndürme kilidi",
        False,
        inverted=True,
        tooltip="Açıkken ekran, aşağıdaki yönde sabit kalır.",
    ),
    setting_toggle("secure", "doze_always_on", "Her zaman açık ekran", None),
    setting_toggle("secure", "wake_gesture_enabled", "Kaldırınca uyandır", None),
)
BATTERY_TOGGLES = (
    command_toggle(
        "low_power",
        "Pil tasarrufu",
        get_setting_command("global", "low_power"),
        "cmd power set-mode 1",
        "cmd power set-mode 0",
    ),
    setting_toggle("global", "adaptive_battery_management_enabled", "Uyarlanabilir pil", None),
)
DATE_TOGGLES = (
    setting_toggle("global", "auto_time", "Otomatik saat", None),
    setting_toggle("global", "auto_time_zone", "Otomatik saat dilimi", None),
    setting_toggle("system", "time_12_24", "24 saat biçimi", False, on="24", off="12"),
)
PRIVACY_TOGGLES = (
    setting_toggle(
        "global",
        "verifier_verify_adb_installs",
        "ADB ile kurulan uygulamaları doğrula",
        None,
    ),
    setting_toggle("global", "package_verifier_enable", "Uygulama kurulum doğrulaması", True),
    setting_toggle("system", "show_password", "Parolayı yazarken göster", True),
    setting_toggle("secure", "lock_screen_show_notifications", "Kilit ekranında bildirimler", True),
    setting_toggle(
        "secure",
        "lock_screen_allow_private_notifications",
        "Kilit ekranında bildirim içeriği",
        True,
    ),
)
ALL_TOGGLES = (
    CONNECTIVITY_TOGGLES + SOUND_TOGGLES + SCREEN_TOGGLES + BATTERY_TOGGLES + DATE_TOGGLES + PRIVACY_TOGGLES
)
AUDIO_STREAMS = (
    AudioStream("music", "Medya", 3),
    AudioStream("ring", "Zil", 2),
    AudioStream("notification", "Bildirim", 5),
    AudioStream("alarm", "Alarm", 4),
    AudioStream("call", "Arama", 0),
)


# --- validators -------------------------------------------------------------
def validate_hostname(text: str) -> str:
    host = text.strip()
    is_valid = (
        len(host) <= MAX_HOSTNAME_LENGTH
        and _HOSTNAME.fullmatch(host) is not None
        and not host.rsplit(".", 1)[-1].isdigit()  # a bare IP address is not a hostname
    )
    if not is_valid:
        raise ValueError("Ana makine adı geçersiz. Örnek: dns.example.com")
    return host


def validate_dns_mode(mode: str) -> str:
    if mode not in DNS_MODES:
        raise ValueError(f"Geçersiz özel DNS kipi: {mode!r}")
    return mode


def validate_ringer(mode: str) -> str:
    if mode not in RINGER_MODES:
        raise ValueError(f"Geçersiz zil kipi: {mode!r}")
    return mode


def validate_stream(key: str) -> AudioStream:
    stream = next((item for item in AUDIO_STREAMS if item.key == key), None)
    if stream is None:
        raise ValueError(f"Bilinmeyen ses akışı: {key!r}")
    return stream


def validate_toggle(key: str) -> Toggle:
    toggle = next((item for item in ALL_TOGGLES if item.key == key), None)
    if toggle is None:
        raise ValueError(f"Bilinmeyen ayar: {key!r}")
    return toggle


def validate_volume(value: str | int, minimum: int, maximum: int) -> int:
    return validate_ranged_int(value, minimum, maximum, "Ses düzeyi")


def validate_rotation(value: str | int) -> int:
    return validate_ranged_int(value, MIN_ROTATION, MAX_ROTATION, "Ekran yönü")


def validate_refresh(limit: str, rate: str | int, supported: tuple[int, ...]) -> tuple[str, int]:
    """Check a refresh-rate limit; the minimum may also be 0 (no limit)."""
    if limit not in REFRESH_KEYS:
        raise ValueError(f"Bilinmeyen yenileme hızı sınırı: {limit!r}")
    allowed = supported + ((0,) if limit == "min" else ())
    low, high = (min(allowed), max(allowed)) if allowed else (0, 0)
    value = validate_ranged_int(rate, low, high, "Yenileme hızı")
    if value not in allowed:
        raise ValueError("Bu yenileme hızı ekran tarafından desteklenmiyor.")
    return REFRESH_KEYS[limit], value


# --- parsers ----------------------------------------------------------------
def parse_on(output: str, default: bool | None, on_word: str = "1", off_word: str = "0") -> bool | None:
    """Read a switch from command output; unset or unreadable output gives `default`."""
    lines = output.strip().splitlines()
    text = lines[0].rsplit("=", 1)[-1].strip().lower() if lines else ""
    if text in ON_WORDS or text == on_word:
        return True
    if text in OFF_WORDS or text == off_word:
        return False
    return default


def parse_toggle(toggle: Toggle, output: str) -> bool | None:
    state = parse_on(output, toggle.default, toggle.on_word, toggle.off_word)
    return None if state is None else state != toggle.inverted


def parse_toggles(toggles: tuple[Toggle, ...], outputs: list[str]) -> dict[str, bool | None]:
    return {toggle.key: parse_toggle(toggle, text) for toggle, text in zip(toggles, outputs)}


def parse_stream(key: str, output: str) -> StreamVolume | None:
    """Parse `get-stream-volume`, `get-min-volume`, `get-max-volume` lines, in that order."""
    numbers = [int(number) for number in _VOLUME.findall(output)]
    if len(numbers) != 3 or numbers[1] > numbers[2]:
        return None
    current, minimum, maximum = numbers
    return StreamVolume(key, minimum, maximum, min(max(current, minimum), maximum))


def parse_ringer(output: str) -> str:
    match = _RINGER.search(output)
    return match.group(1) if match else ""


def parse_refresh_rates(output: str) -> tuple[int, ...]:
    match = _RATE_LIST.search(output)
    if not match:
        return ()
    rates = {round(float(number)) for number in _NUMBER.findall(match.group(1))}
    return tuple(sorted(rate for rate in rates if rate > 0))


def parse_rate(output: str) -> int | None:
    value = parse_float(output)
    return round(value) if value is not None and math.isfinite(value) else None


def parse_hotspot(output: str) -> bool:
    return _HOTSPOT_LINK.search(output) is not None


def parse_usb_tethering(output: str) -> bool:
    return any(function in output.lower() for function in ("rndis", "ncm"))


def first_value(*outputs: str) -> str:
    """The first output that holds a real value (not empty and not `null`)."""
    return next((text.strip() for text in outputs if text.strip() not in ("", "null")), "")


def _battery_field(output: str, name: str) -> str:
    match = re.search(rf"^\s*{name}:[ \t]*(\S.*?)\s*$", output, re.MULTILINE)
    return match.group(1) if match else ""


def parse_battery(toggles: Mapping[str, bool | None], output: str) -> BatteryState:
    health, status = parse_int(_battery_field(output, "health")), parse_int(_battery_field(output, "status"))
    temperature = parse_int(_battery_field(output, "temperature"))
    cycles = _CYCLES.search(output)
    return BatteryState(
        toggles=toggles,
        health=BATTERY_HEALTH.get(health, "") if health is not None else "",
        status=BATTERY_STATUS.get(status, "") if status is not None else "",
        technology=_battery_field(output, "technology"),
        level=parse_int(_battery_field(output, "level")),
        voltage_mv=parse_int(_battery_field(output, "voltage")),
        temperature_c=None if temperature is None else temperature / TENTHS_PER_DEGREE,
        cycles=int(cycles.group(1)) if cycles else None,
        is_simulated="UPDATES STOPPED" in output or "UpdatesStopped: true" in output,
    )


# --- command builders -------------------------------------------------------
def volume_command(stream_key: str, value: str | int, minimum: int, maximum: int) -> str:
    stream = validate_stream(stream_key)
    return f"cmd audio set-volume {stream.stream_id} {validate_volume(value, minimum, maximum)}"


def dns_command(mode: str, hostname: str = "") -> str:
    """Private DNS: the hostname is stored first so the mode never points at nothing."""
    validate_dns_mode(mode)
    if mode != DNS_HOSTNAME:
        return put_setting_command("global", "private_dns_mode", mode)
    host = validate_hostname(hostname)
    return "; ".join(
        (
            put_setting_command("global", "private_dns_specifier", host),
            put_setting_command("global", "private_dns_mode", DNS_HOSTNAME),
        )
    )


def battery_command(action: str, value: str | int | None = None) -> str:
    """`dumpsys battery` simulation commands."""
    if action == "level":
        level = validate_ranged_int(value, MIN_BATTERY_LEVEL, MAX_BATTERY_LEVEL, "Pil seviyesi")
        return f"dumpsys battery set level {level}"
    if action == "status":
        status = validate_ranged_int(value, min(BATTERY_STATUS), max(BATTERY_STATUS), "Pil durumu")
        # The phone draws its charging icon from the plug state, not the status, so the
        # two are set together; otherwise "not charging" still shows the charging bolt.
        plug = "set ac 1" if status in BATTERY_PLUGGED_STATUSES else "unplug"
        return f"dumpsys battery {plug}; dumpsys battery set status {status}"
    if action in ("unplug", "reset"):
        return f"dumpsys battery {action}"
    raise ValueError(f"Geçersiz pil işlemi: {action!r}")


# --- device access ----------------------------------------------------------
def _sections(adb: Adb, serial: str, commands: tuple[str, ...]) -> list[str]:
    # One parallel batch per tab: each Android command costs ~0.2 s.
    return DeviceService(adb, serial)._run_sections(commands)


def _reads(toggles: tuple[Toggle, ...]) -> tuple[str, ...]:
    return tuple(toggle.read for toggle in toggles)


def _shell_checked(adb: Adb, serial: str, command: str) -> str:
    """Run a write command; the shell tools report failures as text, not exit codes."""
    output = adb.shell(serial, command).strip()
    if output.startswith(_ERROR_MARKERS):
        raise AdbError(output.splitlines()[0])
    return output


def set_toggle(adb: Adb, serial: str, key: str, enabled: bool) -> None:
    toggle = validate_toggle(key)
    _shell_checked(adb, serial, toggle.on_command if enabled else toggle.off_command)


def read_connectivity(adb: Adb, serial: str) -> ConnectivityState:
    outputs = _sections(
        adb,
        serial,
        _reads(CONNECTIVITY_TOGGLES)
        + (
            get_setting_command("global", "private_dns_mode"),
            get_setting_command("global", "private_dns_specifier"),
            get_setting_command("secure", "bluetooth_name"),
            "getprop sys.usb.state",
            "ip -o -4 addr show",
        ),
    )
    count = len(CONNECTIVITY_TOGGLES)
    mode, host, bluetooth, usb, links = outputs[count:]
    return ConnectivityState(
        toggles=parse_toggles(CONNECTIVITY_TOGGLES, outputs[:count]),
        dns_mode=first_value(mode) or DNS_AUTOMATIC,
        dns_host=first_value(host),
        bluetooth_name=first_value(bluetooth),
        usb_tethering=parse_usb_tethering(usb),
        hotspot=parse_hotspot(links),
    )


def set_private_dns(adb: Adb, serial: str, mode: str, hostname: str = "") -> None:
    _shell_checked(adb, serial, dns_command(mode, hostname))


def read_sound(adb: Adb, serial: str) -> SoundState:
    stream_commands = tuple(
        "; ".join(f"cmd audio {name} {stream.stream_id}" for name in ("get-stream-volume", "get-min-volume", "get-max-volume"))
        for stream in AUDIO_STREAMS
    )
    outputs = _sections(
        adb, serial, _reads(SOUND_TOGGLES) + stream_commands + ("dumpsys audio | grep -m1 'mode (external)'",)
    )
    count = len(SOUND_TOGGLES)
    streams = (parse_stream(stream.key, text) for stream, text in zip(AUDIO_STREAMS, outputs[count:]))
    return SoundState(
        toggles=parse_toggles(SOUND_TOGGLES, outputs[:count]),
        streams=tuple(stream for stream in streams if stream is not None),
        ringer=parse_ringer(outputs[-1]),
    )


def set_stream_volume(adb: Adb, serial: str, stream_key: str, value: int, minimum: int, maximum: int) -> None:
    _shell_checked(adb, serial, volume_command(stream_key, value, minimum, maximum))


def set_ringer_mode(adb: Adb, serial: str, mode: str) -> None:
    _shell_checked(adb, serial, f"cmd audio set-ringer-mode {validate_ringer(mode)}")


def read_screen(adb: Adb, serial: str) -> ScreenState:
    outputs = _sections(
        adb,
        serial,
        _reads(SCREEN_TOGGLES)
        + (
            get_setting_command("system", "user_rotation"),
            "dumpsys display | grep -m1 -o 'supportedRefreshRates \\[[^]]*\\]'",
            get_setting_command("system", "peak_refresh_rate"),
            get_setting_command("system", "min_refresh_rate"),
            get_setting_command("secure", "navigation_mode"),
        ),
    )
    count = len(SCREEN_TOGGLES)
    rotation, rates, peak, minimum, navigation = outputs[count:]
    return ScreenState(
        toggles=parse_toggles(SCREEN_TOGGLES, outputs[:count]),
        rotation=parse_int(rotation),
        rates=parse_refresh_rates(rates),
        peak_rate=parse_rate(peak),
        min_rate=parse_rate(minimum),
        navigation=NAVIGATION_MODES.get(parse_int(navigation), ""),
    )


def set_rotation(adb: Adb, serial: str, rotation: str | int) -> None:
    value = validate_rotation(rotation)
    _shell_checked(adb, serial, put_setting_command("system", "user_rotation", value))


def set_refresh_limit(adb: Adb, serial: str, limit: str, rate: str | int, supported: tuple[int, ...]) -> None:
    key, value = validate_refresh(limit, rate, supported)
    _shell_checked(adb, serial, put_setting_command("system", key, value))


def read_battery(adb: Adb, serial: str) -> BatteryState:
    outputs = _sections(adb, serial, _reads(BATTERY_TOGGLES) + ("dumpsys battery",))
    toggles = parse_toggles(BATTERY_TOGGLES, outputs[:-1])
    return parse_battery(toggles, outputs[-1])


def simulate_battery(adb: Adb, serial: str, action: str, value: str | int | None = None) -> None:
    _shell_checked(adb, serial, battery_command(action, value))


def read_date(adb: Adb, serial: str) -> DateState:
    outputs = _sections(
        adb,
        serial,
        _reads(DATE_TOGGLES)
        + (
            "getprop persist.sys.timezone",
            "getprop persist.sys.locale",
            get_setting_command("system", "system_locales"),
            "getprop ro.product.locale",
        ),
    )
    count = len(DATE_TOGGLES)
    return DateState(
        toggles=parse_toggles(DATE_TOGGLES, outputs[:count]),
        timezone=first_value(outputs[count]),
        locale=first_value(*outputs[count + 1 :]),
    )


def read_privacy(adb: Adb, serial: str) -> PrivacyState:
    outputs = _sections(
        adb,
        serial,
        _reads(PRIVACY_TOGGLES)
        + (
            get_setting_command("global", "adb_enabled"),
            get_setting_command("global", "adb_wifi_enabled"),
            get_setting_command("global", "development_settings_enabled"),
            "getprop ro.adb.secure",
        ),
    )
    count = len(PRIVACY_TOGGLES)
    usb, wireless, developer, secure = (parse_on(text, None) for text in outputs[count:])
    return PrivacyState(
        toggles=parse_toggles(PRIVACY_TOGGLES, outputs[:count]),
        usb_debugging=usb,
        wireless_debugging=wireless,
        developer_options=developer,
        adb_secure=secure,
    )
