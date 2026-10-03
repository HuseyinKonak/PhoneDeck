"""Find-my-phone tools: ring, vibrate, blink the flash and screen, locate, check and secure the phone.

Every function takes `(adb, serial)`. Anything the user types is validated and passed
through `shlex.quote` before it reaches a shell. Ringing changes the ringer mode and
volumes, so the previous values are captured in a `DeviceSnapshot` and put back by
`restore_device`, which also arms a watchdog on the phone itself in case the PC goes away.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .adb import Adb, AdbError
from .device import (
    APP_LISTER_ASSET,
    REMOTE_APP_LISTER,
    REMOTE_STATE_DIR,
    DeviceService,
    validate_phone_number,
)
from .parsers import parse_battery, parse_is_awake

RINGER_SILENT, RINGER_VIBRATE, RINGER_NORMAL = 0, 1, 2
RINGER_NAMES = {RINGER_SILENT: "SILENT", RINGER_VIBRATE: "VIBRATE", RINGER_NORMAL: "NORMAL"}
STREAM_RING, STREAM_MUSIC, STREAM_ALARM, STREAM_NOTIFICATION = 2, 3, 4, 5
RING_STREAMS = (STREAM_RING, STREAM_MUSIC, STREAM_ALARM, STREAM_NOTIFICATION)
RING_DURATIONS_S = (10, 30, 60)
WATCHDOG_GRACE_S = 5
REPLAY_INTERVAL_S = 20
DEFAULT_BRIGHTNESS_MAX = 255
KEYCODE_SLEEP = 223
KEYCODE_WAKEUP = 224
VIBRATE_TOTAL_MS = 30_000
VIBRATE_ON_MS, VIBRATE_OFF_MS = 800, 400
MS_PER_SECOND = 1_000
TORCH_ON_MS, TORCH_OFF_MS = 300, 300
TORCH_MAX_S = 120
TORCH_START_WAIT_S = 2
TORCH_PUSH_TIMEOUT_S = 60
TORCH_LOG = f"{REMOTE_STATE_DIR}/torch.log"
BLINK_DIM_BRIGHTNESS = 1
OWNER_MESSAGE_MAX_CHARS = 120
LOCATION_STALE_S = 24 * 60 * 60
MAPS_URL = "https://www.google.com/maps/search/?api=1&query={latitude:.6f},{longitude:.6f}"
MEDIA_URI = "content://media/internal/audio/media"
WATCHDOG_PID_FILE = f"{REMOTE_STATE_DIR}/finder.pid"
WATCHDOG_MARKER = "phonedeck-finder"
# Preview players that start playing a content URI by themselves (no extra tap).
PLAYER_COMPONENTS = (
    "com.google.android.apps.youtube.music/.audiopreview.AudioPreviewPlayerActivity",
    "com.miui.player/.phone.ui.AudioPreview",
)
RING_SOURCE_FILTERS = ("is_alarm=1", "is_ringtone=1")
TRANSPORT_USB, TRANSPORT_WIFI = "USB", "Wi-Fi"

_ERROR_MARKERS = ("Error", "Exception", "Permission denied", "SecurityException", "Unknown command")
_MEDIA_ID = re.compile(r"_id=(\d+)")
_LOCATION = re.compile(
    r"Location\[(?P<provider>[A-Za-z_]+) (?P<lat>-?\d+(?:\.\d+)?)\**,(?P<lon>-?\d+(?:\.\d+)?)\**"
    r"(?P<rest>[^\]]*)"
)
_ACCURACY = re.compile(r"(?:hAcc|acc)=(\d+(?:\.\d+)?)")
_ELAPSED = re.compile(r"et=\+?((?:\d+d)?(?:\d+h)?(?:\d+m)?(?:\d+s)?(?:\d+ms)?)")
_DURATION_PART = re.compile(r"(\d+)(ms|d|h|m|s)")
_DURATION_SECONDS = {"d": 86_400.0, "h": 3_600.0, "m": 60.0, "s": 1.0, "ms": 0.001}
_STREAM_VALUE = re.compile(r"->\s*(-?\d+)")
_SSID = re.compile(r'connected to "([^"]*)"')
_PLAYER_PACKAGES = tuple(component.split("/")[0] for component in PLAYER_COMPONENTS)
_MEDIA_URI = re.compile(rf"{re.escape(MEDIA_URI)}/\d+")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


# --- data -------------------------------------------------------------------
@dataclass(frozen=True)
class StreamLevel:
    stream: int
    current: int
    maximum: int


@dataclass(frozen=True)
class DeviceSnapshot:
    """Sound and display settings captured before an effect changes them."""

    ringer_mode: int
    streams: tuple[StreamLevel, ...]
    brightness: int | None = None
    brightness_mode: int | None = None
    brightness_max: int = DEFAULT_BRIGHTNESS_MAX


@dataclass(frozen=True)
class RingSession:
    """A ring that is playing: which player and sound file it uses."""

    uri: str
    component: str


@dataclass(frozen=True)
class LocationFix:
    provider: str
    latitude: float
    longitude: float
    accuracy_m: float | None = None
    age_s: int | None = None

    @property
    def coordinates_text(self) -> str:
        return f"{self.latitude:.6f}, {self.longitude:.6f}"

    @property
    def maps_url(self) -> str:
        return MAPS_URL.format(latitude=self.latitude, longitude=self.longitude)


@dataclass(frozen=True)
class OwnerInfo:
    message: str = ""
    is_enabled: bool = False


@dataclass(frozen=True)
class PhoneStatus:
    battery_level: int = 0
    is_charging: bool = False
    ssid: str = ""
    is_screen_on: bool = True
    is_locked: bool = False
    transport: str = TRANSPORT_USB


# --- validators -------------------------------------------------------------
def validate_duration(seconds: int) -> int:
    if seconds not in RING_DURATIONS_S:
        raise ValueError(f"Geçersiz süre: {seconds} sn")
    return seconds


def validate_owner_message(message: str, phone: str) -> str:
    """Combine the free text and phone number shown on the lock screen."""
    text = _CONTROL_CHARS.sub(" ", message).strip()
    number = validate_phone_number(phone) if phone.strip() else ""
    combined = f"{text} {number}".strip()
    if not combined:
        raise ValueError("Mesaj ya da telefon numarası yaz.")
    if len(combined) > OWNER_MESSAGE_MAX_CHARS:
        raise ValueError(f"Mesaj en fazla {OWNER_MESSAGE_MAX_CHARS} karakter olabilir.")
    return combined


def validate_media_uri(uri: str) -> str:
    if not _MEDIA_URI.fullmatch(uri):
        raise ValueError(f"Geçersiz ses kaynağı: {uri}")
    return uri


# --- parsers ----------------------------------------------------------------
def parse_stream_value(output: str) -> int | None:
    """Parse `cmd audio get-stream-volume` / `get-max-volume` output."""
    match = _STREAM_VALUE.search(output)
    return int(match.group(1)) if match else None


def parse_setting_int(output: str) -> int | None:
    text = output.strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else None


def parse_brightness_max(output: str) -> int:
    match = re.search(r"mScreenBrightnessNormalMaximum=(\d+(?:\.\d+)?)", output)
    return round(float(match.group(1))) if match else DEFAULT_BRIGHTNESS_MAX


def parse_snapshot(sections: list[str]) -> DeviceSnapshot:
    """Build a snapshot from the outputs of `snapshot_commands()`, in order."""
    stream_count = len(RING_STREAMS)
    levels = []
    for index, stream in enumerate(RING_STREAMS):
        current = parse_stream_value(sections[index * 2])
        maximum = parse_stream_value(sections[index * 2 + 1])
        if current is None or maximum is None or maximum <= 0:
            raise AdbError("Telefonun ses düzeyleri okunamadı.")
        levels.append(StreamLevel(stream, current, maximum))
    ringer, brightness, mode, display, _power = sections[stream_count * 2:]
    ringer_mode = parse_setting_int(ringer)
    if ringer_mode not in RINGER_NAMES:
        raise AdbError("Telefonun zil modu okunamadı.")
    return DeviceSnapshot(
        ringer_mode=ringer_mode,
        streams=tuple(levels),
        brightness=parse_setting_int(brightness),
        brightness_mode=parse_setting_int(mode),
        brightness_max=parse_brightness_max(display),
    )


def parse_duration_seconds(text: str) -> float:
    """Parse Android's `+1d2h3m4s5ms` duration notation."""
    return sum(
        int(amount) * _DURATION_SECONDS[unit] for amount, unit in _DURATION_PART.findall(text)
    )


def parse_uptime(output: str) -> float | None:
    """First field of /proc/uptime: seconds since boot, the clock `et=` is measured on."""
    parts = output.split()
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return None


def parse_location(dump: str, uptime_s: float | None) -> LocationFix | None:
    """Pick the freshest `Location[...]` entry from `dumpsys location`.

    Formats differ by Android version (`hAcc=` / `acc=`, optional `et=`), and the shell
    sees coordinates with trailing `*` characters; everything is optional except
    provider and coordinates.
    """
    fixes = [fix for line in dump.splitlines() if (fix := _parse_location_line(line, uptime_s))]
    if not fixes:
        return None
    return min(fixes, key=lambda fix: (fix.age_s is None, fix.age_s or 0, fix.accuracy_m or 0.0))


def _parse_location_line(line: str, uptime_s: float | None) -> LocationFix | None:
    match = _LOCATION.search(line)
    if not match:
        return None
    latitude, longitude = float(match["lat"]), float(match["lon"])
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    rest = match["rest"]
    accuracy = _ACCURACY.search(rest)
    elapsed = _ELAPSED.search(rest)
    age = None
    if elapsed and uptime_s is not None:
        age = max(int(uptime_s - parse_duration_seconds(elapsed.group(1))), 0)
    return LocationFix(
        provider=match["provider"],
        latitude=latitude,
        longitude=longitude,
        accuracy_m=float(accuracy.group(1)) if accuracy else None,
        age_s=age,
    )


def parse_ssid(output: str) -> str:
    match = _SSID.search(output)
    ssid = match.group(1) if match else ""
    return "" if ssid.startswith("<unknown") else ssid


def parse_is_locked(output: str) -> bool:
    return "isKeyguardShowing=true" in output


def transport_of(serial: str) -> str:
    """adb names Wi-Fi devices `host:port` or `...._adb-tls-connect._tcp`."""
    return TRANSPORT_WIFI if ":" in serial or "_adb-tls" in serial else TRANSPORT_USB


def parse_owner_info(message_output: str, enabled_output: str) -> OwnerInfo:
    text = message_output.strip()
    return OwnerInfo(
        message="" if text == "null" else text,
        is_enabled=enabled_output.strip() == "1",
    )


# --- command builders -------------------------------------------------------
def snapshot_commands() -> tuple[str, ...]:
    commands: list[str] = []
    for stream in RING_STREAMS:
        commands += [f"cmd audio get-stream-volume {stream}", f"cmd audio get-max-volume {stream}"]
    commands += [
        "settings get global mode_ringer",
        "settings get system screen_brightness",
        "settings get system screen_brightness_mode",
        "dumpsys display | grep mScreenBrightnessNormalMaximum",
        "dumpsys power | grep mWakefulness=",
    ]
    return tuple(commands)


def build_loud_script(snapshot: DeviceSnapshot) -> str:
    """Normal ringer mode first, then every ring-related stream to its maximum."""
    parts = [f"cmd audio set-ringer-mode {RINGER_NAMES[RINGER_NORMAL]}"]
    parts += [f"cmd audio set-volume {s.stream} {s.maximum}" for s in snapshot.streams]
    return "; ".join(parts)


def build_restore_script(snapshot: DeviceSnapshot) -> str:
    """Put volumes, brightness and (last, so volumes cannot flip it) the ringer mode back."""
    parts = [f"cmd audio set-volume {s.stream} {s.current}" for s in snapshot.streams]
    if snapshot.brightness_mode is not None:
        parts.append(f"settings put system screen_brightness_mode {snapshot.brightness_mode}")
    if snapshot.brightness is not None:
        parts.append(f"settings put system screen_brightness {snapshot.brightness}")
    parts.append(f"cmd audio set-ringer-mode {RINGER_NAMES[snapshot.ringer_mode]}")
    return "; ".join(parts)


def build_flash_script(snapshot: DeviceSnapshot) -> str:
    return (
        "settings put system screen_brightness_mode 0; "
        f"settings put system screen_brightness {snapshot.brightness_max}"
    )


def build_playback_command(component: str, uri: str) -> str:
    return (
        f"am start -n {shlex.quote(component)} -a android.intent.action.VIEW "
        f"-t 'audio/*' -d {shlex.quote(validate_media_uri(uri))}"
    )


def build_stop_playback_script() -> str:
    return "; ".join(f"am force-stop {package}" for package in _PLAYER_PACKAGES)


def build_vibrate_command(total_ms: int = VIBRATE_TOTAL_MS) -> str:
    """Finite on/off waveform; the leading 0 is the initial pause. `-f` ignores Do Not Disturb."""
    cycle_ms = VIBRATE_ON_MS + VIBRATE_OFF_MS
    timings = ["0"] + [f"{VIBRATE_ON_MS} {VIBRATE_OFF_MS}"] * (int(total_ms) // cycle_ms)
    return f"cmd vibrator_manager synced -f -B waveform {' '.join(timings)}"


def validate_torch_seconds(seconds: int) -> int:
    if not 1 <= int(seconds) <= TORCH_MAX_S:
        raise ValueError(f"Flaş süresi 1–{TORCH_MAX_S} sn arasında olmalı.")
    return int(seconds)


def build_torch_command(seconds: int) -> str:
    """Start the flash blinker detached and report its first lines.

    The helper keeps running on the phone and switches the light off by itself when
    the time is up, so the flash never stays on if adb or the PC goes away.
    """
    helper = (
        f"CLASSPATH={REMOTE_APP_LISTER} app_process / phonedeck.PhoneTool torch "
        f"{validate_torch_seconds(seconds)} {TORCH_ON_MS} {TORCH_OFF_MS}"
    )
    return (
        f"mkdir -p {REMOTE_STATE_DIR}; "
        f"nohup setsid sh -c {shlex.quote(helper)} > {TORCH_LOG} 2>&1 & "
        f"sleep {TORCH_START_WAIT_S}; cat {TORCH_LOG}"
    )


def build_stop_torch_command() -> str:
    """Kill the blinker; the camera service turns the light off when its owner dies.

    The `[c]` keeps the pattern from matching the shell that runs this very command;
    `true` because pkill reports "nothing to kill" as a failure.
    """
    return "pkill -f 'PhoneTool tor[c]h'; true"


def build_blink_command(snapshot: DeviceSnapshot, is_bright: bool) -> str:
    """One step of screen blinking that keeps the screen on, so playback is not interrupted."""
    level = snapshot.brightness_max if is_bright else BLINK_DIM_BRIGHTNESS
    return f"settings put system screen_brightness {level}; input keyevent {KEYCODE_WAKEUP}"


def build_watchdog_command(restore_script: str, delay_s: int) -> str:
    """Detached restore on the phone that fires even when adb or the PC is gone."""
    body = f": {WATCHDOG_MARKER}; sleep {int(delay_s)}; {restore_script}"
    return (
        f"mkdir -p {REMOTE_STATE_DIR}; "
        f"nohup setsid sh -c {shlex.quote(body)} >/dev/null 2>&1 & "
        f"echo $! > {WATCHDOG_PID_FILE}"
    )


def build_cancel_watchdog_command() -> str:
    pid = f"$(cat {WATCHDOG_PID_FILE} 2>/dev/null)"
    return (
        f"if tr '\\0' ' ' < /proc/{pid}/cmdline 2>/dev/null | grep -q {WATCHDOG_MARKER}; "
        f"then kill {pid}; fi; rm -f {WATCHDOG_PID_FILE}"
    )


def build_owner_info_script(message: str) -> str:
    return (
        f"settings put secure lock_screen_owner_info {shlex.quote(message)}; "
        "settings put secure lock_screen_owner_info_enabled 1"
    )


# --- device access ----------------------------------------------------------
def _sections(adb: Adb, serial: str, commands: tuple[str, ...]) -> list[str]:
    return DeviceService(adb, serial)._run_sections(commands)


def _shell_checked(adb: Adb, serial: str, command: str) -> str:
    """Run a command whose failures come back as text rather than an exit code."""
    output = adb.shell(serial, command).strip()
    if output.startswith(_ERROR_MARKERS):
        raise AdbError(output.splitlines()[0])
    return output


def read_snapshot(adb: Adb, serial: str) -> DeviceSnapshot:
    return parse_snapshot(_sections(adb, serial, snapshot_commands()))


def find_ring_source(adb: Adb, serial: str) -> str:
    """Content URI of the phone's own alarm tone (or ringtone as a fallback)."""
    for condition in RING_SOURCE_FILTERS:
        output = adb.shell(
            serial,
            f"content query --uri {MEDIA_URI} --projection _id --where {shlex.quote(condition)}",
        )
        match = _MEDIA_ID.search(output)
        if match:
            return f"{MEDIA_URI}/{match.group(1)}"
    raise AdbError("Telefonda çalınacak bir zil sesi bulunamadı.")


def play_sound(adb: Adb, serial: str, uri: str) -> RingSession:
    """Start the sound in the first preview player the phone has."""
    for component in PLAYER_COMPONENTS:
        output = adb.shell(serial, build_playback_command(component, uri))
        if not any(marker in output for marker in _ERROR_MARKERS):
            return RingSession(uri=uri, component=component)
    raise AdbError("Telefonda sesi çalabilecek bir müzik uygulaması bulunamadı.")


def replay_sound(adb: Adb, serial: str, session: RingSession) -> None:
    """Restart the tone; the page calls this every `REPLAY_INTERVAL_S` while ringing."""
    _shell_checked(adb, serial, build_playback_command(session.component, session.uri))


def start_ring(adb: Adb, serial: str, snapshot: DeviceSnapshot, seconds: int) -> RingSession:
    """Make the phone loud and play the tone; undoes everything itself on failure."""
    validate_duration(seconds)
    uri = find_ring_source(adb, serial)
    try:
        adb.shell(serial, build_loud_script(snapshot))
        session = play_sound(adb, serial, uri)
        arm_watchdog(adb, serial, snapshot, seconds + WATCHDOG_GRACE_S)
    except AdbError:
        restore_device(adb, serial, snapshot)
        raise
    return session


def arm_watchdog(adb: Adb, serial: str, snapshot: DeviceSnapshot, delay_s: int) -> None:
    script = f"{build_stop_playback_script()}; {build_restore_script(snapshot)}"
    adb.shell(serial, f"{build_cancel_watchdog_command()}; {build_watchdog_command(script, delay_s)}")


def restore_device(
    adb: Adb, serial: str, snapshot: DeviceSnapshot, should_stop_player: bool = True
) -> None:
    """The single restore path: stop playback, disarm the watchdog, put settings back."""
    parts = []
    if should_stop_player:
        parts.append(build_stop_playback_script())
    parts += [build_cancel_watchdog_command(), build_restore_script(snapshot)]
    adb.shell(serial, "; ".join(parts))


def start_vibration(adb: Adb, serial: str) -> None:
    _shell_checked(adb, serial, build_vibrate_command())


def stop_vibration(adb: Adb, serial: str) -> None:
    adb.shell(serial, "cmd vibrator_manager cancel")


def start_torch(adb: Adb, serial: str, seconds: int) -> None:
    """Blink the camera flash for `seconds`; a blinker that is already running is replaced."""
    command = build_torch_command(seconds)
    if not APP_LISTER_ASSET.is_file():
        raise AdbError(f"applister.jar dosyası eksik: {APP_LISTER_ASSET}")
    adb.run(["push", str(APP_LISTER_ASSET), REMOTE_APP_LISTER], serial, TORCH_PUSH_TIMEOUT_S)
    stop_torch(adb, serial)
    output = adb.shell(serial, command)
    error = next((line for line in output.splitlines() if line.startswith("ERROR ")), "")
    if error:
        raise AdbError(f"Flaş yakılamadı: {error[6:]}")


def stop_torch(adb: Adb, serial: str) -> None:
    adb.shell(serial, build_stop_torch_command())


def blink_screen(adb: Adb, serial: str, snapshot: DeviceSnapshot, is_bright: bool) -> None:
    adb.shell(serial, build_blink_command(snapshot, is_bright))


def start_everything(
    adb: Adb, serial: str, snapshot: DeviceSnapshot, seconds: int
) -> tuple[RingSession, tuple[str, ...]]:
    """Ring, vibrate, blink the flash and light the screen together.

    The ring is required (it raises and undoes itself on failure). The other effects are
    best effort: the names of those that could not start are returned.
    """
    session = start_ring(adb, serial, snapshot, seconds)
    extras = (
        ("ekran", lambda: prepare_flash(adb, serial, snapshot)),
        ("titreşim", lambda: _shell_checked(adb, serial, build_vibrate_command(seconds * MS_PER_SECOND))),
        ("flaş", lambda: start_torch(adb, serial, seconds)),
    )
    skipped = []
    for name, start in extras:
        try:
            start()
        except AdbError:
            skipped.append(name)
    return session, tuple(skipped)


def stop_everything(adb: Adb, serial: str, snapshot: DeviceSnapshot) -> None:
    """Undo `start_everything`: settings first, since those are what must not be left behind."""
    restore_device(adb, serial, snapshot)
    stop_vibration(adb, serial)
    stop_torch(adb, serial)


def prepare_flash(adb: Adb, serial: str, snapshot: DeviceSnapshot) -> None:
    adb.shell(serial, build_flash_script(snapshot))


def pulse_screen(adb: Adb, serial: str, is_on: bool) -> None:
    adb.shell(serial, f"input keyevent {KEYCODE_WAKEUP if is_on else KEYCODE_SLEEP}")


def read_location(adb: Adb, serial: str) -> LocationFix | None:
    uptime, dump = _sections(
        adb,
        serial,
        ("cat /proc/uptime", "dumpsys location | grep -E 'Location\\[[A-Za-z_]+ -?[0-9]'"),
    )
    return parse_location(dump, parse_uptime(uptime))


def read_status(adb: Adb, serial: str) -> PhoneStatus:
    battery, power, window, wifi = _sections(
        adb,
        serial,
        (
            "dumpsys battery",
            "dumpsys power | grep mWakefulness=",
            "dumpsys window | grep isKeyguardShowing",
            "cmd wifi status | grep 'connected to'",
        ),
    )
    info = parse_battery(battery)
    return PhoneStatus(
        battery_level=info.level,
        is_charging=info.is_charging,
        ssid=parse_ssid(wifi),
        is_screen_on=parse_is_awake(power),
        is_locked=parse_is_locked(window),
        transport=transport_of(serial),
    )


def lock_screen(adb: Adb, serial: str) -> None:
    adb.shell(serial, f"input keyevent {KEYCODE_SLEEP}")


def read_owner_info(adb: Adb, serial: str) -> OwnerInfo:
    message, enabled = _sections(
        adb,
        serial,
        ("settings get secure lock_screen_owner_info", "settings get secure lock_screen_owner_info_enabled"),
    )
    return parse_owner_info(message, enabled)


def set_owner_info(adb: Adb, serial: str, message: str, phone: str) -> str:
    """Show `message` + `phone` on the lock screen; returns the exact text that was set."""
    text = validate_owner_message(message, phone)
    _shell_checked(adb, serial, build_owner_info_script(text))
    return text


def clear_owner_info(adb: Adb, serial: str) -> None:
    adb.shell(
        serial,
        "settings put secure lock_screen_owner_info_enabled 0; "
        "settings delete secure lock_screen_owner_info",
    )
