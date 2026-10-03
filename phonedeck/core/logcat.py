"""Live logcat: parsing, filtering and a background reader around `adb logcat`."""

from __future__ import annotations

import logging
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .adb import Adb, AdbError

logger = logging.getLogger(__name__)

LEVELS = ("V", "D", "I", "W", "E", "F")
LEVEL_RANK = {level: rank for rank, level in enumerate(LEVELS)}
# `-b` argument per buffer choice offered to the user.
BUFFERS = ("main", "system", "crash", "events", "all")
DEFAULT_BUFFER = "main"
TAIL_LINES = 500
BATCH_INTERVAL_S = 0.05
MAX_BATCH_LINES = 2000
STOP_TIMEOUT_S = 2.0
MAX_EXIT_NOTES = 3
PID_QUERY_TIMEOUT_S = 10
PACKAGE_PATTERN = re.compile(r"^[A-Za-z0-9_.]+$")
_LINE_PATTERN = re.compile(
    r"^(\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+(\d+)\s+(\d+)\s+([VDIWEFS])\s+(.*?)\s*:\s?(.*)$"
)
_BUFFER_HEADER = "--------- "
_END = object()


@dataclass(frozen=True)
class LogLine:
    """One `-v threadtime` record."""

    timestamp: str
    pid: int
    tid: int
    level: str
    tag: str
    message: str


@dataclass(frozen=True)
class LogFilter:
    """Which lines to show. `pids=None` means any process; an empty set shows nothing."""

    min_level: str = "V"
    tag: str = ""
    text: str = ""
    is_regex: bool = False
    pids: frozenset[int] | None = None

    def matcher(self) -> Callable[[LogLine], bool]:
        """Compile into a predicate. Raises ValueError for an invalid regex."""
        min_rank = LEVEL_RANK.get(self.min_level, 0)
        tag = self.tag.strip().lower()
        text = self.text.lower()
        pattern = _compile_pattern(self.text) if self.is_regex and self.text else None

        def matches(line: LogLine) -> bool:
            if LEVEL_RANK.get(line.level, 0) < min_rank:
                return False
            if self.pids is not None and line.pid not in self.pids:
                return False
            if tag and tag not in line.tag.lower():
                return False
            if pattern is not None:
                return pattern.search(line.message) is not None
            return not text or text in line.message.lower()

        return matches


def _compile_pattern(expression: str) -> re.Pattern[str]:
    try:
        return re.compile(expression, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Geçersiz düzenli ifade: {exc}") from exc


def parse_logcat_line(line: str) -> LogLine | None:
    """Parse one `adb logcat -v threadtime` line; None for headers and noise."""
    found = _LINE_PATTERN.match(line.rstrip("\r\n"))
    if found is None:
        return None
    timestamp, pid, tid, level, tag, message = found.groups()
    return LogLine(timestamp, int(pid), int(tid), level, tag, message)


def format_line(line: LogLine) -> str:
    """Render a record back to plain text (used when saving)."""
    return f"{line.timestamp} {line.pid:>5} {line.tid:>5} {line.level} {line.tag}: {line.message}"


def filter_lines(lines: tuple[LogLine, ...] | list[LogLine], criteria: LogFilter) -> list[LogLine]:
    matches = criteria.matcher()
    return [line for line in lines if matches(line)]


def resolve_package_pids(adb: Adb, serial: str, package: str) -> frozenset[int]:
    """Process ids currently running `package`; empty when it is not running."""
    name = package.strip()
    if not PACKAGE_PATTERN.match(name):
        raise ValueError("Geçersiz paket adı. Örnek: com.android.chrome")
    output = adb.shell(serial, f"pidof {name} || true", timeout=PID_QUERY_TIMEOUT_S)
    return frozenset(int(token) for token in output.split() if token.isdigit())


def clear_device_buffer(adb: Adb, serial: str) -> None:
    """Empty the phone's log buffers (`logcat -c`)."""
    adb.run(["logcat", "-c"], serial, timeout=PID_QUERY_TIMEOUT_S)


def build_args(buffer: str = DEFAULT_BUFFER, tail: bool = True) -> list[str]:
    if buffer not in BUFFERS:
        raise ValueError(f"Bilinmeyen log tamponu: {buffer}")
    args = ["logcat", "-v", "threadtime", "-b", buffer]
    if tail:
        args += ["-T", str(TAIL_LINES)]
    return args


class LogcatReader:
    """Streams `adb logcat` on background threads and delivers parsed batches.

    `on_batch` and `on_stopped` are called from a worker thread. `on_stopped`
    fires only when adb ends by itself (device unplugged, adb killed), not
    after `stop()`.
    """

    def __init__(
        self,
        adb: Adb,
        serial: str,
        on_batch: Callable[[list[LogLine]], None],
        on_stopped: Callable[[str], None] | None = None,
        buffer: str = DEFAULT_BUFFER,
        tail: bool = True,
    ) -> None:
        self._adb = adb
        self._serial = serial
        self._on_batch = on_batch
        self._on_stopped = on_stopped
        self._args = build_args(buffer, tail)
        self._process: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._is_stopping = threading.Event()
        self._notes: list[str] = []

    @property
    def is_running(self) -> bool:
        return self._process is not None and not self._is_stopping.is_set()

    def start(self) -> None:
        """Launch adb. Raises AdbError if it cannot be started."""
        if self._process is not None:
            raise RuntimeError("Okuyucu zaten başlatıldı.")
        self._process = self._adb.popen(self._args, self._serial)
        self._threads = [
            threading.Thread(target=self._read_loop, name="logcat-read", daemon=True),
            threading.Thread(target=self._dispatch_loop, name="logcat-dispatch", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Terminate adb and wait for the workers. Safe to call repeatedly."""
        self._is_stopping.set()
        process = self._process
        if process is not None:
            self._terminate(process)
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current:
                thread.join(STOP_TIMEOUT_S)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            logger.warning("adb logcat did not exit, killing it")
            process.kill()
            process.wait(STOP_TIMEOUT_S)

    def _read_loop(self) -> None:
        process = self._process
        try:
            for raw in iter(process.stdout.readline, b""):
                if self._is_stopping.is_set():
                    break
                self._handle_raw(raw)
        except (OSError, ValueError):
            logger.info("logcat stream closed")
        finally:
            self._lines.put(_END)
            self._close_stream(process)

    def _handle_raw(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace")
        parsed = parse_logcat_line(text)
        if parsed is not None:
            self._lines.put(parsed)
            return
        note = text.strip()
        if note and not note.startswith(_BUFFER_HEADER) and len(self._notes) < MAX_EXIT_NOTES:
            self._notes.append(note)

    @staticmethod
    def _close_stream(process: subprocess.Popen) -> None:
        try:
            process.stdout.close()
        except (OSError, ValueError):
            logger.debug("logcat stream already closed")

    def _dispatch_loop(self) -> None:
        is_ended = False
        while not is_ended:
            first = self._lines.get()
            if first is _END:
                break
            batch, is_ended = self._collect_batch(first)
            self._deliver(batch)
        if not self._is_stopping.is_set():
            self._notify_stopped()

    def _collect_batch(self, first: LogLine) -> tuple[list[LogLine], bool]:
        batch = [first]
        deadline = time.monotonic() + BATCH_INTERVAL_S
        while len(batch) < MAX_BATCH_LINES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self._lines.get(timeout=remaining)
            except queue.Empty:
                break
            if item is _END:
                return batch, True
            batch.append(item)
        return batch, False

    def _deliver(self, batch: list[LogLine]) -> None:
        if self._is_stopping.is_set():
            return
        try:
            self._on_batch(batch)
        except Exception:  # a broken consumer must not kill the stream
            logger.exception("logcat batch callback failed")

    def _notify_stopped(self) -> None:
        if self._on_stopped is None:
            return
        reason = " ".join(self._notes) or "adb logcat sona erdi."
        try:
            self._on_stopped(reason)
        except Exception:
            logger.exception("logcat stop callback failed")
