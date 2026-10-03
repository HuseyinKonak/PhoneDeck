"""A persistent shell on the phone: state (cd, variables) survives between commands."""

from __future__ import annotations

import codecs
import logging
import threading
from typing import Callable

from .adb import Adb

logger = logging.getLogger(__name__)

READ_CHUNK_BYTES = 4096
STOP_WAIT_SECONDS = 2

OutputCallback = Callable[[str], None]
ExitCallback = Callable[[], None]


class ShellSession:
    """One long-running `adb shell`. Callbacks fire on a background thread."""

    def __init__(self, adb: Adb, serial: str, on_output: OutputCallback, on_exit: ExitCallback) -> None:
        self._adb = adb
        self._serial = serial
        self._on_output = on_output
        self._on_exit = on_exit
        self._process = None
        self._write_lock = threading.Lock()
        self._is_stopping = False

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        self._process = self._adb.popen(["shell"], self._serial, with_stdin=True)
        threading.Thread(target=self._read_loop, name="phone-shell", daemon=True).start()

    def send(self, command: str) -> bool:
        """Run a command line. Returns False when the shell is gone."""
        if not self.is_running:
            return False
        try:
            with self._write_lock:
                self._process.stdin.write(command.encode("utf-8") + b"\n")
                self._process.stdin.flush()
            return True
        except OSError as exc:
            logger.warning("shell write failed: %s", exc)
            return False

    def stop(self) -> None:
        """End the shell (also the only way to interrupt a running command)."""
        self._is_stopping = True
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(STOP_WAIT_SECONDS)
        except Exception:  # noqa: BLE001 - subprocess.TimeoutExpired; force the kill
            process.kill()

    def _read_loop(self) -> None:
        # Incremental decoding keeps multi-byte characters intact across chunks.
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        process = self._process
        try:
            while True:
                chunk = process.stdout.read1(READ_CHUNK_BYTES)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    self._on_output(text.replace("\r\n", "\n"))
        except (OSError, ValueError) as exc:
            logger.debug("shell read ended: %s", exc)
        if not self._is_stopping:
            self._on_exit()
