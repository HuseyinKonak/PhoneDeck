"""Pure parsers for adb / Android shell output. No I/O here."""

from __future__ import annotations

import re

from .models import (
    AppInfo,
    BatteryInfo,
    BrightnessInfo,
    CallEntry,
    Device,
    FileEntry,
    Notification,
    SmsMessage,
    UsageInfo,
    VolumeInfo,
)

SMS_TYPE_INBOX = "1"
PHONE_KEY_DIGITS = 10
PHONE_KEY_MIN_DIGITS = 7
_ROW_START = re.compile(r"^Row: \d+ ", re.MULTILINE)
_CALL_ROW = re.compile(
    r"number=(?P<number>.*?), type=(?P<type>-?\d+), date=(?P<date>\d+), "
    r"duration=(?P<duration>\d+), name=(?P<name>.*)",
    re.DOTALL,
)
_SMS_ROW = re.compile(
    r"address=(?P<address>.*?), date=(?P<date>\d+), type=(?P<type>\d+), "
    r"read=(?P<read>\d+), body=(?P<body>.*)",
    re.DOTALL,
)
_CONTACT_ROW = re.compile(r"data1=(?P<number>.*?), display_name=(?P<name>.*)", re.DOTALL)
_NOTIFICATION_RECORD = re.compile(r"NotificationRecord\(\S+ pkg=(\S+)")
_NOTIFICATION_EXTRA = re.compile(r"android\.(title|text)=\w+ \((.*)\)\s*$")

KIB = 1024
APP_LINE_PREFIX = "APP"
_LS_LINE = re.compile(
    r"^(?P<perms>[\-dlcbps][\w\-]{9}\S*)\s+\d+\s+\S+\s+\S+\s+"
    r"(?P<size>\d+)\s+(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+(?P<name>.+)$"
)
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def parse_devices(output: str) -> tuple[Device, ...]:
    """Parse `adb devices -l`."""
    devices = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or line.startswith("*"):
            continue
        extras = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append(Device(serial=parts[0], state=parts[1], model=extras.get("model", "")))
    return tuple(devices)


def parse_battery(output: str) -> BatteryInfo:
    """Parse `dumpsys battery`."""
    fields = {}
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    is_charging = any(
        fields.get(key) == "true" for key in ("AC powered", "USB powered", "Wireless powered")
    )
    return BatteryInfo(
        level=_to_int(fields.get("level")),
        is_charging=is_charging,
        temperature_c=_to_int(fields.get("temperature")) / 10,
    )


def parse_df(output: str) -> UsageInfo:
    """Parse `df -k <path>` (1K blocks) into bytes."""
    for line in reversed(output.strip().splitlines()):
        parts = line.split()
        if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit():
            return UsageInfo(total=int(parts[1]) * KIB, used=int(parts[2]) * KIB)
    return UsageInfo()


def parse_meminfo(output: str) -> UsageInfo:
    """Parse `/proc/meminfo` into bytes."""
    values = {}
    for line in output.splitlines():
        match = re.match(r"(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * KIB
    total = values.get("MemTotal", 0)
    return UsageInfo(total=total, used=max(total - values.get("MemAvailable", 0), 0))


def parse_packages(output: str) -> tuple[str, ...]:
    """Parse `pm list packages`."""
    names = (line.partition(":")[2].strip() for line in output.splitlines())
    return tuple(sorted(name for name in names if name))


def parse_app_list(output: str) -> tuple[AppInfo, ...]:
    """Parse AppLister output: `APP<TAB>package<TAB>0|1<TAB>label`, sorted by label."""
    apps = []
    for line in output.splitlines():
        fields = line.rstrip("\r").split("\t", 3)
        if len(fields) != 4 or fields[0] != APP_LINE_PREFIX or not fields[1]:
            continue
        _, package, system_flag, label = fields
        apps.append(AppInfo(package=package, label=label.strip() or package, is_system=system_flag == "1"))
    return tuple(sorted(apps, key=lambda app: (app.label.casefold(), app.package)))


def parse_notifications(output: str) -> tuple[Notification, ...]:
    """Parse the record/title/text lines of `dumpsys notification --noredact`.

    Records without a title and text (group summaries, silent system entries) are dropped.
    """
    notifications: list[Notification] = []
    package, title, text = "", "", ""

    def flush() -> None:
        if package and (title or text):
            notifications.append(Notification(package, title or text, text if title else ""))

    for line in output.splitlines():
        record = _NOTIFICATION_RECORD.search(line)
        if record:
            flush()
            package, title, text = record.group(1), "", ""
            continue
        extra = _NOTIFICATION_EXTRA.search(line)
        if extra and extra.group(2) != "null":
            if extra.group(1) == "title":
                title = extra.group(2).strip()
            else:
                text = extra.group(2).strip()
    flush()
    return tuple(notifications)


def parse_call_log(output: str) -> tuple[CallEntry, ...]:
    """Parse `content query` rows projected as number:type:date:duration:name."""
    calls = []
    for row in _split_rows(output):
        match = _CALL_ROW.match(row)
        if match:
            calls.append(
                CallEntry(
                    number=_null_to_empty(match.group("number")),
                    # Names are single-line; anything after is unrelated shell noise.
                    name=_null_to_empty(match.group("name").split("\n", 1)[0]),
                    kind=int(match.group("type")),
                    timestamp_ms=int(match.group("date")),
                    duration_s=int(match.group("duration")),
                )
            )
    return tuple(calls)


def parse_sms(output: str) -> tuple[SmsMessage, ...]:
    """Parse `content query` rows projected as address:date:type:read:body."""
    messages = []
    for row in _split_rows(output):
        match = _SMS_ROW.match(row)
        if match:
            messages.append(
                SmsMessage(
                    address=_null_to_empty(match.group("address")),
                    body=_null_to_empty(match.group("body")),
                    timestamp_ms=int(match.group("date")),
                    is_incoming=match.group("type") == SMS_TYPE_INBOX,
                    is_read=match.group("read") != "0",
                )
            )
    return tuple(messages)


def parse_contacts(output: str) -> dict[str, str]:
    """Parse rows projected as data1:display_name into {phone key: name}."""
    contacts = {}
    for row in _split_rows(output):
        match = _CONTACT_ROW.match(row)
        if match and phone_key(match.group("number")):
            contacts[phone_key(match.group("number"))] = match.group("name").strip()
    return contacts


def phone_key(number: str) -> str:
    """Last digits of a phone number, so +90 505… and 0505… compare equal."""
    digits = re.sub(r"\D", "", number)
    return digits[-PHONE_KEY_DIGITS:] if len(digits) >= PHONE_KEY_MIN_DIGITS else ""


def _split_rows(output: str) -> list[str]:
    """Split `content query` output into rows; values may span several lines."""
    return [row.strip() for row in _ROW_START.split(output)[1:]]


def _null_to_empty(value: str) -> str:
    stripped = value.strip()
    return "" if stripped == "NULL" else stripped


def parse_ls(output: str) -> tuple[FileEntry, ...]:
    """Parse toybox `ls -la`; directories first, then by name."""
    entries = []
    for line in output.splitlines():
        match = _LS_LINE.match(line.strip())
        if not match:
            continue
        kind = match.group("perms")[0]
        name = match.group("name").split(" -> ")[0] if kind == "l" else match.group("name")
        if name in (".", ".."):
            continue
        entries.append(
            FileEntry(
                name=name,
                is_dir=kind in ("d", "l"),
                size=int(match.group("size")),
                modified=match.group("date"),
                permissions=match.group("perms"),
            )
        )
    return tuple(sorted(entries, key=lambda e: (not e.is_dir, e.name.lower())))


def parse_wm_size(output: str) -> str:
    """Parse `wm size`, preferring the override size when present."""
    sizes = re.findall(r"(\d+x\d+)", output)
    return sizes[-1] if sizes else ""


def parse_ip(output: str) -> str:
    """Parse `ip -f inet addr show <iface>`."""
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", output)
    return match.group(1) if match else ""


def parse_volume(output: str) -> VolumeInfo:
    """Parse `cmd media_session volume --get`."""
    match = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]", output)
    if not match:
        return VolumeInfo(0, 15)
    return VolumeInfo(current=int(match.group(1)), maximum=int(match.group(3)))


def parse_audio_volume(current_output: str, maximum_output: str) -> VolumeInfo | None:
    """Parse `cmd audio get-stream-volume` / `get-max-volume`; None when unsupported."""
    current = re.search(r"->\s*(\d+)", current_output)
    maximum = re.search(r"->\s*(\d+)", maximum_output)
    if not current or not maximum or int(maximum.group(1)) <= 0:
        return None
    return VolumeInfo(current=int(current.group(1)), maximum=int(maximum.group(1)))


def parse_brightness(setting_output: str, range_output: str) -> BrightnessInfo:
    """Combine `settings get system screen_brightness` with the range from `dumpsys display`.

    Stock Android uses 1..255; some vendors (OPPO, Xiaomi) report a wider range.
    """
    default = BrightnessInfo()
    minimum = _first_positive(r"mScreenBrightnessRangeMinimum=([\d.]+)", range_output) or default.minimum
    maximum = _first_positive(r"mScreenBrightnessNormalMaximum=([\d.]+)", range_output) or default.maximum
    if maximum <= minimum:
        minimum, maximum = default.minimum, default.maximum
    current = _to_int(setting_output.strip()) or default.current
    return BrightnessInfo(
        current=min(max(current, minimum), maximum), minimum=minimum, maximum=maximum
    )


def _first_positive(pattern: str, output: str) -> int:
    values = (round(float(value)) for value in re.findall(pattern, output))
    return next((value for value in values if value > 0), 0)


def parse_setting_enabled(output: str) -> bool:
    """Parse `settings get ...` for an on/off value ("null" means unset)."""
    return output.strip() not in ("0", "null", "")


def parse_is_awake(output: str) -> bool:
    """Parse `dumpsys power`; unknown output counts as awake."""
    match = re.search(r"mWakefulness=(\w+)", output)
    return match.group(1) == "Awake" if match else True


def parse_is_playing(output: str) -> bool:
    """Parse `dumpsys media_session`: true when any session is playing."""
    return bool(re.search(r"state=PlaybackState \{state=(PLAYING\(3\)|3\b)", output))


def parse_is_muted(output: str) -> bool:
    """Parse the STREAM_MUSIC block of `dumpsys audio`."""
    match = re.search(r"STREAM_MUSIC:\s*\n\s*Muted:\s*(\w+)", output)
    return match.group(1) == "true" if match else False


def format_size(size: int) -> str:
    value = float(size)
    for unit in _SIZE_UNITS:
        if value < KIB or unit == _SIZE_UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= KIB
    return f"{size} B"


def _to_int(value: str | None) -> int:
    try:
        return int(value) if value is not None else 0
    except ValueError:
        return 0
