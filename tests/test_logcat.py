import queue
import threading

import pytest

from phonedeck.core.adb import AdbError
from phonedeck.core.logcat import (
    LogFilter,
    LogLine,
    LogcatReader,
    build_args,
    clear_device_buffer,
    filter_lines,
    format_line,
    parse_logcat_line,
    resolve_package_pids,
)

SAMPLE = "01-02 03:04:05.678  1234  5678 W ActivityManager: Slow operation: 42ms"
WAIT_S = 3


def make_line(level="I", tag="Tag", message="hello", pid=1) -> LogLine:
    return LogLine("01-02 03:04:05.000", pid, pid, level, tag, message)


class PipeStdout:
    """Blocking readline source that ends when closed or fed the end marker."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self.is_closed = False

    def feed(self, text: str) -> None:
        self._queue.put(text.encode())

    def end(self) -> None:
        self._queue.put(b"")

    def readline(self) -> bytes:
        return self._queue.get(timeout=WAIT_S)

    def close(self) -> None:
        self.is_closed = True


class FakeProcess:
    def __init__(self, stdout: PipeStdout) -> None:
        self.stdout = stdout
        self.returncode: int | None = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = 0
        self.stdout.end()

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9
        self.stdout.end()

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class FakePopenAdb:
    def __init__(self, shell_output: str = "") -> None:
        self.stdout = PipeStdout()
        self.process = FakeProcess(self.stdout)
        self.popen_args: list[list[str]] = []
        self.shell_output = shell_output
        self.shell_calls: list[str] = []
        self.run_calls: list[list[str]] = []

    def popen(self, args: list[str], serial: str) -> FakeProcess:
        self.popen_args.append(args)
        return self.process

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.shell_calls.append(command)
        return self.shell_output

    def run(self, args: list[str], serial: str | None = None, timeout: float = 30) -> str:
        self.run_calls.append(args)
        return ""


class Collector:
    def __init__(self) -> None:
        self.lines: list[LogLine] = []
        self.stopped: list[str] = []
        self.got_lines = threading.Event()
        self.got_stopped = threading.Event()

    def on_batch(self, batch: list[LogLine]) -> None:
        self.lines.extend(batch)
        self.got_lines.set()

    def on_stopped(self, reason: str) -> None:
        self.stopped.append(reason)
        self.got_stopped.set()


def make_reader(adb: FakePopenAdb, **kwargs) -> tuple[LogcatReader, Collector]:
    collector = Collector()
    reader = LogcatReader(adb, "serial", collector.on_batch, collector.on_stopped, **kwargs)
    return reader, collector


# --- parser ----------------------------------------------------------------
def test_parse_logcat_line_reads_all_fields():
    line = parse_logcat_line(SAMPLE)

    assert line == LogLine("01-02 03:04:05.678", 1234, 5678, "W", "ActivityManager", "Slow operation: 42ms")


def test_parse_logcat_line_strips_padding_after_tag():
    line = parse_logcat_line("01-02 03:04:05.678   99    99 I adbd    : service requested")

    assert (line.tag, line.message) == ("adbd", "service requested")


def test_parse_logcat_line_keeps_empty_message():
    line = parse_logcat_line("01-02 03:04:05.678  1  2 D Tag: ")

    assert line is not None and line.message == ""


@pytest.mark.parametrize("text", ["--------- beginning of main", "", "garbage", "- waiting for device -"])
def test_parse_logcat_line_returns_none_for_non_records(text):
    assert parse_logcat_line(text) is None


def test_parse_logcat_line_ignores_trailing_crlf():
    line = parse_logcat_line(SAMPLE + "\r\n")

    assert line is not None and line.message == "Slow operation: 42ms"


def test_format_line_round_trips_through_parser():
    original = parse_logcat_line(SAMPLE)

    assert parse_logcat_line(format_line(original)) == original


# --- filter ----------------------------------------------------------------
def test_filter_by_minimum_level_keeps_equal_and_higher():
    lines = [make_line(level) for level in "VDIWEF"]

    result = filter_lines(lines, LogFilter(min_level="W"))

    assert [line.level for line in result] == ["W", "E", "F"]


def test_filter_by_tag_substring_is_case_insensitive():
    lines = [make_line(tag="ActivityManager"), make_line(tag="Wifi")]

    result = filter_lines(lines, LogFilter(tag="activity"))

    assert [line.tag for line in result] == ["ActivityManager"]


def test_filter_by_text_substring_is_case_insensitive():
    lines = [make_line(message="Connection LOST"), make_line(message="ok")]

    result = filter_lines(lines, LogFilter(text="lost"))

    assert len(result) == 1


def test_filter_by_regex_matches_message_pattern():
    lines = [make_line(message="took 42ms"), make_line(message="took long")]

    result = filter_lines(lines, LogFilter(text=r"\d+ms", is_regex=True))

    assert [line.message for line in result] == ["took 42ms"]


def test_filter_invalid_regex_raises_value_error():
    with pytest.raises(ValueError, match="düzenli ifade"):
        LogFilter(text="(", is_regex=True).matcher()


def test_filter_by_pids_keeps_only_listed_processes():
    lines = [make_line(pid=1), make_line(pid=2), make_line(pid=3)]

    result = filter_lines(lines, LogFilter(pids=frozenset({1, 3})))

    assert [line.pid for line in result] == [1, 3]


def test_filter_with_empty_pid_set_shows_nothing():
    assert filter_lines([make_line()], LogFilter(pids=frozenset())) == []


def test_filter_defaults_show_everything():
    lines = [make_line("V"), make_line("F")]

    assert filter_lines(lines, LogFilter()) == lines


def test_filter_combines_all_criteria():
    lines = [
        make_line("E", "Net", "timeout", pid=7),
        make_line("E", "Net", "timeout", pid=8),
        make_line("I", "Net", "timeout", pid=7),
    ]

    result = filter_lines(lines, LogFilter("W", "net", "time", False, frozenset({7})))

    assert result == [lines[0]]


# --- helpers ---------------------------------------------------------------
def test_resolve_package_pids_parses_multiple_ids():
    adb = FakePopenAdb("123 456\n")

    pids = resolve_package_pids(adb, "serial", "com.example.app")

    assert pids == frozenset({123, 456})
    assert "com.example.app" in adb.shell_calls[0]


def test_resolve_package_pids_empty_when_not_running():
    assert resolve_package_pids(FakePopenAdb(""), "serial", "com.example.app") == frozenset()


@pytest.mark.parametrize("name", ["", "a b", "com.x; reboot", "$(id)"])
def test_resolve_package_pids_rejects_unsafe_names(name):
    adb = FakePopenAdb()

    with pytest.raises(ValueError):
        resolve_package_pids(adb, "serial", name)

    assert adb.shell_calls == []


def test_clear_device_buffer_runs_logcat_clear():
    adb = FakePopenAdb()

    clear_device_buffer(adb, "serial")

    assert adb.run_calls == [["logcat", "-c"]]


def test_build_args_includes_buffer_and_tail():
    assert build_args("system", True) == [
        "logcat", "-v", "threadtime", "-b", "system", "-T", "500"
    ]


def test_build_args_without_tail_omits_it():
    assert "-T" not in build_args("all", False)


def test_build_args_rejects_unknown_buffer():
    with pytest.raises(ValueError):
        build_args("radio; reboot")


# --- reader ----------------------------------------------------------------
def test_reader_delivers_parsed_lines_and_skips_headers():
    adb = FakePopenAdb()
    reader, collector = make_reader(adb)
    reader.start()

    adb.stdout.feed("--------- beginning of main\n")
    adb.stdout.feed(SAMPLE + "\n")
    assert collector.got_lines.wait(WAIT_S)
    reader.stop()

    assert [line.pid for line in collector.lines] == [1234]


def test_reader_passes_buffer_and_tail_to_adb():
    adb = FakePopenAdb()
    reader, _ = make_reader(adb, buffer="crash", tail=False)

    reader.start()
    reader.stop()

    assert adb.popen_args == [["logcat", "-v", "threadtime", "-b", "crash"]]


def test_reader_batches_many_lines_into_few_callbacks():
    adb = FakePopenAdb()
    batches: list[int] = []
    done = threading.Event()
    total = 300

    def on_batch(batch: list[LogLine]) -> None:
        batches.append(len(batch))
        if sum(batches) >= total:
            done.set()

    reader = LogcatReader(adb, "serial", on_batch)
    reader.start()
    for _ in range(total):
        adb.stdout.feed(SAMPLE + "\n")
    assert done.wait(WAIT_S)
    reader.stop()

    assert sum(batches) == total and len(batches) < total


def test_reader_stop_terminates_process_and_joins_threads():
    adb = FakePopenAdb()
    reader, collector = make_reader(adb)
    reader.start()

    reader.stop()

    assert adb.process.terminate_calls == 1
    assert adb.stdout.is_closed
    assert not any(thread.is_alive() for thread in reader._threads)
    assert collector.stopped == []


def test_reader_stop_is_idempotent():
    adb = FakePopenAdb()
    reader, _ = make_reader(adb)
    reader.start()

    reader.stop()
    reader.stop()

    assert adb.process.terminate_calls == 1


def test_reader_stop_kills_process_that_ignores_terminate():
    adb = FakePopenAdb()
    calls = {"wait": 0}

    def stubborn_terminate() -> None:
        adb.process.terminate_calls += 1

    def wait(timeout: float | None = None) -> int:
        import subprocess

        calls["wait"] += 1
        if calls["wait"] == 1:
            raise subprocess.TimeoutExpired("adb", timeout or 0)
        return 0

    adb.process.terminate = stubborn_terminate
    adb.process.wait = wait
    reader, _ = make_reader(adb)
    reader.start()

    reader.stop()

    assert adb.process.kill_calls == 1


def test_reader_reports_when_adb_exits_by_itself_with_reason():
    adb = FakePopenAdb()
    reader, collector = make_reader(adb)
    reader.start()

    adb.stdout.feed("error: device offline\n")
    adb.stdout.end()
    assert collector.got_stopped.wait(WAIT_S)
    reader.stop()

    assert collector.stopped == ["error: device offline"]


def test_reader_does_not_report_stop_requested_by_caller():
    adb = FakePopenAdb()
    reader, collector = make_reader(adb)
    reader.start()
    adb.stdout.feed(SAMPLE + "\n")
    assert collector.got_lines.wait(WAIT_S)

    reader.stop()

    assert not collector.got_stopped.is_set()


def test_reader_start_propagates_adb_failure():
    class BrokenAdb(FakePopenAdb):
        def popen(self, args, serial):
            raise AdbError("adb çalıştırılamadı")

    reader, _ = make_reader(BrokenAdb())

    with pytest.raises(AdbError):
        reader.start()


def test_reader_cannot_be_started_twice():
    adb = FakePopenAdb()
    reader, _ = make_reader(adb)
    reader.start()

    with pytest.raises(RuntimeError):
        reader.start()

    reader.stop()
