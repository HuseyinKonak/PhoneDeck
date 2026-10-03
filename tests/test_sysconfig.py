import pytest

from phonedeck.core import sysconfig
from phonedeck.core.adb import AdbError
from phonedeck.core.device import SEPARATOR
from phonedeck.core.sysconfig import Toggle

INJECTIONS = ("a; reboot", "a b", "a$(id)", "a`id`", "a|b", "a\nb", "a'b", "a&b", "a>b")
BATTERY_DUMP = """Current Battery Service state:
  AC powered: false
  Max charging voltage: 5000000
  status: 3
  health: 2
  present: true
  level: 64
  scale: 100
  voltage: 3906
  temperature: 305
  technology: Li-ion
  Charging state: 0
"""


class FakeAdb:
    """Records shell calls; replies with canned output."""

    def __init__(self, shell_output: str = "") -> None:
        self.shell_output = shell_output
        self.shell_calls: list[str] = []

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.shell_calls.append(command)
        return self.shell_output


def joined(*sections: str) -> str:
    return f"\n{SEPARATOR}\n".join(sections)


def blank_sections(toggles: tuple[Toggle, ...], *extra: str) -> str:
    return joined(*([""] * len(toggles)), *extra)


# --- toggle definitions -------------------------------------------------------
def test_toggle_keys_are_unique():
    keys = [toggle.key for toggle in sysconfig.ALL_TOGGLES]
    assert len(keys) == len(set(keys))


def test_setting_toggle_builds_put_commands_for_plain_values():
    toggle = sysconfig.setting_toggle("global", "data_roaming", "Dolaşım", False)
    assert toggle.on_command == "settings put global data_roaming 1"
    assert toggle.off_command == "settings put global data_roaming 0"
    assert toggle.read == "settings get global data_roaming"


def test_inverted_toggle_writes_the_opposite_value():
    toggle = sysconfig.validate_toggle("accelerometer_rotation")
    assert toggle.on_command.endswith("accelerometer_rotation 0")
    assert toggle.off_command.endswith("accelerometer_rotation 1")


def test_validate_toggle_rejects_unknown_key_and_injection():
    for bad in ("", "nope", "airplane; reboot", "data_roaming "):
        with pytest.raises(ValueError):
            sysconfig.validate_toggle(bad)


def test_set_toggle_sends_on_and_off_commands():
    adb = FakeAdb()
    sysconfig.set_toggle(adb, "s", "airplane", True)
    sysconfig.set_toggle(adb, "s", "airplane", False)
    assert adb.shell_calls == [
        "cmd connectivity airplane-mode enable",
        "cmd connectivity airplane-mode disable",
    ]


def test_set_toggle_raises_on_unknown_key_without_touching_the_phone():
    adb = FakeAdb()
    with pytest.raises(ValueError):
        sysconfig.set_toggle(adb, "s", "x; reboot", True)
    assert adb.shell_calls == []


@pytest.mark.parametrize("output", ["Error: no such service", "Exception occurred", "Unknown command: x", "Permission denied"])
def test_set_toggle_turns_shell_error_text_into_adb_error(output):
    with pytest.raises(AdbError):
        sysconfig.set_toggle(FakeAdb(output), "s", "nfc", True)


# --- toggle parsing -----------------------------------------------------------
@pytest.mark.parametrize(
    "output, default, expected",
    [
        ("1", None, True),
        ("0\n", None, False),
        ("enabled", None, True),
        ("disabled", None, False),
        ("mState=on", None, True),
        ("mState=off", None, False),
        ("null", None, None),
        ("null", True, True),
        ("", None, None),
        ("", False, False),
        ("garbage", None, None),
        ("2", False, False),
    ],
)
def test_parse_on_handles_words_unset_and_garbage(output, default, expected):
    assert sysconfig.parse_on(output, default) is expected


def test_parse_on_uses_custom_words_for_time_format():
    assert sysconfig.parse_on("24", False, "24", "12") is True
    assert sysconfig.parse_on("12", True, "24", "12") is False


def test_parse_toggle_flips_inverted_switches_and_keeps_unsupported_as_none():
    lock = sysconfig.validate_toggle("accelerometer_rotation")
    assert sysconfig.parse_toggle(lock, "1") is False
    assert sysconfig.parse_toggle(lock, "0") is True
    always_on = sysconfig.validate_toggle("doze_always_on")
    assert sysconfig.parse_toggle(always_on, "null") is None


# --- validators ---------------------------------------------------------------
@pytest.mark.parametrize("host", ["dns.google", "one.one.one.one", "a-b.example.com", "x1.y2.co", " dns.google "])
def test_validate_hostname_accepts_valid_names(host):
    assert sysconfig.validate_hostname(host) == host.strip()


@pytest.mark.parametrize(
    "host",
    ["", "localhost", "-a.com", "a-.com", "a..com", ".com", "a.com.", "1.1.1.1", "dns_google.com", "ü.com", "a.com/x", "a" * 64 + ".com", ("a." * 130) + "com"]
    + list(INJECTIONS),
)
def test_validate_hostname_rejects_bad_names_and_injection(host):
    with pytest.raises(ValueError):
        sysconfig.validate_hostname(host)


def test_validate_enumerations_reject_unknown_values():
    assert sysconfig.validate_dns_mode("hostname") == "hostname"
    assert sysconfig.validate_ringer("SILENT") == "SILENT"
    for bad in ("", "Off", "off; reboot", "automatic"):
        with pytest.raises(ValueError):
            sysconfig.validate_dns_mode(bad)
    for bad in ("", "normal", "SILENT; reboot", "LOUD"):
        with pytest.raises(ValueError):
            sysconfig.validate_ringer(bad)


def test_validate_stream_maps_known_keys_and_rejects_others():
    assert sysconfig.validate_stream("music").stream_id == 3
    for bad in ("3", "", "music; id", "Music"):
        with pytest.raises(ValueError):
            sysconfig.validate_stream(bad)


def test_validate_volume_enforces_phone_range():
    assert sysconfig.validate_volume("7", 0, 15) == 7
    for bad in ("-1", "16", "x", "5; id", "", "1.5"):
        with pytest.raises(ValueError):
            sysconfig.validate_volume(bad, 0, 15)


def test_validate_rotation_allows_zero_to_three_only():
    assert [sysconfig.validate_rotation(n) for n in range(4)] == [0, 1, 2, 3]
    for bad in ("4", "-1", "a", "1; id"):
        with pytest.raises(ValueError):
            sysconfig.validate_rotation(bad)


def test_validate_refresh_accepts_supported_rates_and_zero_minimum():
    assert sysconfig.validate_refresh("peak", 90, (60, 90)) == ("peak_refresh_rate", 90)
    assert sysconfig.validate_refresh("min", "0", (60, 90)) == ("min_refresh_rate", 0)


def test_validate_refresh_rejects_unsupported_rates_and_unknown_limits():
    with pytest.raises(ValueError):
        sysconfig.validate_refresh("peak", 0, (60, 90))
    with pytest.raises(ValueError):
        sysconfig.validate_refresh("peak", 75, (60, 90))
    with pytest.raises(ValueError):
        sysconfig.validate_refresh("peak", 120, ())
    with pytest.raises(ValueError):
        sysconfig.validate_refresh("peak; id", 60, (60,))


# --- parsers ------------------------------------------------------------------
def test_parse_stream_reads_current_min_max_in_order():
    text = "AudioManager.getStreamVolume(3) -> 104\nAudioManager.getStreamMinVolume(3) -> 0\nAudioManager.getStreamMaxVolume(3) -> 160"
    assert sysconfig.parse_stream("music", text) == sysconfig.StreamVolume("music", 0, 160, 104)


def test_parse_stream_clamps_current_and_rejects_incomplete_output():
    text = "x -> 20\nx -> 1\nx -> 9"
    assert sysconfig.parse_stream("call", text).current == 9
    assert sysconfig.parse_stream("call", "x -> 1") is None
    assert sysconfig.parse_stream("call", "") is None
    assert sysconfig.parse_stream("call", "x -> 1\nx -> 9\nx -> 2") is None


def test_parse_ringer_reads_external_mode():
    assert sysconfig.parse_ringer("- mode (external) = VIBRATE") == "VIBRATE"
    assert sysconfig.parse_ringer("") == ""


def test_parse_refresh_rates_rounds_sorts_and_dedupes():
    assert sysconfig.parse_refresh_rates("supportedRefreshRates [90.0, 60.000004, 90.0]") == (60, 90)
    assert sysconfig.parse_refresh_rates("nothing here") == ()
    assert sysconfig.parse_refresh_rates("supportedRefreshRates []") == ()


def test_parse_rate_ignores_unset_and_non_finite_values():
    assert sysconfig.parse_rate("90.0") == 90
    assert sysconfig.parse_rate("null") is None
    assert sysconfig.parse_rate("Infinity") is None
    assert sysconfig.parse_rate("nan") is None


def test_parse_hotspot_and_usb_tethering():
    assert sysconfig.parse_hotspot("12: swlan0    inet 192.168.43.1/24 scope global") is True
    assert sysconfig.parse_hotspot("37: wlan0    inet 192.168.1.5/24 scope global") is False
    assert sysconfig.parse_usb_tethering("rndis,adb") is True
    assert sysconfig.parse_usb_tethering("adb") is False


def test_first_value_skips_empty_and_null():
    assert sysconfig.first_value("", "null", " tr-TR ", "en-US") == "tr-TR"
    assert sysconfig.first_value("", "null") == ""


def test_parse_battery_reads_facts_and_ignores_similar_lines():
    state = sysconfig.parse_battery({}, BATTERY_DUMP)
    assert (state.health, state.status, state.technology) == ("İyi", "Şarj olmuyor (deşarj)", "Li-ion")
    assert (state.level, state.voltage_mv, state.temperature_c) == (64, 3906, 30.5)
    assert state.cycles is None
    assert state.is_simulated is False


def test_parse_battery_detects_cycle_count_and_simulation():
    text = BATTERY_DUMP + "  Cycle count: 212\n  (UPDATES STOPPED -- use 'reset' to restart)\n"
    state = sysconfig.parse_battery({}, text)
    assert state.cycles == 212
    assert state.is_simulated is True


def test_parse_battery_handles_empty_output():
    state = sysconfig.parse_battery({}, "")
    assert (state.health, state.level, state.temperature_c, state.cycles) == ("", None, None, None)


# --- command builders ---------------------------------------------------------
def test_volume_command_uses_stream_id_and_validated_index():
    assert sysconfig.volume_command("ring", 5, 0, 16) == "cmd audio set-volume 2 5"


def test_volume_command_rejects_out_of_range_and_injection():
    with pytest.raises(ValueError):
        sysconfig.volume_command("ring", 17, 0, 16)
    with pytest.raises(ValueError):
        sysconfig.volume_command("ring", "5; reboot", 0, 16)
    with pytest.raises(ValueError):
        sysconfig.volume_command("2; reboot", 5, 0, 16)


def test_dns_command_for_off_and_automatic_sets_only_the_mode():
    assert sysconfig.dns_command("off") == "settings put global private_dns_mode off"
    assert sysconfig.dns_command("opportunistic", "ignored; reboot") == "settings put global private_dns_mode opportunistic"


def test_dns_command_stores_hostname_before_switching_mode():
    command = sysconfig.dns_command("hostname", "dns.google")
    assert command == (
        "settings put global private_dns_specifier dns.google; "
        "settings put global private_dns_mode hostname"
    )


@pytest.mark.parametrize("host", INJECTIONS + ("", "1.1.1.1"))
def test_dns_command_rejects_bad_hostnames(host):
    with pytest.raises(ValueError):
        sysconfig.dns_command("hostname", host)


def test_dns_command_rejects_unknown_mode():
    with pytest.raises(ValueError):
        sysconfig.dns_command("hostname; reboot", "dns.google")


def test_battery_command_builds_every_simulation_action():
    assert sysconfig.battery_command("level", 15) == "dumpsys battery set level 15"
    assert sysconfig.battery_command("status", "5") == "dumpsys battery set ac 1; dumpsys battery set status 5"
    assert sysconfig.battery_command("status", 3) == "dumpsys battery unplug; dumpsys battery set status 3"
    assert sysconfig.battery_command("unplug") == "dumpsys battery unplug"
    assert sysconfig.battery_command("reset") == "dumpsys battery reset"


@pytest.mark.parametrize(
    "action, value",
    [("level", 101), ("level", -1), ("level", "50; reboot"), ("level", None), ("level", ""), ("status", 0), ("status", 6), ("status", "x"), ("reset; reboot", None), ("set", 1), ("", None)],
)
def test_battery_command_rejects_bad_input(action, value):
    with pytest.raises(ValueError):
        sysconfig.battery_command(action, value)


def test_every_write_function_validates_before_calling_the_phone():
    adb = FakeAdb()
    bad_calls = (
        lambda: sysconfig.set_private_dns(adb, "s", "hostname", "a;b"),
        lambda: sysconfig.set_ringer_mode(adb, "s", "LOUD"),
        lambda: sysconfig.set_rotation(adb, "s", 9),
        lambda: sysconfig.set_stream_volume(adb, "s", "music", 999, 0, 15),
        lambda: sysconfig.set_refresh_limit(adb, "s", "peak", 144, (60, 90)),
        lambda: sysconfig.simulate_battery(adb, "s", "level", 500),
    )
    for call in bad_calls:
        with pytest.raises(ValueError):
            call()
    assert adb.shell_calls == []


def test_write_functions_send_the_expected_commands():
    adb = FakeAdb()
    sysconfig.set_ringer_mode(adb, "s", "VIBRATE")
    sysconfig.set_rotation(adb, "s", 3)
    sysconfig.set_stream_volume(adb, "s", "alarm", 4, 1, 16)
    sysconfig.set_refresh_limit(adb, "s", "min", 60, (60, 90))
    sysconfig.set_private_dns(adb, "s", "hostname", "dns.google")
    sysconfig.simulate_battery(adb, "s", "reset")
    assert adb.shell_calls == [
        "cmd audio set-ringer-mode VIBRATE",
        "settings put system user_rotation 3",
        "cmd audio set-volume 4 4",
        "settings put system min_refresh_rate 60",
        "settings put global private_dns_specifier dns.google; settings put global private_dns_mode hostname",
        "dumpsys battery reset",
    ]


# --- readers ------------------------------------------------------------------
def test_read_connectivity_marks_unreadable_commands_as_unsupported():
    extra = ("null", "null", "Phone", "rndis,adb", "37: wlan0    inet 10.0.0.2/24 scope global")
    adb = FakeAdb(blank_sections(sysconfig.CONNECTIVITY_TOGGLES, *extra))
    state = sysconfig.read_connectivity(adb, "s")
    assert state.toggles["airplane"] is None
    assert state.toggles["data_roaming"] is False
    assert (state.dns_mode, state.dns_host, state.bluetooth_name) == ("opportunistic", "", "Phone")
    assert state.usb_tethering is True
    assert state.hotspot is False


def test_read_connectivity_reads_state_from_command_output():
    toggles = ["enabled", "true", "mState=on", "1"]
    adb = FakeAdb(joined(*toggles, "hostname", "dns.google", "null", "adb", ""))
    state = sysconfig.read_connectivity(adb, "s")
    assert state.toggles == {"airplane": True, "location": True, "nfc": True, "data_roaming": True}
    assert (state.dns_mode, state.dns_host) == ("hostname", "dns.google")


def test_read_sound_parses_streams_and_skips_unreadable_ones():
    music = "x -> 10\nx -> 0\nx -> 15"
    sections = [""] * len(sysconfig.SOUND_TOGGLES) + [music, "", "", "", ""] + ["- mode (external) = SILENT"]
    state = sysconfig.read_sound(FakeAdb(joined(*sections)), "s")
    assert state.streams == (sysconfig.StreamVolume("music", 0, 15, 10),)
    assert state.ringer == "SILENT"
    assert state.toggles["sound_effects_enabled"] is True


def test_read_screen_reads_rates_navigation_and_rotation():
    extra = ("2", "supportedRefreshRates [90.0, 60.0]", "90.0", "null", "2")
    state = sysconfig.read_screen(FakeAdb(blank_sections(sysconfig.SCREEN_TOGGLES, *extra)), "s")
    assert (state.rotation, state.rates, state.peak_rate, state.min_rate) == (2, (60, 90), 90, None)
    assert state.navigation == "Hareketle gezinme"
    assert state.toggles["doze_always_on"] is None


def test_read_battery_combines_toggles_and_dump():
    sections = ["0", "null", BATTERY_DUMP]
    state = sysconfig.read_battery(FakeAdb(joined(*sections)), "s")
    assert state.toggles == {"low_power": False, "adaptive_battery_management_enabled": None}
    assert state.level == 64


def test_read_date_prefers_first_non_empty_locale():
    extra = ("Europe/Istanbul", "", "null", "tr-TR")
    state = sysconfig.read_date(FakeAdb(blank_sections(sysconfig.DATE_TOGGLES, *extra)), "s")
    assert (state.timezone, state.locale) == ("Europe/Istanbul", "tr-TR")
    assert state.toggles["time_12_24"] is False


def test_read_privacy_reads_debugging_facts():
    extra = ("1", "0", "1", "1")
    state = sysconfig.read_privacy(FakeAdb(blank_sections(sysconfig.PRIVACY_TOGGLES, *extra)), "s")
    assert (state.usb_debugging, state.wireless_debugging, state.developer_options, state.adb_secure) == (True, False, True, True)
    assert state.toggles["verifier_verify_adb_installs"] is None
    assert state.toggles["show_password"] is True


def test_readers_raise_when_the_phone_returns_a_wrong_section_count():
    with pytest.raises(AdbError):
        sysconfig.read_battery(FakeAdb("only one section"), "s")
