"""Thin wrapper around the adb executable."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .models import Device
from .parsers import parse_devices

DEFAULT_TIMEOUT = 30
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class AdbError(RuntimeError):
    """adb is missing, timed out, or returned a failure."""


def find_adb() -> str:
    # An explicit search path: by default Windows also looks in the current folder,
    # where a stray adb.exe (for example inside a downloaded project) would be run.
    found = shutil.which("adb", path=os.environ.get("PATH", ""))
    if found and Path(found).resolve().parent != Path.cwd().resolve():
        return found
    candidates = [
        Path(os.environ.get(var, "")) / "platform-tools" / "adb.exe"
        for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT")
        if os.environ.get(var)
    ]
    candidates.append(
        Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise AdbError("adb bulunamadı. Android platform-tools kurup PATH'e ekleyin.")


class Adb:
    def __init__(self, path: str | None = None) -> None:
        self._path = path or find_adb()

    def _command(self, args: list[str], serial: str | None) -> list[str]:
        prefix = [self._path, "-s", serial] if serial else [self._path]
        return [*prefix, *args]

    def run_bytes(
        self, args: list[str], serial: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> bytes:
        try:
            result = subprocess.run(
                self._command(args, serial),
                capture_output=True,
                timeout=timeout,
                creationflags=_NO_WINDOW,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            # Only the adb subcommand: a shell command can carry message text or passwords.
            raise AdbError(f"adb zaman aşımı ({args[0] if args else 'adb'})") from exc
        except OSError as exc:
            raise AdbError(f"adb çalıştırılamadı: {exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
            raise AdbError(message or f"adb hata kodu {result.returncode}")
        return result.stdout

    def run(
        self, args: list[str], serial: str | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> str:
        return self.run_bytes(args, serial, timeout).decode("utf-8", "replace")

    def shell(self, serial: str, command: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        return self.run(["shell", command], serial, timeout)

    def popen(self, args: list[str], serial: str, with_stdin: bool = False) -> subprocess.Popen:
        """Start a long-running adb process with merged, piped output."""
        try:
            return subprocess.Popen(
                self._command(args, serial),
                stdin=subprocess.PIPE if with_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            raise AdbError(f"adb çalıştırılamadı: {exc}") from exc

    def devices(self) -> tuple[Device, ...]:
        return parse_devices(self.run(["devices", "-l"], timeout=10))

    def connect(self, address: str) -> str:
        return self.run(["connect", address], timeout=15).strip()

    def disconnect(self, address: str) -> str:
        return self.run(["disconnect", address], timeout=15).strip()
