import pytest

from phonedeck.core.adb import AdbError
from phonedeck.core.perf import (
    BatteryReading,
    CpuTimes,
    MemoryInfo,
    NetCounter,
    PerfSample,
    ProcessInfo,
    build_sample,
    compute_rates,
    cpu_usage,
    force_stop,
    kill_process,
    parse_battery_reading,
    parse_cpu_times,
    parse_frequencies,
    parse_meminfo_kb,
    parse_net_dev,
    parse_ps,
    parse_size,
    parse_thermal,
    parse_top,
    read_sample,
    validate_pid,
)

STAT = """cpu  100 0 100 700 100 0 0 0 0 0
cpu0 50 0 50 350 50 0 0 0 0 0
cpu1 50 0 50 350 50 0 0 0 0 0
intr 12345
"""

TOP_MODERN = """Tasks: 791 total,   3 running, 788 sleeping,   0 stopped,   0 zombie
  Mem:  7755304K total,  7647560K used,   107744K free,     2824K buffers
800%cpu  94%user 115%nice 182%sys 364%idle   0%iow  36%irq   9%sirq   0%host
  PID USER         PR  NI VIRT  RES  SHR S[%CPU] %MEM     TIME+ ARGS
 6078 u0_a104      20   0 9.4G 201M 120M S  157   2.6   0:02.40 com.example.shop:remote --flag
 2537 system       18  -2  18G 497M 267M S 30.3   6.5 315:39.18 system_server
 6256 root         20   0    0    0    0 I  3.0   0.0   0:07.90 [kworker/u16:6-memlat_wq]
"""

TOP_LEGACY = """User 10%, System 5%, IOW 0%, IRQ 0%
  PID USER     PR  NI CPU% S  #THR     VSS     RSS PCY UID      Name
 1234 u0_a12   10 -10  12% S    30 900000K  45678K  fg u0_a12   com.example.app
 2345 system   10 -10   0% S    90 900000K  98765K  fg system   system_server
"""

PS = """  PID USER            RSS NAME
    1 root           8724 init
 4321 u0_a55        20480 com.example.chat
"""

BATTERY = """Current Battery Service state:
  AC powered: false
  Max charging current: 0
  Max charging voltage: 0
  status: 2
  health: 2
  level: 33
  voltage: 3884
  temperature: 414
  Battery current : -1072
"""

NET_DEV = (
    "Inter-|   Receive    |  Transmit\n"
    " face |bytes packets errs drop fifo frame compressed multicast"
    "|bytes packets errs drop fifo colls carrier compressed\n"
    "    lo:  100  1 0 0 0 0 0 0   100 1 0 0 0 0 0 0\n"
    " wlan0: 5000 10 0 0 0 0 0 0  7000 9 0 0 0 0 0 0\n"
)


def make_sample(
    taken_at: float = 0.0, cpu: tuple[CpuTimes, ...] = (), network: tuple[NetCounter, ...] = ()
) -> PerfSample:
    return PerfSample(taken_at, cpu, MemoryInfo(), BatteryReading(), network, (), (), ())


class FakeAdb:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.commands: list[str] = []

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.commands.append(command)
        return self.outputs.pop(0)


def full_output(top: str = TOP_MODERN) -> str:
    sections = [STAT, "MemTotal: 100 kB\nMemAvailable: 50 kB", BATTERY, NET_DEV, "cpu|40000", "0 1000", top]
    return "\n@@perfsep@@\n".join(sections)


def test_parse_cpu_times_returns_total_then_cores():
    result = parse_cpu_times(STAT)

    assert len(result) == 3
    assert result[0] == CpuTimes(idle=800, total=1000)
    assert result[1] == CpuTimes(idle=400, total=500)


def test_parse_cpu_times_returns_empty_for_garbage():
    assert parse_cpu_times("nothing useful") == ()


def test_cpu_usage_is_busy_share_between_samples():
    result = cpu_usage(CpuTimes(idle=800, total=1000), CpuTimes(idle=850, total=1100))

    assert result == pytest.approx(50.0)


def test_cpu_usage_is_zero_when_no_time_passed():
    assert cpu_usage(CpuTimes(1, 10), CpuTimes(1, 10)) == 0.0


def test_parse_meminfo_converts_kib_to_bytes():
    text = "MemTotal: 2000 kB\nMemAvailable: 500 kB\nSwapTotal: 100 kB\nSwapFree: 40 kB\n"

    info = parse_meminfo_kb(text)

    assert (info.total, info.available) == (2000 * 1024, 500 * 1024)
    assert (info.swap_total, info.swap_used) == (100 * 1024, 60 * 1024)
    assert info.percent == pytest.approx(75.0)


def test_parse_meminfo_falls_back_to_free_without_available():
    assert parse_meminfo_kb("MemTotal: 100 kB\nMemFree: 25 kB").available == 25 * 1024


def test_memory_percent_is_zero_when_total_unknown():
    assert MemoryInfo().percent == 0.0


def test_parse_battery_ignores_max_charging_lines():
    reading = parse_battery_reading(BATTERY)

    assert reading == BatteryReading(level=33, temperature_c=41.4, voltage_mv=3884, current_ma=-1072, health=2)
    assert reading.health_label == "İyi"


def test_parse_battery_returns_none_fields_when_missing():
    reading = parse_battery_reading("garbage")

    assert reading.level is None and reading.current_ma is None
    assert reading.health_label == "Bilinmiyor"


def test_parse_net_dev_reads_rx_and_tx_per_interface():
    counters = parse_net_dev(NET_DEV)

    assert counters == (NetCounter("lo", 100, 100), NetCounter("wlan0", 5000, 7000))


def test_compute_rates_uses_elapsed_time_and_ignores_loopback():
    before = make_sample(
        10.0, (CpuTimes(800, 1000),), (NetCounter("lo", 0, 0), NetCounter("wlan0", 1000, 1000))
    )
    after = make_sample(
        12.0, (CpuTimes(850, 1100),), (NetCounter("lo", 9999, 9999), NetCounter("wlan0", 5000, 2000))
    )

    rates = compute_rates(before, after)

    assert rates.rx_bytes_per_s == pytest.approx(2000.0)
    assert rates.tx_bytes_per_s == pytest.approx(500.0)
    assert rates.cpu_total == pytest.approx(50.0)


def test_compute_rates_treats_reset_counters_as_zero():
    before = make_sample(0.0, (CpuTimes(0, 0),), (NetCounter("wlan0", 9000, 9000),))
    after = make_sample(1.0, (CpuTimes(0, 0),), (NetCounter("wlan0", 100, 100),))

    rates = compute_rates(before, after)

    assert (rates.rx_bytes_per_s, rates.tx_bytes_per_s) == (0.0, 0.0)


def test_parse_thermal_normalizes_millidegrees_and_skips_bad_lines():
    zones = parse_thermal("cpu-0|47300\nbattery|38\nbroken|\n|1000\nnoseparator")

    assert [(zone.name, zone.temperature_c) for zone in zones] == [("cpu-0", 47.3), ("battery", 38.0)]


def test_parse_frequencies_orders_by_core_and_skips_bad_lines():
    assert parse_frequencies("1 2000\n0 1000\njunk\n2 abc") == (1000, 2000)


@pytest.mark.parametrize(
    ("token", "default_unit", "expected"),
    [
        ("201M", "", 201 * 1024**2),
        ("5.5M", "", int(5.5 * 1024**2)),
        ("0", "", 0),
        ("1234K", "", 1234 * 1024),
        ("512", "K", 512 * 1024),
        ("9.4G", "", int(9.4 * 1024**3)),
    ],
)
def test_parse_size_handles_units(token, default_unit, expected):
    assert parse_size(token, default_unit) == expected


def test_parse_size_rejects_non_numbers():
    assert parse_size("abc") is None


def test_parse_top_modern_layout_with_glued_cpu_header():
    processes = parse_top(TOP_MODERN)

    assert len(processes) == 3
    first = processes[0]
    assert (first.pid, first.user, first.cpu_percent) == (6078, "u0_a104", 157.0)
    assert first.rss_bytes == 201 * 1024**2
    assert first.name == "com.example.shop:remote"
    assert first.package == "com.example.shop"
    assert processes[2].name == "[kworker/u16:6-memlat_wq]" and processes[2].rss_bytes == 0


def test_parse_top_legacy_layout_uses_rss_in_kib_and_percent_suffix():
    processes = parse_top(TOP_LEGACY)

    assert [(p.pid, p.cpu_percent, p.rss_bytes, p.name) for p in processes] == [
        (1234, 12.0, 45678 * 1024, "com.example.app"),
        (2345, 0.0, 98765 * 1024, "system_server"),
    ]


def test_parse_top_returns_empty_without_header():
    assert parse_top("top: not found") == ()


def test_parse_ps_fallback_reads_rss_in_kib():
    processes = parse_ps(PS)

    assert processes == (
        ProcessInfo(1, "root", 0.0, 8724 * 1024, "init"),
        ProcessInfo(4321, "u0_a55", 0.0, 20480 * 1024, "com.example.chat"),
    )


def test_package_is_none_for_daemons_and_kernel_threads():
    assert ProcessInfo(1, "root", 0.0, 0, "system_server").package is None
    assert ProcessInfo(2, "root", 0.0, 0, "[kworker/0:1]").package is None


def test_read_sample_uses_one_round_trip_when_top_works():
    fake = FakeAdb([full_output()])

    sample = read_sample(fake, "serial")

    assert len(fake.commands) == 1
    assert len(sample.cpu) == 3 and len(sample.processes) == 3
    assert sample.battery.level == 33 and sample.thermal[0].temperature_c == 40.0
    assert [c.name for c in sample.network] == ["lo", "wlan0"]


def test_read_sample_falls_back_to_ps_when_top_is_unreadable():
    fake = FakeAdb([full_output("top: not found"), PS])

    sample = read_sample(fake, "serial")

    assert len(fake.commands) == 2 and "ps -A" in fake.commands[1]
    assert [p.pid for p in sample.processes] == [1, 4321]


def test_read_sample_raises_when_sections_are_missing():
    fake = FakeAdb(["only one section"])

    with pytest.raises(AdbError):
        read_sample(fake, "serial")


def test_build_sample_keeps_taken_at():
    sections = [STAT, "", "", "", "", "", TOP_MODERN]

    assert build_sample(sections, 42.0).taken_at == 42.0


@pytest.mark.parametrize("pid", [0, 1, -5, True, "12", 1.5, None])
def test_validate_pid_rejects_bad_values(pid):
    with pytest.raises(ValueError):
        validate_pid(pid)


def test_kill_process_sends_integer_pid_only():
    fake = FakeAdb([""])

    kill_process(fake, "serial", 4321)

    assert fake.commands == ["kill 4321"]


def test_kill_process_rejects_injection_without_touching_the_phone():
    fake = FakeAdb([])

    with pytest.raises(ValueError):
        kill_process(fake, "serial", "1; reboot")

    assert fake.commands == []


def test_force_stop_runs_am_force_stop_for_valid_package():
    fake = FakeAdb([""])

    force_stop(fake, "serial", "com.example.app")

    assert fake.commands == ["am force-stop com.example.app"]


@pytest.mark.parametrize("package", ["", "system_server", "com.example; reboot", "com..app", "a b.c"])
def test_force_stop_rejects_invalid_package(package):
    fake = FakeAdb([])

    with pytest.raises(ValueError):
        force_stop(fake, "serial", package)

    assert fake.commands == []
