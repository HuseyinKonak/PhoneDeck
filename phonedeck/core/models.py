"""Immutable data models shared by the core and the UI."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str = ""

    @property
    def is_ready(self) -> bool:
        return self.state == "device"

    @property
    def is_wireless(self) -> bool:
        return ":" in self.serial

    @property
    def title(self) -> str:
        name = self.model.replace("_", " ") or self.serial
        return f"{name} ({'Wi-Fi' if self.is_wireless else 'USB'})"


@dataclass(frozen=True)
class BatteryInfo:
    level: int = 0
    is_charging: bool = False
    temperature_c: float = 0.0


@dataclass(frozen=True)
class UsageInfo:
    """Used/total pair in bytes (storage or memory)."""

    total: int = 0
    used: int = 0

    @property
    def percent(self) -> int:
        return round(self.used * 100 / self.total) if self.total else 0


@dataclass(frozen=True)
class DeviceInfo:
    model: str = ""
    brand: str = ""
    android_version: str = ""
    sdk: str = ""
    resolution: str = ""
    ip_address: str = ""
    battery: BatteryInfo = BatteryInfo()
    storage: UsageInfo = UsageInfo()
    memory: UsageInfo = UsageInfo()


@dataclass(frozen=True)
class AppInfo:
    package: str
    label: str
    is_system: bool = False


@dataclass(frozen=True)
class Notification:
    package: str
    title: str
    text: str


@dataclass(frozen=True)
class CallEntry:
    number: str
    name: str
    kind: int
    timestamp_ms: int
    duration_s: int


@dataclass(frozen=True)
class SmsMessage:
    address: str
    body: str
    timestamp_ms: int
    is_incoming: bool
    is_read: bool


@dataclass(frozen=True)
class FileEntry:
    name: str
    is_dir: bool
    size: int
    modified: str
    permissions: str


@dataclass(frozen=True)
class VolumeInfo:
    current: int
    maximum: int


@dataclass(frozen=True)
class BrightnessInfo:
    """Raw `screen_brightness` setting and the range this phone accepts."""

    current: int = 128
    minimum: int = 1
    maximum: int = 255


@dataclass(frozen=True)
class ToggleState:
    wifi: bool = False
    bluetooth: bool = False
    mobile_data: bool = False
    auto_rotate: bool = False
    do_not_disturb: bool = False
    is_awake: bool = True
    is_playing: bool = False
    is_muted: bool = False
    volume: VolumeInfo = VolumeInfo(0, 15)
    brightness: BrightnessInfo = BrightnessInfo()
