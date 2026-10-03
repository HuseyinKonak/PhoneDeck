"""Detailed information and management actions for one installed app."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .adb import Adb, AdbError
from .device import SEPARATOR, TRANSFER_TIMEOUT, validate_package

# `enabled=` values in `dumpsys package` (PackageManager.COMPONENT_ENABLED_STATE_*).
_DISABLED_STATES = frozenset({"2", "3", "4"})
_PERMISSION_NAME = re.compile(r"[A-Za-z0-9_.]+")
_PERMISSION_LINE = re.compile(r"^\s+([A-Za-z0-9_.]+): granted=(true|false)", re.MULTILINE)
_UNSAFE_FILE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
_DISKSTATS_KEYS = ("Package Names", "App Sizes", "App Data Sizes", "Cache Sizes")


@dataclass(frozen=True)
class Permission:
    name: str
    is_granted: bool
    is_runtime: bool

    @property
    def short_name(self) -> str:
        return self.name.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class AppSizes:
    app: int = 0
    data: int = 0
    cache: int = 0

    @property
    def total(self) -> int:
        return self.app + self.data + self.cache


@dataclass(frozen=True)
class AppDetails:
    package: str
    version_name: str = ""
    version_code: str = ""
    min_sdk: str = ""
    target_sdk: str = ""
    first_install: str = ""
    last_update: str = ""
    installer: str = ""
    data_dir: str = ""
    is_enabled: bool = True
    is_system: bool = False
    apk_paths: tuple[str, ...] = ()
    permissions: tuple[Permission, ...] = ()
    sizes: AppSizes = AppSizes()


def validate_permission(name: str) -> str:
    if not _PERMISSION_NAME.fullmatch(name):
        raise ValueError(f"Geçersiz izin adı: {name}")
    return name


def _first(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""


def _section(text: str, start: str, end_pattern: str) -> str:
    """Text between a heading line and the next line matching `end_pattern`."""
    begin = text.find(start)
    if begin < 0:
        return ""
    body = text[begin + len(start):].lstrip("\r\n")
    end = re.search(end_pattern, body, re.MULTILINE)
    return body[: end.start()] if end else body


def parse_permissions(dump: str) -> tuple[Permission, ...]:
    """Install-time and runtime permissions with their granted state."""
    install = _section(dump, "install permissions:", r"^\s+User \d+:")
    runtime = _section(dump, "runtime permissions:", r"^\s*$|^\s+\w[\w ]*:\s*$")
    permissions = {
        name: Permission(name, granted == "true", is_runtime=False)
        for name, granted in _PERMISSION_LINE.findall(install)
    }
    for name, granted in _PERMISSION_LINE.findall(runtime):
        permissions[name] = Permission(name, granted == "true", is_runtime=True)
    return tuple(sorted(permissions.values(), key=lambda p: (not p.is_runtime, p.short_name)))


def parse_apk_paths(output: str) -> tuple[str, ...]:
    """Parse `pm path`: one `package:/path.apk` per line (base plus splits)."""
    paths = (line.partition(":")[2].strip() for line in output.splitlines())
    return tuple(path for path in paths if path.endswith(".apk"))


def parse_diskstats(output: str, package: str) -> AppSizes:
    """Find one package in the parallel arrays printed by `dumpsys diskstats`."""
    arrays = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in _DISKSTATS_KEYS:
            try:
                arrays[key.strip()] = json.loads(value)
            except json.JSONDecodeError:
                return AppSizes()
    names = arrays.get("Package Names", [])
    if package not in names:
        return AppSizes()
    index = names.index(package)

    def pick(key: str) -> int:
        values = arrays.get(key, [])
        return int(values[index]) if index < len(values) else 0

    return AppSizes(app=pick("App Sizes"), data=pick("App Data Sizes"), cache=pick("Cache Sizes"))


def parse_details(package: str, dump: str, paths: str, diskstats: str) -> AppDetails:
    flags = _first(r"pkgFlags=\[(.*?)\]", dump)
    return AppDetails(
        package=package,
        version_name=_first(r"versionName=(\S+)", dump),
        version_code=_first(r"versionCode=(\d+)", dump),
        min_sdk=_first(r"minSdk=(\d+)", dump),
        target_sdk=_first(r"targetSdk=(\d+)", dump),
        first_install=_first(r"firstInstallTime=(.+)", dump),
        last_update=_first(r"lastUpdateTime=(.+)", dump),
        installer=_first(r"installerPackageName=(\S+)", dump),
        data_dir=_first(r"dataDir=(\S+)", dump),
        is_enabled=_first(r"User 0:.*? enabled=(\d)", dump) not in _DISABLED_STATES,
        is_system="SYSTEM" in flags.split(),
        apk_paths=parse_apk_paths(paths),
        permissions=parse_permissions(dump),
        sizes=parse_diskstats(diskstats, package),
    )


def read_details(adb: Adb, serial: str, package: str) -> AppDetails:
    validate_package(package)
    script = (
        f"dumpsys package {package}; echo {SEPARATOR}; pm path {package}; echo {SEPARATOR}; "
        "dumpsys diskstats | grep -E 'Package Names|App Sizes|App Data Sizes|Cache Sizes'"
    )
    parts = adb.shell(serial, script, timeout=60).split(SEPARATOR)
    if len(parts) != 3:
        raise AdbError("Uygulama bilgisi okunamadı.")
    return parse_details(package, *parts)


def set_permission(adb: Adb, serial: str, package: str, permission: str, is_granted: bool) -> None:
    action = "grant" if is_granted else "revoke"
    adb.shell(serial, f"pm {action} {validate_package(package)} {validate_permission(permission)}")


def set_enabled(adb: Adb, serial: str, package: str, is_enabled: bool) -> None:
    validate_package(package)
    command = f"pm enable {package}" if is_enabled else f"pm disable-user --user 0 {package}"
    adb.shell(serial, command)


def clear_data(adb: Adb, serial: str, package: str) -> None:
    adb.shell(serial, f"pm clear {validate_package(package)}")


def open_settings(adb: Adb, serial: str, package: str) -> None:
    adb.shell(
        serial,
        "am start -a android.settings.APPLICATION_DETAILS_SETTINGS "
        f"-d package:{validate_package(package)}",
    )


def extract_apks(adb: Adb, serial: str, details: AppDetails, local_dir: str) -> tuple[Path, ...]:
    """Copy the app's APK files (base and splits) to this computer."""
    if not details.apk_paths:
        raise AdbError("APK yolu bulunamadı.")
    target_dir = Path(local_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for remote in details.apk_paths:
        version = _UNSAFE_FILE_CHARS.sub("_", details.version_name) or "apk"
        target = target_dir / f"{details.package}-{version}-{Path(remote).name}"
        adb.run(["pull", remote, str(target)], serial, TRANSFER_TIMEOUT)
        saved.append(target)
    return tuple(saved)
