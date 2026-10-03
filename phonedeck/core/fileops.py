"""File operations on the phone beyond listing: rename, copy, move, preview, usage."""

from __future__ import annotations

import posixpath
import shlex

from .adb import Adb, AdbError
from .device import TRANSFER_TIMEOUT, is_protected_path, normalize_path

TEXT_PREVIEW_BYTES = 200_000
EXISTS_MARKER = "@@exists@@"


def validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or cleaned in (".", "..") or "/" in cleaned or "\x00" in cleaned:
        raise ValueError(f"Geçersiz ad: {name}")
    return cleaned


def _movable(path: str) -> str:
    target = normalize_path(path)
    if is_protected_path(target):
        raise ValueError(f"Korunan yol değiştirilemez: {target}")
    return target


def rename(adb: Adb, serial: str, path: str, new_name: str) -> str:
    """Rename in place without overwriting. Returns the new path."""
    source = _movable(path)
    target = posixpath.join(posixpath.dirname(source), validate_name(new_name))
    quoted_source, quoted_target = shlex.quote(source), shlex.quote(target)
    output = adb.shell(
        serial,
        f"if [ -e {quoted_target} ]; then echo {EXISTS_MARKER}; "
        f"else mv -- {quoted_source} {quoted_target}; fi",
    )
    if EXISTS_MARKER in output:
        raise AdbError(f"Bu adda bir öğe zaten var: {new_name}")
    return target


def copy_into(adb: Adb, serial: str, sources: list[str], destination: str) -> None:
    _transfer(adb, serial, "cp -r", [normalize_path(source) for source in sources], destination)


def move_into(adb: Adb, serial: str, sources: list[str], destination: str) -> None:
    _transfer(adb, serial, "mv", [_movable(source) for source in sources], destination)


def _transfer(adb: Adb, serial: str, command: str, sources: list[str], destination: str) -> None:
    if not sources:
        return
    target = normalize_path(destination)
    for source in sources:
        if target == source or target.startswith(source.rstrip("/") + "/"):
            raise ValueError("Bir klasör kendi içine kopyalanamaz veya taşınamaz.")
    quoted = " ".join(shlex.quote(source) for source in sources)
    adb.shell(serial, f"{command} -- {quoted} {shlex.quote(target + '/')}", TRANSFER_TIMEOUT)


def read_text(adb: Adb, serial: str, path: str, limit: int = TEXT_PREVIEW_BYTES) -> str:
    """First `limit` bytes of a file, decoded leniently for preview."""
    data = adb.run_bytes(
        ["exec-out", f"head -c {int(limit)} -- {shlex.quote(normalize_path(path))}"], serial
    )
    return data.decode("utf-8", "replace")


def disk_usage(adb: Adb, serial: str, path: str) -> str:
    """Human-readable total size of a file or folder (`du -sh`)."""
    output = adb.shell(serial, f"du -sh -- {shlex.quote(normalize_path(path))}", timeout=120)
    return output.split()[0] if output.split() else "?"
