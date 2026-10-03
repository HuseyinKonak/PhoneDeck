"""Wi-Fi, saved networks, mobile network and connectivity tests for one phone.

Everything here talks to Android's `cmd wifi`, `ip`, `dumpsys` and `ping`. Anything
typed by the user (SSIDs, passwords, hosts) goes through a validator and `shlex.quote`.
"""

from __future__ import annotations

import ipaddress
import re
import shlex
from dataclasses import dataclass, replace

from .adb import Adb, AdbError

WIFI_INTERFACE = "wlan0"
SECTION_MARK = "@@phonedeck-net@@"
PROBE_ADDRESS = "1.1.1.1"
MAX_SSID_BYTES = 32
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 63
MAX_HOST_LENGTH = 253
MAX_LABEL_LENGTH = 63
PING_COUNT = 4
PING_WAIT_S = 2
PING_TIMEOUT_S = 25
DNS_TIMEOUT_S = 10
INVALID_RSSI = -127
MAX_SIGNAL_BARS = 4
# RSSI (dBm) at or above which a network shows 4, 3, 2 and 1 bars.
BAR_THRESHOLDS = (-55, -66, -77, -88)
UNSUPPORTED_SECURITY = ""

SECURITY_OPEN = "open"
SECURITY_OWE = "owe"
SECURITY_WPA2 = "wpa2"
SECURITY_WPA3 = "wpa3"
CONNECT_TYPES = (SECURITY_OPEN, SECURITY_OWE, SECURITY_WPA2, SECURITY_WPA3)
PASSWORD_TYPES = (SECURITY_WPA2, SECURITY_WPA3)

_HOST_LABEL = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_MAC = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"
_CONNECTED = re.compile(r'^Wifi is connected to "(.*)"\s*$', re.MULTILINE)
_ENABLED = re.compile(r"^Wifi is (enabled|disabled)", re.MULTILINE)
_BSSID = re.compile(rf"BSSID: ({_MAC})")
_OWN_MAC = re.compile(rf"BSSID: {_MAC}, MAC: ({_MAC})")
_RSSI = re.compile(r", RSSI: (-?\d+)")
_LINK_SPEED = re.compile(r", Link speed: (\d+)Mbps")
_FREQUENCY = re.compile(r"Frequency: (\d+)MHz")
_INET = re.compile(r"^\s*inet (\d+\.\d+\.\d+\.\d+)/", re.MULTILINE)
_LINK_ETHER = re.compile(rf"link/ether ({_MAC})")
_VIA = re.compile(r"\bvia (\S+) dev (\S+)")
_DNS_ADDRESSES = re.compile(r"DnsAddresses: \[([^\]]*)\]")
_DNS_PROPERTY = re.compile(r"^\[[^\]]*\.dns\d\]: \[([^\]]*)\]", re.MULTILINE)
_SCAN_LINE = re.compile(
    rf"^\s*({_MAC})\s+(\d+)\s+(-?\d+)\(\S*\)\s+\S+\s+(.*?)\s*((?:\[[^\]]*\])*)\s*$"
)
_SAVED_LINE = re.compile(r"^\s*(\d+)\s+(.*?)\s+(\S+)\s*$")
_PING_COUNTS = re.compile(
    r"(\d+) packets transmitted, (\d+) (?:packets )?received.*?(\d+(?:\.\d+)?)% packet loss"
)
_PING_RTT = re.compile(r"= (\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")
_PING_TARGET = re.compile(r"^PING \S+ \(([^)]+)\)", re.MULTILINE)
_OPERATOR = re.compile(r"mOperatorAlphaLong=([^,}]*)")
_DATA_TECH = re.compile(r"getRilDataRadioTechnology=\d+\(([^)]*)\)")
_ROAMING_TYPE = re.compile(r"roamingType=([A-Z_]+)")
_PRIMARY_CELL = re.compile(r"primary=CellSignalStrength(\w+)")
_ANY_LEVEL = re.compile(r"level\s*=\s*(\d)")
_DATA_CONNECTION = re.compile(r"mDataConnectionState=(\d+)")

_NETWORK_TYPE_NAMES = {"NR": "5G", "LTE_CA": "LTE+", "UNKNOWN": ""}
_DATA_STATES = {0: "Bağlı değil", 1: "Bağlanıyor", 2: "Bağlı", 3: "Askıda"}
_FAILURE_WORDS = ("fail", "error", "exception", "invalid")


class WifiError(AdbError):
    """A Wi-Fi action failed; the message is safe to show (it never holds a password)."""


# --- data -------------------------------------------------------------------------
@dataclass(frozen=True)
class WifiStatus:
    is_enabled: bool = False
    is_connected: bool = False
    ssid: str = ""
    bssid: str = ""
    mac: str = ""
    frequency_mhz: int = 0
    link_speed_mbps: int = 0
    rssi: int | None = None
    ip_address: str = ""
    gateway: str = ""
    dns_servers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanResult:
    ssid: str
    bssid: str
    frequency_mhz: int
    rssi: int
    security: str
    connect_type: str


@dataclass(frozen=True)
class SavedNetwork:
    network_id: int
    ssid: str
    security: str
    connect_type: str


@dataclass(frozen=True)
class MobileLine:
    operator: str
    network_type: str
    signal_level: int
    data_state: str
    is_roaming: bool


@dataclass(frozen=True)
class PingResult:
    host: str
    resolved_address: str
    transmitted: int
    received: int
    loss_percent: float
    min_ms: float | None
    avg_ms: float | None
    max_ms: float | None


# --- validators -------------------------------------------------------------------
def validate_ssid(ssid: str) -> str:
    if not ssid or len(ssid.encode("utf-8")) > MAX_SSID_BYTES or _has_control_chars(ssid):
        raise ValueError("Geçersiz ağ adı.")
    return ssid


def validate_password(password: str, security: str) -> str:
    """Open networks take no password; WPA networks need 8 to 63 characters."""
    if security not in PASSWORD_TYPES:
        if password:
            raise ValueError("Bu ağ türü parola almaz.")
        return ""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH or _has_control_chars(password):
        raise ValueError(f"Parola {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} karakter olmalı.")
    return password


def validate_security(security: str) -> str:
    if security not in CONNECT_TYPES:
        raise ValueError("Desteklenmeyen güvenlik türü.")
    return security


def validate_network_id(network_id: int | str) -> int:
    text = str(network_id).strip()
    if not text.isascii() or not text.isdigit():
        raise ValueError("Geçersiz ağ numarası.")
    return int(text)


def validate_host(host: str) -> str:
    """A hostname or an IP address; nothing the shell or ping could read as an option."""
    cleaned = host.strip()
    if "%" in cleaned:
        raise ValueError("Geçersiz ana makine adı veya IP adresi.")
    if _is_ip_address(cleaned):
        return cleaned
    labels = cleaned.rstrip(".").split(".")
    is_valid = (
        0 < len(cleaned) <= MAX_HOST_LENGTH
        and all(len(label) <= MAX_LABEL_LENGTH and _HOST_LABEL.fullmatch(label) for label in labels)
    )
    if not is_valid:
        raise ValueError("Geçersiz ana makine adı veya IP adresi.")
    return cleaned


def _has_control_chars(text: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


def _is_ip_address(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


# --- signal helpers ---------------------------------------------------------------
def signal_bars(rssi: int | None) -> int:
    """0 to 4 bars for an RSSI in dBm."""
    if rssi is None:
        return 0
    return sum(1 for threshold in BAR_THRESHOLDS if rssi >= threshold)


def band_label(frequency_mhz: int) -> str:
    if 2400 <= frequency_mhz < 2500:
        return "2,4 GHz"
    if 5150 <= frequency_mhz < 5925:
        return "5 GHz"
    if 5925 <= frequency_mhz <= 7125:
        return "6 GHz"
    return ""


def security_from_flags(flags: str) -> tuple[str, str]:
    """Readable security label and the `connect-network` type ('' when unsupported)."""
    upper = flags.upper()
    if "EAP" in upper or "WEP" in upper:
        return ("WPA Kurumsal" if "EAP" in upper else "WEP"), UNSUPPORTED_SECURITY
    has_sae, has_psk = "SAE" in upper, "PSK" in upper
    if has_sae and has_psk:
        return "WPA2/WPA3", SECURITY_WPA2
    if has_sae:
        return "WPA3", SECURITY_WPA3
    if has_psk:
        return "WPA2", SECURITY_WPA2
    if "OWE" in upper:
        return "Gelişmiş açık", SECURITY_OWE
    return "Açık", SECURITY_OPEN


def _security_from_saved(kinds: str) -> tuple[str, str]:
    """Same mapping for `list-networks` names such as 'wpa2-psk' or 'wpa3-sae'."""
    return security_from_flags(kinds.replace("-", " ").replace("open", ""))


# --- parsers ----------------------------------------------------------------------
def parse_wifi_status(text: str) -> WifiStatus:
    enabled = _ENABLED.search(text)
    connected = _CONNECTED.search(text)
    if connected is None:
        return WifiStatus(is_enabled=bool(enabled and enabled.group(1) == "enabled"))
    info = text[connected.end():]
    rssi = _first_int(_RSSI, info)
    return WifiStatus(
        is_enabled=True,
        is_connected=True,
        ssid=connected.group(1),
        bssid=_first_group(_BSSID, info),
        mac=_first_group(_OWN_MAC, info),
        frequency_mhz=_first_int(_FREQUENCY, info) or 0,
        link_speed_mbps=_first_int(_LINK_SPEED, info) or 0,
        rssi=None if rssi is None or rssi <= INVALID_RSSI else rssi,
    )


def parse_ip_addr(text: str) -> tuple[str, str]:
    """(IPv4 address, MAC) of the Wi-Fi interface from `ip addr show`."""
    return _first_group(_INET, text), _first_group(_LINK_ETHER, text)


def parse_gateway(text: str) -> str:
    """Gateway from `ip route get`, only if the route really leaves over Wi-Fi."""
    match = _VIA.search(text)
    if match is None or match.group(2) != WIFI_INTERFACE:
        return ""
    return match.group(1) if _is_ip_address(match.group(1)) else ""


def parse_dns(dumpsys_text: str, props_text: str) -> tuple[str, ...]:
    """DNS servers from the Wi-Fi link properties, falling back to `net.dns*` props."""
    match = _DNS_ADDRESSES.search(dumpsys_text)
    raw = match.group(1) if match else " ".join(_DNS_PROPERTY.findall(props_text))
    candidates = (item.strip().lstrip("/") for item in re.split(r"[\s,]+", raw))
    return tuple(dict.fromkeys(item for item in candidates if _is_ip_address(item)))


def parse_scan_results(text: str) -> tuple[ScanResult, ...]:
    """Scan results, strongest first; one entry per network name and band."""
    best: dict[tuple[str, str], ScanResult] = {}
    for line in text.splitlines():
        match = _SCAN_LINE.match(line.rstrip("\r"))
        if match is None or not match.group(4):
            continue  # header, or a hidden network that cannot be joined by name
        security, connect_type = security_from_flags(match.group(5))
        result = ScanResult(
            ssid=match.group(4),
            bssid=match.group(1),
            frequency_mhz=int(match.group(2)),
            rssi=int(match.group(3)),
            security=security,
            connect_type=connect_type,
        )
        key = (result.ssid, band_label(result.frequency_mhz))
        if key not in best or result.rssi > best[key].rssi:
            best[key] = result
    return tuple(sorted(best.values(), key=lambda item: item.rssi, reverse=True))


def parse_saved_networks(text: str) -> tuple[SavedNetwork, ...]:
    """Saved networks; WPA2/WPA3 transition networks are listed twice and merged."""
    lines = [line.rstrip("\r") for line in text.splitlines()]
    kinds: dict[int, list[str]] = {}
    names: dict[int, str] = {}
    for line in lines:
        match = _SAVED_LINE.match(line)
        if match is None:
            continue
        network_id = int(match.group(1))
        names.setdefault(network_id, match.group(2))
        kinds.setdefault(network_id, []).append(match.group(3))
    networks = []
    for network_id, found in kinds.items():
        security, connect_type = _security_from_saved(" ".join(found))
        networks.append(SavedNetwork(network_id, names[network_id], security, connect_type))
    return tuple(networks)


def parse_mobile(text: str) -> tuple[MobileLine, ...]:
    """One entry per SIM from the `mServiceState`/`mSignalStrength` registry lines."""
    blocks: list[dict[str, str]] = []
    for line in text.splitlines():
        key, _, _ = line.strip().partition("=")
        if key == "mServiceState":
            blocks.append({"service": line})
        elif blocks and key in ("mSignalStrength", "mDataConnectionState"):
            blocks[-1].setdefault(key, line)
    return tuple(_mobile_line(block) for block in blocks)


def _mobile_line(block: dict[str, str]) -> MobileLine:
    service = block["service"]
    technology = _first_group(_DATA_TECH, service)
    if "nrState=CONNECTED" in service:
        technology = "NR"
    data = _first_int(_DATA_CONNECTION, block.get("mDataConnectionState", ""))
    roaming_types = set(_ROAMING_TYPE.findall(service))
    return MobileLine(
        operator=_first_group(_OPERATOR, service).strip(),
        network_type=_NETWORK_TYPE_NAMES.get(technology, technology),
        signal_level=_cell_level(block.get("mSignalStrength", "")),
        data_state=_DATA_STATES.get(data if data is not None else -1, "—"),
        is_roaming="mIsDataRoamingFromRegistration=true" in service
        or bool(roaming_types - {"NOT_ROAMING", "UNKNOWN"}),
    )


def _cell_level(signal_line: str) -> int:
    """Level (0-4) of the primary cell technology, else the best level reported."""
    primary = _PRIMARY_CELL.search(signal_line)
    if primary:
        cell = re.search(
            rf"CellSignalStrength{primary.group(1)}:\{{?[^{{}}]*?level\s*=\s*(\d)", signal_line
        )
        if cell:
            return min(int(cell.group(1)), MAX_SIGNAL_BARS)
    levels = [int(value) for value in _ANY_LEVEL.findall(signal_line)]
    return min(max(levels, default=0), MAX_SIGNAL_BARS)


def parse_ping(host: str, output: str) -> PingResult:
    """Statistics from `ping`; raises WifiError when the name or network is unreachable."""
    counts = _PING_COUNTS.search(output)
    if counts is None:
        lowered = output.lower()
        if "unknown host" in lowered or "not known" in lowered or "bad address" in lowered:
            raise WifiError(f"{host} adı çözümlenemedi.")
        if "unreachable" in lowered:
            raise WifiError("Ağa ulaşılamıyor.")
        raise WifiError("Ping sonucu okunamadı.")
    rtt = _PING_RTT.search(output)
    low, mean, high = (float(value) for value in rtt.groups()) if rtt else (None, None, None)
    return PingResult(
        host=host,
        resolved_address=_first_group(_PING_TARGET, output),
        transmitted=int(counts.group(1)),
        received=int(counts.group(2)),
        loss_percent=float(counts.group(3)),
        min_ms=low,
        avg_ms=mean,
        max_ms=high,
    )


def _first_group(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def _first_int(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    return int(match.group(1)) if match else None


# --- command builders -------------------------------------------------------------
def build_connect_command(ssid: str, security: str, password: str = "") -> str:
    """`cmd wifi connect-network` with every user value validated and quoted."""
    validate_ssid(ssid)
    validate_security(security)
    validate_password(password, security)
    parts = ["cmd", "wifi", "connect-network", shlex.quote(ssid), security]
    if security in PASSWORD_TYPES:
        parts.append(shlex.quote(password))
    return " ".join(parts)


def build_forget_command(network_id: int | str) -> str:
    return f"cmd wifi forget-network {validate_network_id(network_id)}"


def build_ping_command(host: str, count: int = PING_COUNT) -> str:
    # `exit 0` keeps a 100% loss run from looking like an adb failure.
    return f"ping -c {int(count)} -W {PING_WAIT_S} {shlex.quote(validate_host(host))} 2>&1; exit 0"


def build_status_command() -> str:
    """One round trip: Wi-Fi status, address, route, DNS link properties and DNS props."""
    commands = (
        "cmd wifi status",
        f"ip addr show {WIFI_INTERFACE}",
        f"ip route get {PROBE_ADDRESS} oif {WIFI_INTERFACE}",
        "dumpsys wifi | grep -m1 -o 'DnsAddresses: \\[[^]]*\\]'",
        "getprop | grep dns",
    )
    return f"; echo {SECTION_MARK}; ".join(f"{command} 2>/dev/null" for command in commands)


MOBILE_COMMAND = (
    "dumpsys telephony.registry | "
    "grep -E '^ *(mServiceState|mSignalStrength|mDataConnectionState)='"
)


# --- device calls -----------------------------------------------------------------
def read_status(adb: Adb, serial: str) -> WifiStatus:
    parts = adb.shell(serial, build_status_command()).split(SECTION_MARK)
    parts += [""] * (5 - len(parts))
    status = parse_wifi_status(parts[0])
    if not status.is_connected:
        return status
    address, hardware = parse_ip_addr(parts[1])
    return replace(
        status,
        mac=hardware or status.mac,
        ip_address=address,
        gateway=parse_gateway(parts[2]),
        dns_servers=parse_dns(parts[3], parts[4]),
    )


def read_mobile(adb: Adb, serial: str) -> tuple[MobileLine, ...]:
    return parse_mobile(adb.shell(serial, MOBILE_COMMAND))


def set_wifi_enabled(adb: Adb, serial: str, is_enabled: bool) -> None:
    adb.shell(serial, f"svc wifi {'enable' if is_enabled else 'disable'}")


def start_scan(adb: Adb, serial: str) -> None:
    adb.shell(serial, "cmd wifi start-scan")


def list_scan_results(adb: Adb, serial: str) -> tuple[ScanResult, ...]:
    return parse_scan_results(adb.shell(serial, "cmd wifi list-scan-results"))


def list_saved_networks(adb: Adb, serial: str) -> tuple[SavedNetwork, ...]:
    return parse_saved_networks(adb.shell(serial, "cmd wifi list-networks"))


def connect_network(adb: Adb, serial: str, ssid: str, security: str, password: str = "") -> None:
    """Join a network. Errors are rewritten so the command line (and password) never leak."""
    command = build_connect_command(ssid, security, password)
    try:
        output = adb.shell(serial, command)
    except AdbError as exc:
        raise WifiError(_safe_failure(str(exc), (password, shlex.quote(password)))) from None
    if any(word in output.lower() for word in _FAILURE_WORDS):
        raise WifiError("Telefon bağlantı isteğini reddetti.")


def forget_network(adb: Adb, serial: str, network_id: int | str) -> None:
    adb.shell(serial, build_forget_command(network_id))


def ping_host(adb: Adb, serial: str, host: str) -> PingResult:
    cleaned = validate_host(host)
    output = adb.shell(serial, build_ping_command(cleaned), PING_TIMEOUT_S)
    return parse_ping(cleaned, output)


def resolve_host(adb: Adb, serial: str, host: str) -> str:
    """Resolve a name through the phone's resolver (a single ping; there is no nslookup)."""
    cleaned = validate_host(host)
    output = adb.shell(serial, build_ping_command(cleaned, count=1), DNS_TIMEOUT_S)
    address = _first_group(_PING_TARGET, output)
    if not address:
        parse_ping(cleaned, output)  # raises the matching "unknown host" error
        raise WifiError(f"{cleaned} adı çözümlenemedi.")
    return address


def _safe_failure(message: str, secrets: tuple[str, ...]) -> str:
    if "zaman aşımı" in message:
        return "Telefon bağlantı isteğine yanıt vermedi."
    for secret in filter(None, secrets):
        message = message.replace(secret, "***")
    return f"Bağlanılamadı: {message[:120]}"
