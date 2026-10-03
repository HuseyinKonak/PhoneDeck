import shlex

import pytest

from phonedeck.core import wifi
from phonedeck.core.adb import AdbError
from phonedeck.core.wifi import SECTION_MARK, WifiError

NASTY_TEXTS = ("a b", "it's", 'say "hi"', "a;reboot", "a$(id)", "a`id`", "a|b&c", "a\\b", "ç ğ ü ş")

STATUS_TEXT = """Wifi is enabled
Wifi scanning is always available
==== Primary ClientModeManager instance ====
Wifi is connected to "Home, \\"Net\\""
WifiInfo: SSID: "Home", BSSID: aa:bb:cc:00:11:22, MAC: 02:11:22:33:44:55, IP: /192.168.1.7, Security type: 2, Supplicant state: COMPLETED, Wi-Fi standard: 11n, RSSI: -72, Link speed: 72Mbps, Tx Link speed: 60Mbps, Max Supported Tx Link speed: 72Mbps, Frequency: 2442MHz, Net ID: 69, Metered hint: false
successfulTxPackets: 17569
"""

SCAN_TEXT = (
    "    BSSID              Frequency      RSSI           Age(sec)     SSID                                 Flags\n"
    "  aa:bb:cc:00:00:01       2437    -45(0:-45)          498,477    Cafe Wifi                         [WPA2-PSK-CCMP-128][RSN-PSK-CCMP-128][ESS][WPS]\n"
    "  aa:bb:cc:00:00:02       5180    -89(0:-89)          844,458    Cafe Wifi                         [RSN-PSK-CCMP-128][ESS]\n"
    "  aa:bb:cc:00:00:03       2437    -60(0:-60)          498,498    Cafe Wifi                         [RSN-PSK-CCMP-128][ESS]\n"
    "  aa:bb:cc:00:00:04       5240    -50(0:-50)          497,231    Guest                             [ESS]\n"
    "  aa:bb:cc:00:00:05       2412    -70(0:-70)          496,552    [Odd] name [x]                    [RSN-SAE-CCMP-128][ESS]\n"
    "  aa:bb:cc:00:00:06       2412    -75(0:-75)          496,552                                      [ESS]\n"
    "  aa:bb:cc:00:00:07       2412    -65(0:-65)          496,552    Office                            [RSN-EAP/SHA1-CCMP-128][ESS]\n"
)

SAVED_TEXT = (
    "Network Id      SSID                         Security type\r\n"
    "0               Home                         wpa2-psk\r\n"
    "0               Home                         wpa3-sae\r\n"
    "1               Cafe  Wifi                   open\r\n"
    "2               Beacon                       owe\r\n"
    "10              02 FR 707                    wpa3-sae\r\n"
)

REGISTRY_TEXT = """    mCallState=0
    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE), mOperatorAlphaLong=Operator One, mOperatorAlphaShort=One, getRilDataRadioTechnology=14(LTE), mIsDataRoamingFromRegistration=false, mNetworkRegistrationInfos=[NetworkRegistrationInfo{ domain=PS transportType=WLAN roamingType=NOT_ROAMING accessNetworkTechnology=IWLAN nrState=**** }]}
    mSignalStrength=SignalStrength:{mCdma=CellSignalStrengthCdma: cdmaDbm=2147483647 level=0,mGsm=CellSignalStrengthGsm: rssi=2147483647 mLevel=0,mLte=CellSignalStrengthLte: rssi=-75 rsrp=-110 level=2 parametersUseForLevel=0,mNr=CellSignalStrengthNr:{ csiRsrp = 2147483647 level = 0 parametersUseForLevel = 0 },primary=CellSignalStrengthLte}
    mDataConnectionState=2
    mCallState=0
    mServiceState={mVoiceRegState=0(IN_SERVICE), mDataRegState=0(IN_SERVICE), mOperatorAlphaLong=Operator Two, getRilDataRadioTechnology=20(NR), mIsDataRoamingFromRegistration=true, nrState=CONNECTED}
    mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=-71 level=3 parametersUseForLevel=0,mNr=CellSignalStrengthNr:{ csiRsrp = -90 level = 4 parametersUseForLevel = 0 },primary=CellSignalStrengthNr}
    mDataConnectionState=0
"""

PING_OK = """PING example.com (93.184.216.34) 56(84) bytes of data.
64 bytes from 93.184.216.34: icmp_seq=1 ttl=56 time=12.4 ms

--- example.com ping statistics ---
4 packets transmitted, 3 received, 25% packet loss, time 3004ms
rtt min/avg/max/mdev = 11.250/12.500/14.125/1.125 ms
"""


class FakeAdb:
    """Records shell calls; replies with canned output or raises."""

    def __init__(self, output: str = "", error: AdbError | None = None) -> None:
        self.output = output
        self.error = error
        self.calls: list[tuple[str, float | None]] = []

    def shell(self, serial: str, command: str, timeout: float | None = None) -> str:
        self.calls.append((command, timeout))
        if self.error is not None:
            raise self.error
        return self.output


# --- validators -------------------------------------------------------------------
@pytest.mark.parametrize("ssid", ("Home", "a b", "ç" * 16, "x" * 32) + NASTY_TEXTS)
def test_validate_ssid_accepts_any_printable_text(ssid):
    assert wifi.validate_ssid(ssid) == ssid


@pytest.mark.parametrize("ssid", ("", "x" * 33, "ç" * 17, "a\nb", "a\x00b", "a\rb"))
def test_validate_ssid_rejects_empty_long_and_control_characters(ssid):
    with pytest.raises(ValueError):
        wifi.validate_ssid(ssid)


def test_validate_password_enforces_wpa_length():
    assert wifi.validate_password("12345678", "wpa2") == "12345678"
    assert wifi.validate_password("x" * 63, "wpa3") == "x" * 63
    for bad in ("1234567", "x" * 64, ""):
        with pytest.raises(ValueError):
            wifi.validate_password(bad, "wpa2")


def test_validate_password_rejects_control_characters():
    with pytest.raises(ValueError):
        wifi.validate_password("pass\nword1", "wpa2")


def test_validate_password_for_open_network_must_be_empty():
    assert wifi.validate_password("", "open") == ""
    with pytest.raises(ValueError):
        wifi.validate_password("secret123", "owe")


def test_validate_security_rejects_unknown_types():
    assert wifi.validate_security("wpa3") == "wpa3"
    for bad in ("wep", "wpa2; reboot", "", "WPA2"):
        with pytest.raises(ValueError):
            wifi.validate_security(bad)


@pytest.mark.parametrize("value", (0, 7, "42", " 3 "))
def test_validate_network_id_accepts_non_negative_integers(value):
    assert wifi.validate_network_id(value) == int(value)


@pytest.mark.parametrize("value", (-1, "-1", "1;reboot", "", "1 2", "٣", "1.5", "0x1", True))
def test_validate_network_id_rejects_everything_else(value):
    with pytest.raises(ValueError):
        wifi.validate_network_id(value)


@pytest.mark.parametrize(
    "host", ("example.com", "a-b.example.co.uk", "localhost", "8.8.8.8", "2001:4860:4860::8888", "example.com.", " x.org ")
)
def test_validate_host_accepts_names_and_addresses(host):
    assert wifi.validate_host(host) == host.strip()


@pytest.mark.parametrize(
    "host",
    ("", "-c1", "a b", "a;id", "$(id)", "a|b", "a`id`", "-a.com", "a-.com", "a..b", "fe80::1%eth0", "ex_ample.com", "a" * 64 + ".com", "ü.com", "a\nb", "."),
)
def test_validate_host_rejects_options_and_metacharacters(host):
    with pytest.raises(ValueError):
        wifi.validate_host(host)


# --- helpers ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("rssi", "bars"),
    ((None, 0), (-30, 4), (-55, 4), (-56, 3), (-66, 3), (-70, 2), (-77, 2), (-85, 1), (-88, 1), (-89, 0), (-100, 0)),
)
def test_signal_bars_maps_rssi_to_zero_through_four(rssi, bars):
    assert wifi.signal_bars(rssi) == bars


@pytest.mark.parametrize(
    ("frequency", "band"), ((2412, "2,4 GHz"), (5180, "5 GHz"), (5955, "6 GHz"), (900, ""))
)
def test_band_label_names_the_band(frequency, band):
    assert wifi.band_label(frequency) == band


@pytest.mark.parametrize(
    ("flags", "label", "connect"),
    (
        ("[WPA2-PSK-CCMP-128][RSN-PSK-CCMP-128][ESS]", "WPA2", "wpa2"),
        ("[RSN-PSK-CCMP-128][RSN-SAE-CCMP-128][ESS]", "WPA2/WPA3", "wpa2"),
        ("[RSN-SAE-CCMP-128][ESS]", "WPA3", "wpa3"),
        ("[RSN-OWE-CCMP-128][ESS]", "Gelişmiş açık", "owe"),
        ("[ESS]", "Açık", "open"),
        ("", "Açık", "open"),
        ("[RSN-EAP/SHA1-CCMP-128][ESS]", "WPA Kurumsal", ""),
        ("[WEP][ESS]", "WEP", ""),
    ),
)
def test_security_from_flags_classifies_networks(flags, label, connect):
    assert wifi.security_from_flags(flags) == (label, connect)


# --- parsers ----------------------------------------------------------------------
def test_parse_wifi_status_reads_connected_network():
    status = wifi.parse_wifi_status(STATUS_TEXT)

    assert status.is_enabled and status.is_connected
    assert status.ssid == 'Home, \\"Net\\"'
    assert status.bssid == "aa:bb:cc:00:11:22"
    assert status.mac == "02:11:22:33:44:55"
    assert (status.rssi, status.frequency_mhz, status.link_speed_mbps) == (-72, 2442, 72)


def test_parse_wifi_status_when_enabled_but_not_connected():
    status = wifi.parse_wifi_status("Wifi is enabled\nWifi is not connected\n")

    assert status.is_enabled and not status.is_connected and status.ssid == ""


def test_parse_wifi_status_when_disabled():
    assert not wifi.parse_wifi_status("Wifi is disabled\n").is_enabled


def test_parse_wifi_status_ignores_unavailable_rssi():
    text = STATUS_TEXT.replace("RSSI: -72", "RSSI: -127")

    assert wifi.parse_wifi_status(text).rssi is None


def test_parse_wifi_status_survives_garbage():
    assert wifi.parse_wifi_status("") == wifi.WifiStatus()


def test_parse_ip_addr_reads_address_and_mac():
    text = (
        "37: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n"
        "    link/ether 02:11:22:33:44:55 brd ff:ff:ff:ff:ff:ff\n"
        "    inet 192.168.1.7/24 brd 192.168.1.255 scope global dynamic wlan0\n"
        "    inet6 fe80::1/64 scope link\n"
    )

    assert wifi.parse_ip_addr(text) == ("192.168.1.7", "02:11:22:33:44:55")
    assert wifi.parse_ip_addr("") == ("", "")


def test_parse_gateway_requires_the_wifi_interface():
    assert wifi.parse_gateway("1.1.1.1 via 192.168.1.1 dev wlan0 table 1037 src 192.168.1.7") == "192.168.1.1"
    assert wifi.parse_gateway("1.1.1.1 via 10.0.0.1 dev rmnet_data4 src 10.0.0.2") == ""
    assert wifi.parse_gateway("1.1.1.1 via nonsense dev wlan0") == ""
    assert wifi.parse_gateway("") == ""


def test_parse_dns_prefers_link_properties_and_dedupes():
    dumpsys = "DnsAddresses: [ /192.168.1.1, /8.8.8.8, /192.168.1.1 ]"

    assert wifi.parse_dns(dumpsys, "[net.dns1]: [9.9.9.9]") == ("192.168.1.1", "8.8.8.8")


def test_parse_dns_falls_back_to_properties():
    props = "[net.dns1]: [9.9.9.9]\n[net.dns2]: [1.1.1.1]\n[persist.sys.dns_customized_timeout]: [1]\n"

    assert wifi.parse_dns("", props) == ("9.9.9.9", "1.1.1.1")
    assert wifi.parse_dns("", "") == ()


def test_parse_scan_results_sorts_by_signal_and_keeps_best_per_band():
    results = wifi.parse_scan_results(SCAN_TEXT)

    assert [item.rssi for item in results] == sorted((item.rssi for item in results), reverse=True)
    cafe = [item for item in results if item.ssid == "Cafe Wifi"]
    assert sorted(item.rssi for item in cafe) == [-89, -45]


def test_parse_scan_results_handles_spaces_brackets_hidden_and_enterprise():
    by_name = {item.ssid: item for item in wifi.parse_scan_results(SCAN_TEXT)}

    assert "[Odd] name [x]" in by_name and by_name["[Odd] name [x]"].connect_type == "wpa3"
    assert by_name["Guest"].security == "Açık" and by_name["Guest"].connect_type == "open"
    assert by_name["Office"].connect_type == ""
    assert "" not in by_name
    assert len(by_name) == 4


def test_parse_scan_results_of_empty_output_is_empty():
    assert wifi.parse_scan_results("") == ()
    assert wifi.parse_scan_results("Scan results are not available\n") == ()


def test_parse_saved_networks_merges_transition_networks():
    networks = wifi.parse_saved_networks(SAVED_TEXT)

    assert [item.network_id for item in networks] == [0, 1, 2, 10]
    assert networks[0].security == "WPA2/WPA3" and networks[0].connect_type == "wpa2"
    assert networks[1].ssid == "Cafe  Wifi" and networks[1].connect_type == "open"
    assert networks[2].connect_type == "owe"
    assert networks[3].ssid == "02 FR 707" and networks[3].connect_type == "wpa3"


def test_parse_saved_networks_of_header_only_is_empty():
    assert wifi.parse_saved_networks("Network Id      SSID      Security type\r\n") == ()
    assert wifi.parse_saved_networks("") == ()


def test_parse_mobile_returns_one_line_per_sim():
    lines = wifi.parse_mobile(REGISTRY_TEXT)

    assert len(lines) == 2
    assert lines[0] == wifi.MobileLine("Operator One", "LTE", 2, "Bağlı", False)
    assert lines[1] == wifi.MobileLine("Operator Two", "5G", 4, "Bağlı değil", True)


def test_parse_mobile_of_empty_output_is_empty():
    assert wifi.parse_mobile("") == ()


def test_parse_ping_reads_loss_and_round_trip_times():
    result = wifi.parse_ping("example.com", PING_OK)

    assert (result.transmitted, result.received, result.loss_percent) == (4, 3, 25.0)
    assert (result.min_ms, result.avg_ms, result.max_ms) == (11.25, 12.5, 14.125)
    assert result.resolved_address == "93.184.216.34"


def test_parse_ping_total_loss_has_no_round_trip_times():
    output = "PING 10.0.0.9 (10.0.0.9) 56(84) bytes of data.\n4 packets transmitted, 0 received, 100% packet loss, time 3070ms\n"

    result = wifi.parse_ping("10.0.0.9", output)

    assert result.loss_percent == 100.0 and result.avg_ms is None


@pytest.mark.parametrize(
    ("output", "fragment"),
    (
        ("ping: unknown host nope.invalid", "çözümlenemedi"),
        ("ping: nope.invalid: Name or service not known", "çözümlenemedi"),
        ("connect: Network is unreachable", "ulaşılamıyor"),
        ("", "okunamadı"),
    ),
)
def test_parse_ping_reports_failures_in_turkish(output, fragment):
    with pytest.raises(WifiError, match=fragment):
        wifi.parse_ping("nope.invalid", output)


# --- command builders -------------------------------------------------------------
@pytest.mark.parametrize("text", NASTY_TEXTS)
def test_build_connect_command_quotes_ssid_and_password(text):
    password = f"{text}12345678"

    command = wifi.build_connect_command(text, "wpa2", password)

    assert shlex.split(command) == ["cmd", "wifi", "connect-network", text, "wpa2", password]


def test_build_connect_command_for_open_network_has_no_password():
    command = wifi.build_connect_command("Guest net", "open")

    assert shlex.split(command) == ["cmd", "wifi", "connect-network", "Guest net", "open"]


def test_build_connect_command_rejects_bad_input():
    with pytest.raises(ValueError):
        wifi.build_connect_command("", "open")
    with pytest.raises(ValueError):
        wifi.build_connect_command("Home", "wpa2", "short")
    with pytest.raises(ValueError):
        wifi.build_connect_command("Home", "wep", "12345678")


def test_build_forget_command_uses_a_validated_integer():
    assert wifi.build_forget_command("12") == "cmd wifi forget-network 12"
    with pytest.raises(ValueError):
        wifi.build_forget_command("1; reboot")


def test_build_ping_command_quotes_host_and_never_fails_the_shell():
    command = wifi.build_ping_command("example.com")

    assert command == "ping -c 4 -W 2 example.com 2>&1; exit 0"
    with pytest.raises(ValueError):
        wifi.build_ping_command("a;reboot")


def test_build_status_command_has_one_section_per_source():
    command = wifi.build_status_command()

    assert command.count(f"echo {SECTION_MARK}") == 4
    assert "cmd wifi status" in command and "getprop | grep dns" in command


# --- device calls -----------------------------------------------------------------
def test_read_status_merges_all_sections():
    sections = (
        STATUS_TEXT,
        "    link/ether 02:aa:bb:cc:dd:ee brd ff:ff:ff:ff:ff:ff\n    inet 192.168.1.7/24 brd x scope global wlan0\n",
        "1.1.1.1 via 192.168.1.1 dev wlan0 table 1037 src 192.168.1.7",
        "DnsAddresses: [ /192.168.1.1 ]",
        "",
    )
    adb = FakeAdb(SECTION_MARK.join(sections))

    status = wifi.read_status(adb, "s")

    assert status.ip_address == "192.168.1.7" and status.gateway == "192.168.1.1"
    assert status.dns_servers == ("192.168.1.1",) and status.mac == "02:aa:bb:cc:dd:ee"


def test_read_status_skips_details_when_disconnected():
    adb = FakeAdb(f"Wifi is enabled\n{SECTION_MARK}")

    status = wifi.read_status(adb, "s")

    assert status.is_enabled and status.ip_address == "" and status.dns_servers == ()


@pytest.mark.parametrize(("is_on", "command"), ((True, "svc wifi enable"), (False, "svc wifi disable")))
def test_set_wifi_enabled_sends_svc_command(is_on, command):
    adb = FakeAdb()

    wifi.set_wifi_enabled(adb, "s", is_on)

    assert adb.calls == [(command, None)]


def test_connect_network_sends_quoted_command():
    adb = FakeAdb("Connection initiated")

    wifi.connect_network(adb, "s", "My Net", "wpa3", "pa ss'word1")

    assert shlex.split(adb.calls[0][0])[-3:] == ["My Net", "wpa3", "pa ss'word1"]


def test_connect_network_failure_output_raises_without_echoing_input():
    adb = FakeAdb("Error: invalid configuration")

    with pytest.raises(WifiError) as raised:
        wifi.connect_network(adb, "s", "Home", "wpa2", "hunter2hunter2")

    assert "hunter2" not in str(raised.value)


def test_connect_network_adb_error_hides_the_password():
    secret = "hunter2 hunter2"
    error = AdbError(f"cmd wifi connect-network Home wpa2 {shlex.quote(secret)} failed: {secret}")
    adb = FakeAdb(error=error)

    with pytest.raises(WifiError) as raised:
        wifi.connect_network(adb, "s", "Home", "wpa2", secret)

    assert "hunter2" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_connect_network_timeout_message_has_no_command_line():
    adb = FakeAdb(error=AdbError("adb zaman aşımı: shell cmd wifi connect-network Home"))

    with pytest.raises(WifiError) as raised:
        wifi.connect_network(adb, "s", "Home", "wpa2", "hunter2hunter2")

    assert "connect-network" not in str(raised.value)


def test_connect_network_validates_before_touching_the_phone():
    adb = FakeAdb()

    with pytest.raises(ValueError):
        wifi.connect_network(adb, "s", "Home", "wpa2", "short")

    assert adb.calls == []


def test_forget_network_sends_integer_id():
    adb = FakeAdb()

    wifi.forget_network(adb, "s", 7)

    assert adb.calls == [("cmd wifi forget-network 7", None)]


def test_ping_host_returns_parsed_result_with_long_timeout():
    adb = FakeAdb(PING_OK)

    result = wifi.ping_host(adb, "s", " example.com ")

    assert result.received == 3
    assert adb.calls == [("ping -c 4 -W 2 example.com 2>&1; exit 0", wifi.PING_TIMEOUT_S)]


def test_ping_host_rejects_injection_without_calling_the_phone():
    adb = FakeAdb()

    with pytest.raises(ValueError):
        wifi.ping_host(adb, "s", "x; reboot")

    assert adb.calls == []


def test_resolve_host_returns_the_resolved_address():
    adb = FakeAdb("PING example.com (93.184.216.34) 56(84) bytes of data.\n")

    assert wifi.resolve_host(adb, "s", "example.com") == "93.184.216.34"
    assert adb.calls[0][0].startswith("ping -c 1 ")


def test_resolve_host_reports_unknown_names():
    adb = FakeAdb("ping: unknown host nope.invalid")

    with pytest.raises(WifiError, match="çözümlenemedi"):
        wifi.resolve_host(adb, "s", "nope.invalid")


def test_scan_and_list_calls_use_cmd_wifi():
    adb = FakeAdb(SCAN_TEXT)

    wifi.start_scan(adb, "s")
    found = wifi.list_scan_results(adb, "s")

    assert [call[0] for call in adb.calls] == ["cmd wifi start-scan", "cmd wifi list-scan-results"]
    assert found
