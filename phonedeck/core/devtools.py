"""Developer tools for the phone: display, debug flags, power, ports, settings, intents.

Every function takes `(adb, serial)`. Values typed by the user are validated with
strict patterns and then passed through `shlex.quote` before reaching a shell.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from .adb import Adb, AdbError
from .device import DeviceService

NAMESPACES = ("global", "system", "secure")
ANIMATION_SCALES = ("window_animation_scale", "transition_animation_scale", "animator_duration_scale")
ANIMATION_PRESETS = (0.0, 0.5, 1.0)
LAYOUT_PROPERTY = "debug.layout"
LAYOUT_POKE_CODE = 1599295570
STAY_AWAKE_ALL = 7  # AC + USB + wireless bit mask of stay_on_while_plugged_in
MIN_SIZE, MAX_SIZE = 100, 10_000
MIN_DENSITY, MAX_DENSITY = 72, 1_200
MIN_FONT_SCALE, MAX_FONT_SCALE = 0.5, 3.0
MAX_ANIMATION_SCALE = 10.0
MIN_TIMEOUT_MS, MAX_TIMEOUT_MS = 1_000, 86_400_000
MIN_PORT, MAX_PORT = 1, 65_535
INTENT_TIMEOUT = 30

_KEY = re.compile(r"[A-Za-z0-9_.]+")
_COMPONENT = re.compile(r"[A-Za-z0-9_.]+/[A-Za-z0-9_.$]+")
_SIZE = re.compile(r"([0-9]{3,5})x([0-9]{3,5})")
_SPEC = re.compile(r"[a-z]+:[A-Za-z0-9_./@-]+")
_WM_LINE = re.compile(r"^(Physical|Override) (?:size|density): *(\S+)", re.MULTILINE)
_NIGHT = re.compile(r"Night mode: *(\w+)")
_PROP_LINE = re.compile(r"^\[([^\]]+)\]: \[(.*)\]$")
_ERROR_MARKERS = ("Error", "Exception", "Permission denied", "SecurityException")


# --- data -------------------------------------------------------------------
@dataclass(frozen=True)
class WmValue:
    """A `wm size` / `wm density` reading: physical value and optional override."""

    physical: str = ""
    override: str = ""

    @property
    def current(self) -> str:
        return self.override or self.physical


@dataclass(frozen=True)
class DisplayState:
    size: WmValue = WmValue()
    density: WmValue = WmValue()
    night_mode: str = ""
    font_scale: float | None = None
    screen_timeout_ms: int | None = None
    stay_awake: bool = False


@dataclass(frozen=True)
class DebugState:
    show_touches: bool = False
    pointer_location: bool = False
    layout_bounds: bool = False
    animation_scales: tuple[float | None, ...] = (None, None, None)


@dataclass(frozen=True)
class Entry:
    """One property or setting."""

    key: str
    value: str


@dataclass(frozen=True)
class ForwardRule:
    """An `adb forward` (host -> phone) or `adb reverse` (phone -> host) rule."""

    kind: str
    source: str
    target: str


@dataclass(frozen=True)
class PowerAction:
    key: str
    label: str
    args: tuple[str, ...]


POWER_ACTIONS = (
    PowerAction("reboot", "Yeniden başlat", ("reboot",)),
    PowerAction("recovery", "Kurtarma moduna yeniden başlat", ("reboot", "recovery")),
    PowerAction("bootloader", "Bootloader'a yeniden başlat", ("reboot", "bootloader")),
    PowerAction("poweroff", "Kapat", ("shell", "reboot -p")),
)


@dataclass(frozen=True)
class IntentSpec:
    kind: str = "activity"
    action: str = ""
    data: str = ""
    component: str = ""
    categories: tuple[str, ...] = ()
    extras: tuple[tuple[str, str], ...] = ()

    def summary(self) -> str:
        verb = "Yayın" if self.kind == "broadcast" else "Etkinlik"
        target = self.component or self.action or self.data
        return f"{verb}: {target}"


# --- validators -------------------------------------------------------------
def validate_key(key: str) -> str:
    if not _KEY.fullmatch(key):
        raise ValueError(f"Geçersiz anahtar: {key!r} (yalnızca harf, rakam, _ ve .)")
    return key


def validate_namespace(namespace: str) -> str:
    if namespace not in NAMESPACES:
        raise ValueError(f"Geçersiz ayar alanı: {namespace!r}")
    return namespace


def validate_size(text: str) -> str:
    match = _SIZE.fullmatch(text.strip())
    if not match or not all(MIN_SIZE <= int(part) <= MAX_SIZE for part in match.groups()):
        raise ValueError("Çözünürlük 1080x2400 biçiminde olmalı.")
    return match.group(0)


def validate_ranged_int(text: str | int, low: int, high: int, name: str) -> int:
    try:
        value = int(str(text).strip())
    except ValueError:
        raise ValueError(f"{name} bir tam sayı olmalı.") from None
    if not low <= value <= high:
        raise ValueError(f"{name} {low} ile {high} arasında olmalı.")
    return value


def validate_density(value: str | int) -> int:
    return validate_ranged_int(value, MIN_DENSITY, MAX_DENSITY, "Yoğunluk")


def validate_port(value: str | int) -> int:
    return validate_ranged_int(value, MIN_PORT, MAX_PORT, "Port")


def validate_timeout(value: str | int) -> int:
    return validate_ranged_int(value, MIN_TIMEOUT_MS, MAX_TIMEOUT_MS, "Süre (ms)")


def validate_scale(value: float | str, low: float, high: float, name: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} bir sayı olmalı.") from None
    if not low <= number <= high:  # also rejects nan
        raise ValueError(f"{name} {low:g} ile {high:g} arasında olmalı.")
    return f"{number:g}"


def validate_spec(spec: str) -> str:
    if not _SPEC.fullmatch(spec):
        raise ValueError(f"Geçersiz kural: {spec!r}")
    return spec


# --- parsers ----------------------------------------------------------------
def parse_wm(output: str) -> WmValue:
    found = {kind: value for kind, value in _WM_LINE.findall(output)}
    return WmValue(found.get("Physical", ""), found.get("Override", ""))


def parse_night_mode(output: str) -> str:
    match = _NIGHT.search(output)
    return match.group(1).lower() if match else ""


def parse_setting_value(output: str) -> str:
    text = output.strip()
    return "" if text == "null" else text


def parse_float(output: str) -> float | None:
    try:
        return float(parse_setting_value(output))
    except ValueError:
        return None


def parse_int(output: str) -> int | None:
    value = parse_float(output)
    return None if value is None else int(value)


def parse_properties(output: str) -> tuple[Entry, ...]:
    entries = []
    for line in output.splitlines():
        match = _PROP_LINE.match(line.strip())
        if match:
            entries.append(Entry(match.group(1), match.group(2)))
    return tuple(sorted(entries, key=lambda entry: entry.key))


def parse_settings_list(output: str) -> tuple[Entry, ...]:
    """Parse `key=value` lines; lines without `=` continue the previous value."""
    pairs: list[list[str]] = []
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and _KEY.fullmatch(key):
            pairs.append([key, value])
        elif pairs and line:
            pairs[-1][1] += "\n" + line
    return tuple(sorted((Entry(key, value) for key, value in pairs), key=lambda e: e.key))


def parse_rules(output: str, kind: str, serial: str | None = None) -> tuple[ForwardRule, ...]:
    """Parse `adb forward|reverse --list`; `serial` filters forward rules to one phone."""
    rules = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3 or (serial and parts[0] != serial):
            continue
        rules.append(ForwardRule(kind, parts[-2], parts[-1]))
    return tuple(rules)


def parse_extras_text(text: str) -> tuple[tuple[str, str], ...]:
    """Parse one `key=value` pair per line into string extras."""
    extras = []
    for line in text.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"Ekstra 'anahtar=değer' biçiminde olmalı: {line.strip()!r}")
        extras.append((validate_key(key.strip()), value))
    return tuple(extras)


# --- command builders -------------------------------------------------------
def put_setting_command(namespace: str, key: str, value: str | int) -> str:
    return (
        f"settings put {validate_namespace(namespace)} {validate_key(key)} "
        f"{shlex.quote(str(value))}"
    )


def delete_setting_command(namespace: str, key: str) -> str:
    return f"settings delete {validate_namespace(namespace)} {validate_key(key)}"


def get_setting_command(namespace: str, key: str) -> str:
    return f"settings get {validate_namespace(namespace)} {validate_key(key)}"


def layout_bounds_command(enabled: bool) -> str:
    flag = "true" if enabled else "false"
    return f"setprop {LAYOUT_PROPERTY} {flag}; service call activity {LAYOUT_POKE_CODE}"


def build_intent_command(spec: IntentSpec) -> str:
    if spec.kind not in ("activity", "broadcast"):
        raise ValueError(f"Geçersiz intent türü: {spec.kind!r}")
    if not (spec.action or spec.data or spec.component):
        raise ValueError("Eylem, veri veya bileşenden en az biri gerekli.")
    parts = ["am", "start" if spec.kind == "activity" else "broadcast"]
    if spec.action:
        parts += ["-a", shlex.quote(validate_key(spec.action))]
    if spec.data:
        parts += ["-d", shlex.quote(spec.data)]
    if spec.component:
        if not _COMPONENT.fullmatch(spec.component):
            raise ValueError("Bileşen paket/sınıf biçiminde olmalı.")
        parts += ["-n", shlex.quote(spec.component)]
    for category in spec.categories:
        parts += ["-c", shlex.quote(validate_key(category))]
    for key, value in spec.extras:
        parts += ["--es", shlex.quote(validate_key(key)), shlex.quote(value)]
    return " ".join(parts)


# --- device access ----------------------------------------------------------
def _sections(adb: Adb, serial: str, commands: tuple[str, ...]) -> list[str]:
    # Reuses the parallel batch runner: each Android command costs ~0.2 s.
    return DeviceService(adb, serial)._run_sections(commands)


def _shell_checked(adb: Adb, serial: str, command: str) -> str:
    """Run a write command; the shell tools report failures as text, not exit codes."""
    output = adb.shell(serial, command).strip()
    if output.startswith(_ERROR_MARKERS):
        raise AdbError(output.splitlines()[0])
    return output


def read_display(adb: Adb, serial: str) -> DisplayState:
    size, density, night, font, timeout, awake = _sections(
        adb,
        serial,
        (
            "wm size",
            "wm density",
            "cmd uimode night",
            get_setting_command("system", "font_scale"),
            get_setting_command("system", "screen_off_timeout"),
            get_setting_command("global", "stay_on_while_plugged_in"),
        ),
    )
    return DisplayState(
        size=parse_wm(size),
        density=parse_wm(density),
        night_mode=parse_night_mode(night),
        font_scale=parse_float(font),
        screen_timeout_ms=parse_int(timeout),
        stay_awake=bool(parse_int(awake)),
    )


def set_size(adb: Adb, serial: str, size: str) -> None:
    _shell_checked(adb, serial, f"wm size {validate_size(size)}")


def reset_size(adb: Adb, serial: str) -> None:
    _shell_checked(adb, serial, "wm size reset")


def set_density(adb: Adb, serial: str, density: str | int) -> None:
    _shell_checked(adb, serial, f"wm density {validate_density(density)}")


def reset_density(adb: Adb, serial: str) -> None:
    _shell_checked(adb, serial, "wm density reset")


def set_dark_mode(adb: Adb, serial: str, enabled: bool) -> None:
    _shell_checked(adb, serial, f"cmd uimode night {'yes' if enabled else 'no'}")


def set_font_scale(adb: Adb, serial: str, scale: float | str) -> None:
    value = validate_scale(scale, MIN_FONT_SCALE, MAX_FONT_SCALE, "Yazı boyutu")
    _shell_checked(adb, serial, put_setting_command("system", "font_scale", value))


def set_screen_timeout(adb: Adb, serial: str, milliseconds: str | int) -> None:
    value = validate_timeout(milliseconds)
    _shell_checked(adb, serial, put_setting_command("system", "screen_off_timeout", value))


def set_stay_awake(adb: Adb, serial: str, enabled: bool) -> None:
    value = STAY_AWAKE_ALL if enabled else 0
    _shell_checked(adb, serial, put_setting_command("global", "stay_on_while_plugged_in", value))


def read_debug(adb: Adb, serial: str) -> DebugState:
    touches, pointer, layout, *scales = _sections(
        adb,
        serial,
        (
            get_setting_command("system", "show_touches"),
            get_setting_command("system", "pointer_location"),
            f"getprop {LAYOUT_PROPERTY}",
            *(get_setting_command("global", name) for name in ANIMATION_SCALES),
        ),
    )
    return DebugState(
        show_touches=bool(parse_int(touches)),
        pointer_location=bool(parse_int(pointer)),
        layout_bounds=layout.strip() == "true",
        animation_scales=tuple(parse_float(scale) for scale in scales),
    )


def set_debug_flag(adb: Adb, serial: str, key: str, enabled: bool) -> None:
    """Switch `show_touches` or `pointer_location` (system settings)."""
    _shell_checked(adb, serial, put_setting_command("system", key, int(enabled)))


def set_layout_bounds(adb: Adb, serial: str, enabled: bool) -> None:
    _shell_checked(adb, serial, layout_bounds_command(enabled))


def set_animation_scales(
    adb: Adb, serial: str, value: float | str, names: tuple[str, ...] = ANIMATION_SCALES
) -> None:
    scale = validate_scale(value, 0.0, MAX_ANIMATION_SCALE, "Animasyon ölçeği")
    unknown = [name for name in names if name not in ANIMATION_SCALES]
    if unknown:
        raise ValueError(f"Bilinmeyen animasyon ayarı: {unknown[0]}")
    commands = "; ".join(put_setting_command("global", name, scale) for name in names)
    _shell_checked(adb, serial, commands)


def power(adb: Adb, serial: str, key: str) -> None:
    action = next((item for item in POWER_ACTIONS if item.key == key), None)
    if action is None:
        raise ValueError(f"Bilinmeyen güç işlemi: {key!r}")
    adb.run(list(action.args), serial)


def list_rules(adb: Adb, serial: str) -> tuple[ForwardRule, ...]:
    forwards = parse_rules(adb.run(["forward", "--list"], serial), "forward", serial)
    reverses = parse_rules(adb.run(["reverse", "--list"], serial), "reverse")
    return forwards + reverses


def add_rule(adb: Adb, serial: str, kind: str, host_port: str | int, device_port: str | int) -> None:
    host = f"tcp:{validate_port(host_port)}"
    device = f"tcp:{validate_port(device_port)}"
    if kind == "forward":
        adb.run(["forward", host, device], serial)
    elif kind == "reverse":
        adb.run(["reverse", device, host], serial)
    else:
        raise ValueError(f"Geçersiz kural türü: {kind!r}")


def remove_rule(adb: Adb, serial: str, rule: ForwardRule) -> None:
    if rule.kind not in ("forward", "reverse"):
        raise ValueError(f"Geçersiz kural türü: {rule.kind!r}")
    adb.run([rule.kind, "--remove", validate_spec(rule.source)], serial)


def read_properties(adb: Adb, serial: str) -> tuple[Entry, ...]:
    return parse_properties(adb.shell(serial, "getprop"))


def read_settings(adb: Adb, serial: str, namespace: str) -> tuple[Entry, ...]:
    return parse_settings_list(adb.shell(serial, f"settings list {validate_namespace(namespace)}"))


def put_setting(adb: Adb, serial: str, namespace: str, key: str, value: str) -> None:
    if not value.strip():
        raise ValueError("Değer boş olamaz.")
    _shell_checked(adb, serial, put_setting_command(namespace, key, value))


def delete_setting(adb: Adb, serial: str, namespace: str, key: str) -> None:
    _shell_checked(adb, serial, delete_setting_command(namespace, key))


def run_intent(adb: Adb, serial: str, spec: IntentSpec) -> str:
    """Start the activity or send the broadcast; returns the tool's own output."""
    return adb.shell(serial, build_intent_command(spec), INTENT_TIMEOUT).strip()
