"""Live performance sampling: CPU, memory, battery, network, temperatures, processes."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

from .adb import Adb, AdbError
from .device import REMOTE_STATE_DIR, validate_package

logger = logging.getLogger(__name__)

SAMPLE_TIMEOUT = 20
# The script text shows up in the phone's own process list, so the separator it
# prints must differ from the separator it contains (the empty quotes are dropped by the shell).
SEPARATOR = "@@perfsep@@"
_SEPARATOR_ECHO = "@@perf''sep@@"
PS_COMMAND = "ps -A -o PID,USER,RSS,NAME"
KIB = 1024
MILLI = 1000
TEMPERATURE_TENTHS = 10
MIN_KILL_PID = 2
MIN_INTERVAL_S = 0.001
MILLIDEGREE_THRESHOLD = 1000
_SIZE_FACTORS = {"": 1, "K": KIB, "M": KIB**2, "G": KIB**3, "T": KIB**4}
_CPU_FIELDS_USED = 8
_IDLE_FIELDS = (3, 4)
_SIZE_TOKEN = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]?)$", re.IGNORECASE)
_HEALTH_LABELS = {
    2: "İyi",
    3: "Aşırı ısınmış",
    4: "Bozuk",
    5: "Aşırı voltaj",
    6: "Arızalı",
    7: "Soğuk",
}
_UNKNOWN_HEALTH = "Bilinmiyor"

_SAMPLE_COMMANDS = (
    "cat /proc/stat",
    "cat /proc/meminfo",
    "dumpsys battery",
    "cat /proc/net/dev",
    'for z in /sys/class/thermal/thermal_zone*; do echo "$(cat $z/type)|$(cat $z/temp)"; done',
    'for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq; do '
    'n=${f#/sys/devices/system/cpu/cpu}; echo "${n%%/*} $(cat $f)"; done',
    "top -b -n 1",
)


@dataclass(frozen=True)
class CpuTimes:
    """Cumulative jiffies of one CPU line in /proc/stat."""

    idle: int
    total: int


@dataclass(frozen=True)
class MemoryInfo:
    total: int = 0
    available: int = 0
    swap_total: int = 0
    swap_free: int = 0

    @property
    def used(self) -> int:
        return max(self.total - self.available, 0)

    @property
    def swap_used(self) -> int:
        return max(self.swap_total - self.swap_free, 0)

    @property
    def percent(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


@dataclass(frozen=True)
class BatteryReading:
    level: int | None = None
    temperature_c: float | None = None
    voltage_mv: int | None = None
    current_ma: int | None = None
    health: int | None = None

    @property
    def health_label(self) -> str:
        return _HEALTH_LABELS.get(self.health, _UNKNOWN_HEALTH) if self.health else _UNKNOWN_HEALTH


@dataclass(frozen=True)
class NetCounter:
    name: str
    rx_bytes: int
    tx_bytes: int


@dataclass(frozen=True)
class ThermalZone:
    name: str
    temperature_c: float


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    user: str
    cpu_percent: float
    rss_bytes: int
    name: str

    @property
    def package(self) -> str | None:
        """Package this process belongs to, or None for system daemons and kernel threads."""
        candidate = self.name.split(":", 1)[0]
        try:
            return validate_package(candidate)
        except ValueError:
            return None


@dataclass(frozen=True)
class PerfSample:
    """Everything read from the phone in one round trip."""

    taken_at: float
    cpu: tuple[CpuTimes, ...]  # index 0 is the overall line, then one entry per core
    memory: MemoryInfo
    battery: BatteryReading
    network: tuple[NetCounter, ...]
    thermal: tuple[ThermalZone, ...]
    frequencies_khz: tuple[int, ...]
    processes: tuple[ProcessInfo, ...]


@dataclass(frozen=True)
class PerfRates:
    """Values derived from two consecutive samples."""

    cpu_total: float
    cpu_cores: tuple[float, ...]
    rx_bytes_per_s: float
    tx_bytes_per_s: float


# --- parsers ----------------------------------------------------------------
def _to_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def parse_cpu_times(text: str) -> tuple[CpuTimes, ...]:
    """Overall line first, then per-core lines, from /proc/stat."""
    total_line: CpuTimes | None = None
    cores: dict[int, CpuTimes] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or not parts[0].startswith("cpu"):
            continue
        values = [_to_int(part) for part in parts[1:1 + _CPU_FIELDS_USED]]
        if len(values) < len(_IDLE_FIELDS) + 1 or None in values:
            continue
        idle = sum(values[index] for index in _IDLE_FIELDS if index < len(values))
        times = CpuTimes(idle=idle, total=sum(values))
        suffix = parts[0][3:]
        if not suffix:
            total_line = times
        elif suffix.isdigit():
            cores[int(suffix)] = times
    if total_line is None:
        return ()
    return (total_line, *(cores[index] for index in sorted(cores)))


def cpu_usage(previous: CpuTimes, current: CpuTimes) -> float:
    """Busy share between two readings, 0-100."""
    total = current.total - previous.total
    if total <= 0:
        return 0.0
    busy = total - (current.idle - previous.idle)
    return min(max(100.0 * busy / total, 0.0), 100.0)


def parse_meminfo_kb(text: str) -> MemoryInfo:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * KIB
    total = values.get("MemTotal", 0)
    return MemoryInfo(
        total=total,
        available=values.get("MemAvailable", values.get("MemFree", 0)),
        swap_total=values.get("SwapTotal", 0),
        swap_free=values.get("SwapFree", 0),
    )


def _battery_value(text: str, pattern: str) -> int | None:
    match = re.search(rf"^\s*{pattern}\s*:\s*(-?\d+)", text, re.MULTILINE | re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_battery_reading(text: str) -> BatteryReading:
    tenths = _battery_value(text, "temperature")
    return BatteryReading(
        level=_battery_value(text, "level"),
        temperature_c=tenths / TEMPERATURE_TENTHS if tenths is not None else None,
        voltage_mv=_battery_value(text, "voltage"),
        current_ma=_battery_value(text, r"(?:battery current|current now)"),
        health=_battery_value(text, "health"),
    )


def parse_net_dev(text: str) -> tuple[NetCounter, ...]:
    counters = []
    for line in text.splitlines():
        name, separator, rest = line.partition(":")
        fields = rest.split()
        if not separator or len(fields) < 9:
            continue
        rx, tx = _to_int(fields[0]), _to_int(fields[8])
        if rx is not None and tx is not None:
            counters.append(NetCounter(name.strip(), rx, tx))
    return tuple(counters)


def parse_thermal(text: str) -> tuple[ThermalZone, ...]:
    zones = []
    for line in text.splitlines():
        name, separator, raw = line.partition("|")
        value = _to_int(raw.strip())
        if not separator or not name.strip() or value is None:
            continue
        celsius = value / MILLI if abs(value) >= MILLIDEGREE_THRESHOLD else float(value)
        zones.append(ThermalZone(name.strip(), celsius))
    return tuple(zones)


def parse_frequencies(text: str) -> tuple[int, ...]:
    """Per-core current frequency in kHz, ordered by core number."""
    found: dict[int, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        core, khz = _to_int(parts[0]), _to_int(parts[1])
        if core is not None and khz is not None:
            found[core] = khz
    return tuple(found[core] for core in sorted(found))


def parse_size(token: str, default_unit: str = "") -> int | None:
    """Turn '201M', '5.5M', '1234K' or '0' into bytes. Bare numbers use `default_unit`."""
    match = _SIZE_TOKEN.match(token.strip())
    if not match:
        return None
    unit = match.group(2).upper() or default_unit
    return int(float(match.group(1)) * _SIZE_FACTORS[unit])


def _top_columns(header: str) -> list[str]:
    # Some toybox versions glue the state and CPU headers: "S[%CPU]".
    return header.replace("S[%CPU]", "S %CPU").split()


def _column(columns: list[str], *names: str) -> int | None:
    lowered = [column.lower() for column in columns]
    for name in names:
        if name in lowered:
            return lowered.index(name)
    return None


def parse_top(text: str) -> tuple[ProcessInfo, ...]:
    """Parse `top -b -n 1`; columns are located from the header because they vary by Android version."""
    lines = text.splitlines()
    header_at = next((i for i, line in enumerate(lines) if line.split()[:1] == ["PID"]), None)
    if header_at is None:
        return ()
    columns = _top_columns(lines[header_at])
    pid_at, user_at = _column(columns, "pid"), _column(columns, "user")
    cpu_at = _column(columns, "%cpu", "cpu%")
    mem_at = _column(columns, "res", "rss")
    name_at = _column(columns, "args", "name", "cmd", "command")
    if None in (pid_at, cpu_at, mem_at, name_at):
        return ()
    processes = []
    for line in lines[header_at + 1:]:
        fields = line.split(None, len(columns) - 1)
        process = _top_row(fields, len(columns), pid_at, user_at, cpu_at, mem_at, name_at)
        if process:
            processes.append(process)
    return tuple(processes)


def _top_row(
    fields: list[str], width: int, pid_at: int, user_at: int | None,
    cpu_at: int, mem_at: int, name_at: int,
) -> ProcessInfo | None:
    if len(fields) < width:
        return None
    pid = _to_int(fields[pid_at])
    rss = parse_size(fields[mem_at], "K")
    try:
        cpu = float(fields[cpu_at].rstrip("%"))
    except ValueError:
        return None
    name = fields[name_at].split()[0] if fields[name_at].split() else ""
    if pid is None or rss is None or not name:
        return None
    user = fields[user_at] if user_at is not None else ""
    return ProcessInfo(pid, user, cpu, rss, name)


def parse_ps(text: str) -> tuple[ProcessInfo, ...]:
    """Fallback for `ps -A -o PID,USER,RSS,NAME`; no CPU figure is available."""
    processes = []
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, rss_kb = _to_int(parts[0]), _to_int(parts[2])
        if pid is None or rss_kb is None:
            continue
        processes.append(ProcessInfo(pid, parts[1], 0.0, rss_kb * KIB, parts[3].strip()))
    return tuple(processes)


def compute_rates(previous: PerfSample, current: PerfSample) -> PerfRates:
    """CPU load and network speed between two samples."""
    cores = tuple(cpu_usage(a, b) for a, b in zip(previous.cpu[1:], current.cpu[1:]))
    total = cpu_usage(previous.cpu[0], current.cpu[0]) if previous.cpu and current.cpu else 0.0
    seconds = max(current.taken_at - previous.taken_at, MIN_INTERVAL_S)
    before = {counter.name: counter for counter in previous.network if counter.name != "lo"}
    rx = tx = 0
    for counter in current.network:
        old = before.get(counter.name)
        if old is None:
            continue
        # A counter that went backwards means the interface was reset; count it as zero.
        rx += max(counter.rx_bytes - old.rx_bytes, 0)
        tx += max(counter.tx_bytes - old.tx_bytes, 0)
    return PerfRates(total, cores, rx / seconds, tx / seconds)


# --- readers ------------------------------------------------------------------
def _run_parallel(adb: Adb, serial: str, commands: tuple[str, ...]) -> list[str]:
    """Run commands side by side on the phone in one adb call; outputs come back in order."""
    directory = f"{REMOTE_STATE_DIR}/perf$$"
    jobs = " ".join(
        f"({command}) >{directory}/{index} 2>/dev/null &" for index, command in enumerate(commands)
    )
    reads = f"; echo {_SEPARATOR_ECHO}; ".join(f"cat {directory}/{index}" for index in range(len(commands)))
    script = f"mkdir -p {directory}; {jobs} wait; {reads}; rm -rf {directory}"
    parts = [part.strip() for part in adb.shell(serial, script, SAMPLE_TIMEOUT).split(SEPARATOR)]
    if len(parts) != len(commands):
        raise AdbError("Performans verisi okunamadı.")
    return parts


def build_sample(
    sections: list[str], taken_at: float, ps_output: str = ""
) -> PerfSample:
    stat, meminfo, battery, net, thermal, freq, top = sections
    processes = parse_top(top) or parse_ps(ps_output)
    return PerfSample(
        taken_at=taken_at,
        cpu=parse_cpu_times(stat),
        memory=parse_meminfo_kb(meminfo),
        battery=parse_battery_reading(battery),
        network=parse_net_dev(net),
        thermal=parse_thermal(thermal),
        frequencies_khz=parse_frequencies(freq),
        processes=processes,
    )


def read_sample(adb: Adb, serial: str) -> PerfSample:
    """One full reading in a single adb round trip."""
    sections = _run_parallel(adb, serial, _SAMPLE_COMMANDS)
    taken_at = time.monotonic()
    if parse_top(sections[-1]):
        return build_sample(sections, taken_at)
    # `top` is missing or has an unknown layout: fall back to `ps` (costs a second round trip).
    logger.info("top output not recognised, falling back to ps")
    return build_sample(sections, taken_at, adb.shell(serial, PS_COMMAND, SAMPLE_TIMEOUT))


# --- actions ------------------------------------------------------------------
def validate_pid(pid: int) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < MIN_KILL_PID:
        raise ValueError(f"Geçersiz süreç numarası: {pid}")
    return pid


def kill_process(adb: Adb, serial: str, pid: int) -> None:
    adb.shell(serial, f"kill {validate_pid(pid)}")


def force_stop(adb: Adb, serial: str, package: str) -> None:
    adb.shell(serial, f"am force-stop {validate_package(package)}")
