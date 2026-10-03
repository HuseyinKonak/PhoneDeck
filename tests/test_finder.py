import shlex
from collections.abc import Callable

import pytest

from phonedeck.core import finder
from phonedeck.core.adb import AdbError
from phonedeck.core.device import SEPARATOR
from phonedeck.core.finder import DeviceSnapshot, RingSession, StreamLevel

INJECTIONS = ("a; reboot", "a$(id)", "a`id`", "a|b", "a'b", "a\nb")
UPTIME_S = 190_000.0
ET_AGE_800 = "2d4h33m20s0ms"  # 189_200 s since boot -> age 800 s at UPTIME_S
SNAPSHOT = DeviceSnapshot(
    ringer_mode=finder.RINGER_VIBRATE,
    streams=(
        StreamLevel(2, 1, 16),
        StreamLevel(3, 104, 160),
        StreamLevel(4, 16, 16),
        StreamLevel(5, 0, 16),
    ),
    brightness=3319,
    brightness_mode=0,
    brightness_max=3319,
)


class FakeAdb:
    """Records shell commands; `reply` decides the output (or raises) for each one."""

    def __init__(self, reply: Callable[[str], str] | str = "") -> None:
        self._reply = reply if callable(reply) else (lambda _command: reply)
        self.shell_calls: list[str] = []
        self.run_calls: list[list[str]] = []

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.shell_calls.append(command)
        return self._reply(command)

    def run(self, args: list[str], serial: str | None = None, timeout: float = 30) -> str:
        self.run_calls.append(args)
        return ""


def joined(*sections: str) -> str:
    return f"\n{SEPARATOR}\n".join(sections)


def snapshot_sections(ringer: str = "1", brightness: str = "3319") -> list[str]:
    streams = [
        ("AudioManager.getStreamVolume(2) -> 1", "AudioManager.getStreamMaxVolume(2) -> 16"),
        ("AudioManager.getStreamVolume(3) -> 104", "AudioManager.getStreamMaxVolume(3) -> 160"),
        ("AudioManager.getStreamVolume(4) -> 16", "AudioManager.getStreamMaxVolume(4) -> 16"),
        ("AudioManager.getStreamVolume(5) -> 0", "AudioManager.getStreamMaxVolume(5) -> 16"),
    ]
    flat = [text for pair in streams for text in pair]
    return flat + [ringer, brightness, "0", "mScreenBrightnessNormalMaximum=3319.0", "mWakefulness=Awake"]


# --- validators ---------------------------------------------------------------
@pytest.mark.parametrize("seconds", finder.RING_DURATIONS_S)
def test_validate_duration_accepts_offered_choices(seconds):
    assert finder.validate_duration(seconds) == seconds


@pytest.mark.parametrize("seconds", [0, 5, 11, 3600, -30])
def test_validate_duration_rejects_other_values(seconds):
    with pytest.raises(ValueError):
        finder.validate_duration(seconds)


def test_validate_owner_message_joins_text_and_cleaned_number():
    assert finder.validate_owner_message("Lütfen ara:", "+90 (555) 123-45-67") == "Lütfen ara: +905551234567"


def test_validate_owner_message_accepts_text_only_and_number_only():
    assert finder.validate_owner_message("Bulan ara", "") == "Bulan ara"
    assert finder.validate_owner_message("", "0555") == "0555"


def test_validate_owner_message_replaces_control_characters():
    assert finder.validate_owner_message("a\nb\tc", "") == "a b c"


@pytest.mark.parametrize("phone", ["abc", "555; reboot", "$(id)", "5" * 30])
def test_validate_owner_message_rejects_bad_phone_numbers(phone):
    with pytest.raises(ValueError):
        finder.validate_owner_message("x", phone)


def test_validate_owner_message_rejects_empty_and_too_long_input():
    with pytest.raises(ValueError):
        finder.validate_owner_message("  ", " ")
    with pytest.raises(ValueError):
        finder.validate_owner_message("a" * (finder.OWNER_MESSAGE_MAX_CHARS + 1), "")


def test_validate_media_uri_accepts_media_store_items_only():
    uri = "content://media/internal/audio/media/677"
    assert finder.validate_media_uri(uri) == uri
    for bad in ("content://media/internal/audio/media/x", "file:///a.ogg", uri + "; id", ""):
        with pytest.raises(ValueError):
            finder.validate_media_uri(bad)


# --- parsers ------------------------------------------------------------------
def test_parse_stream_value_reads_the_number_after_the_arrow():
    assert finder.parse_stream_value("AudioManager.getStreamVolume(3) -> 104") == 104
    assert finder.parse_stream_value("garbage") is None


def test_parse_setting_int_treats_null_and_text_as_missing():
    assert finder.parse_setting_int("2\n") == 2
    assert finder.parse_setting_int("null") is None
    assert finder.parse_setting_int("") is None


def test_parse_brightness_max_falls_back_to_default():
    assert finder.parse_brightness_max("mScreenBrightnessNormalMaximum=3319.0") == 3319
    assert finder.parse_brightness_max("") == finder.DEFAULT_BRIGHTNESS_MAX


def test_parse_snapshot_reads_streams_ringer_and_brightness():
    snapshot = finder.parse_snapshot(snapshot_sections())
    assert snapshot == SNAPSHOT


def test_parse_snapshot_keeps_unreadable_brightness_as_none():
    snapshot = finder.parse_snapshot(snapshot_sections(brightness="null"))
    assert snapshot.brightness is None


def test_parse_snapshot_rejects_unknown_ringer_mode_and_missing_volumes():
    with pytest.raises(AdbError):
        finder.parse_snapshot(snapshot_sections(ringer="null"))
    broken = snapshot_sections()
    broken[0] = "error"
    with pytest.raises(AdbError):
        finder.parse_snapshot(broken)


def test_snapshot_commands_match_what_parse_snapshot_expects():
    assert len(finder.snapshot_commands()) == len(snapshot_sections())


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("2d4h33m20s0ms", 189_200.0), ("5s250ms", 5.25), ("1h", 3600.0), ("", 0.0)],
)
def test_parse_duration_seconds(text, seconds):
    assert finder.parse_duration_seconds(text) == pytest.approx(seconds)


def test_parse_uptime_takes_first_field():
    assert finder.parse_uptime("190046.82 642687.75\n") == pytest.approx(190046.82)
    assert finder.parse_uptime("") is None
    assert finder.parse_uptime("x y") is None


def test_parse_location_reads_modern_format_with_masked_coordinates():
    dump = f"last location=Location[fused 41.0123****,29.5678**** hAcc=65.0 et=+{ET_AGE_800} alt=1.0]"
    fix = finder.parse_location(dump, UPTIME_S)
    assert fix is not None
    assert (fix.provider, fix.latitude, fix.longitude) == ("fused", 41.0123, 29.5678)
    assert fix.accuracy_m == 65.0
    assert fix.age_s == 800


def test_parse_location_reads_legacy_format_without_age():
    fix = finder.parse_location("Location[gps -33.5,151.25 acc=12 alt=3]", UPTIME_S)
    assert fix is not None
    assert (fix.latitude, fix.longitude, fix.accuracy_m, fix.age_s) == (-33.5, 151.25, 12.0, None)


def test_parse_location_works_without_uptime():
    fix = finder.parse_location(f"Location[network 1.5,2.5 hAcc=9 et=+{ET_AGE_800}]", None)
    assert fix is not None and fix.age_s is None


def test_parse_location_picks_the_freshest_entry():
    dump = "\n".join(
        [
            "Location[network 10.0,10.0 hAcc=50 et=+1d]",
            f"Location[gps 20.0,20.0 hAcc=5 et=+{ET_AGE_800}]",
            "Location[fused 30.0,30.0 hAcc=5 et=+2d]",
        ]
    )
    fix = finder.parse_location(dump, UPTIME_S)
    assert fix is not None and fix.provider == "gps"


def test_parse_location_prefers_known_age_over_unknown():
    dump = f"Location[gps 1.0,1.0 acc=5]\nLocation[fused 2.0,2.0 hAcc=5 et=+{ET_AGE_800}]"
    fix = finder.parse_location(dump, UPTIME_S)
    assert fix is not None and fix.provider == "fused"


@pytest.mark.parametrize(
    "dump",
    ["", "no locations here", "Location[fused 95.0,10.0 hAcc=1]", "Location[fused 10.0,200.0 hAcc=1]"],
)
def test_parse_location_returns_none_when_nothing_usable(dump):
    assert finder.parse_location(dump, UPTIME_S) is None


def test_location_fix_builds_map_url_and_coordinate_text():
    fix = finder.LocationFix("gps", 41.5, 29.25)
    assert fix.coordinates_text == "41.500000, 29.250000"
    assert fix.maps_url.endswith("query=41.500000,29.250000")
    assert fix.maps_url.startswith("https://")


def test_parse_ssid_hides_unknown_names():
    assert finder.parse_ssid('Wifi is connected to "Home Net"') == "Home Net"
    assert finder.parse_ssid('Wifi is connected to "<unknown ssid>"') == ""
    assert finder.parse_ssid("Wifi is disconnected") == ""


def test_parse_is_locked_reads_keyguard_flag():
    assert finder.parse_is_locked("    isKeyguardShowing=true") is True
    assert finder.parse_is_locked("    isKeyguardShowing=false") is False
    assert finder.parse_is_locked("") is False


@pytest.mark.parametrize(
    ("serial", "transport"),
    [
        ("a1b2c3d4", finder.TRANSPORT_USB),
        ("192.168.1.7:5555", finder.TRANSPORT_WIFI),
        ("adb-XYZ-abc._adb-tls-connect._tcp", finder.TRANSPORT_WIFI),
    ],
)
def test_transport_of_serial(serial, transport):
    assert finder.transport_of(serial) == transport


def test_parse_owner_info_handles_unset_values():
    assert finder.parse_owner_info("null", "null") == finder.OwnerInfo("", False)
    assert finder.parse_owner_info("Ara beni", "1") == finder.OwnerInfo("Ara beni", True)


# --- command builders ---------------------------------------------------------
def test_loud_script_sets_normal_ringer_before_maximising_every_stream():
    commands = finder.build_loud_script(SNAPSHOT).split("; ")
    assert commands[0] == "cmd audio set-ringer-mode NORMAL"
    assert "cmd audio set-volume 3 160" in commands
    assert "cmd audio set-volume 5 16" in commands
    assert len(commands) == 1 + len(SNAPSHOT.streams)


def test_restore_script_returns_volumes_and_brightness_then_ringer_last():
    commands = finder.build_restore_script(SNAPSHOT).split("; ")
    assert commands[-1] == "cmd audio set-ringer-mode VIBRATE"
    assert "cmd audio set-volume 2 1" in commands
    assert "cmd audio set-volume 3 104" in commands
    assert "settings put system screen_brightness 3319" in commands
    assert commands.index("settings put system screen_brightness_mode 0") < commands.index(
        "settings put system screen_brightness 3319"
    )


def test_restore_script_skips_brightness_that_was_unreadable():
    snapshot = DeviceSnapshot(finder.RINGER_NORMAL, SNAPSHOT.streams)
    assert "screen_brightness" not in finder.build_restore_script(snapshot)


def test_flash_script_forces_manual_full_brightness():
    script = finder.build_flash_script(SNAPSHOT)
    assert "screen_brightness_mode 0" in script
    assert "screen_brightness 3319" in script


def test_playback_command_targets_component_and_quotes_arguments():
    uri = "content://media/internal/audio/media/5"
    command = finder.build_playback_command(finder.PLAYER_COMPONENTS[0], uri)
    parts = shlex.split(command)
    assert parts[:3] == ["am", "start", "-n"]
    assert parts[3] == finder.PLAYER_COMPONENTS[0]
    assert parts[-1] == uri
    assert "audio/*" in parts


def test_playback_command_rejects_unvalidated_uris():
    with pytest.raises(ValueError):
        finder.build_playback_command(finder.PLAYER_COMPONENTS[0], "content://x; reboot")


def test_stop_playback_force_stops_every_known_player():
    script = finder.build_stop_playback_script()
    assert script.count("am force-stop") == len(finder.PLAYER_COMPONENTS)
    assert "com.google.android.apps.youtube.music" in script


def test_vibrate_command_is_a_finite_pause_then_on_off_waveform():
    parts = finder.build_vibrate_command().split()
    assert parts[:6] == ["cmd", "vibrator_manager", "synced", "-f", "-B", "waveform"]
    timings = [int(value) for value in parts[6:]]
    assert timings[0] == 0
    assert sum(timings) == pytest.approx(finder.VIBRATE_TOTAL_MS, rel=0.05)
    assert timings[1:3] == [finder.VIBRATE_ON_MS, finder.VIBRATE_OFF_MS]


def test_watchdog_runs_detached_with_marker_delay_and_restore():
    command = finder.build_watchdog_command("cmd audio set-volume 3 104", 35)
    assert command.startswith("mkdir -p")
    assert "nohup setsid sh -c" in command
    assert finder.WATCHDOG_MARKER in command
    assert "sleep 35" in command
    assert "cmd audio set-volume 3 104" in command
    assert command.endswith(f"echo $! > {finder.WATCHDOG_PID_FILE}")


def test_cancel_watchdog_only_kills_a_process_carrying_the_marker():
    command = finder.build_cancel_watchdog_command()
    assert f"grep -q {finder.WATCHDOG_MARKER}" in command
    assert "kill" in command
    assert command.endswith(f"rm -f {finder.WATCHDOG_PID_FILE}")


def test_owner_info_script_quotes_the_message():
    for text in INJECTIONS:
        script = finder.build_owner_info_script(text)
        expected = (
            f"settings put secure lock_screen_owner_info {shlex.quote(text)}; "
            "settings put secure lock_screen_owner_info_enabled 1"
        )
        assert script == expected
        assert shlex.quote(text) != text


# --- device access ------------------------------------------------------------
def test_read_snapshot_runs_the_batch_and_parses_it():
    adb = FakeAdb(joined(*snapshot_sections()))
    assert finder.read_snapshot(adb, "serial") == SNAPSHOT


def test_find_ring_source_prefers_alarm_tone():
    adb = FakeAdb("Row: 0 _id=677")
    assert finder.find_ring_source(adb, "s") == "content://media/internal/audio/media/677"
    assert "is_alarm=1" in adb.shell_calls[0]
    assert len(adb.shell_calls) == 1


def test_find_ring_source_falls_back_to_ringtones_then_fails():
    adb = FakeAdb(lambda command: "Row: 0 _id=12" if "is_ringtone=1" in command else "No result found.")
    assert finder.find_ring_source(adb, "s").endswith("/12")
    with pytest.raises(AdbError):
        finder.find_ring_source(FakeAdb("No result found."), "s")


def test_play_sound_falls_back_to_the_next_player():
    uri = "content://media/internal/audio/media/5"

    def reply(command: str) -> str:
        return "Error type 3" if finder.PLAYER_COMPONENTS[0] in command else "Starting: Intent"

    session = finder.play_sound(FakeAdb(reply), "s", uri)
    assert session == RingSession(uri, finder.PLAYER_COMPONENTS[1])


def test_play_sound_fails_when_no_player_starts():
    with pytest.raises(AdbError):
        finder.play_sound(FakeAdb("Error: Activity class does not exist"), "s", "content://media/internal/audio/media/5")


def test_replay_sound_reports_start_errors():
    session = RingSession("content://media/internal/audio/media/5", finder.PLAYER_COMPONENTS[0])
    finder.replay_sound(FakeAdb("Starting: Intent"), "s", session)
    with pytest.raises(AdbError):
        finder.replay_sound(FakeAdb("Error type 3"), "s", session)


def test_start_ring_loosens_volumes_plays_and_arms_the_watchdog():
    adb = FakeAdb(lambda command: "Row: 0 _id=7" if command.startswith("content query") else "Starting: Intent")
    session = finder.start_ring(adb, "s", SNAPSHOT, 30)
    commands = adb.shell_calls
    assert session.uri.endswith("/7")
    assert any("set-ringer-mode NORMAL" in command for command in commands)
    assert any(command.startswith("am start") for command in commands)
    watchdog = commands[-1]
    assert f"sleep {30 + finder.WATCHDOG_GRACE_S}" in watchdog
    assert "set-ringer-mode VIBRATE" in watchdog


def test_start_ring_restores_settings_when_playback_fails():
    def reply(command: str) -> str:
        if command.startswith("content query"):
            return "Row: 0 _id=7"
        return "Error type 3" if command.startswith("am start") else ""

    adb = FakeAdb(reply)
    with pytest.raises(AdbError):
        finder.start_ring(adb, "s", SNAPSHOT, 10)
    assert "set-ringer-mode VIBRATE" in adb.shell_calls[-1]
    assert "set-volume 3 104" in adb.shell_calls[-1]


def test_start_ring_rejects_unlisted_duration_before_touching_the_phone():
    adb = FakeAdb()
    with pytest.raises(ValueError):
        finder.start_ring(adb, "s", SNAPSHOT, 99)
    assert adb.shell_calls == []


def test_restore_device_stops_player_disarms_watchdog_and_restores_in_one_call():
    adb = FakeAdb()
    finder.restore_device(adb, "s", SNAPSHOT)
    assert len(adb.shell_calls) == 1
    script = adb.shell_calls[0]
    assert script.index("am force-stop") < script.index("kill") < script.index("set-volume")
    assert script.endswith("cmd audio set-ringer-mode VIBRATE")


def test_restore_device_can_leave_music_apps_alone():
    adb = FakeAdb()
    finder.restore_device(adb, "s", SNAPSHOT, should_stop_player=False)
    assert "force-stop" not in adb.shell_calls[0]
    assert "set-ringer-mode VIBRATE" in adb.shell_calls[0]


def test_vibration_start_checks_errors_and_stop_cancels():
    adb = FakeAdb()
    finder.start_vibration(adb, "s")
    finder.stop_vibration(adb, "s")
    assert adb.shell_calls[0].startswith("cmd vibrator_manager synced")
    assert adb.shell_calls[1] == "cmd vibrator_manager cancel"
    with pytest.raises(AdbError):
        finder.start_vibration(FakeAdb("Unknown command: waveform"), "s")


def test_pulse_screen_sends_wakeup_and_sleep_keys():
    adb = FakeAdb()
    finder.pulse_screen(adb, "s", True)
    finder.pulse_screen(adb, "s", False)
    assert adb.shell_calls == [
        f"input keyevent {finder.KEYCODE_WAKEUP}",
        f"input keyevent {finder.KEYCODE_SLEEP}",
    ]


def test_lock_screen_sends_sleep_key():
    adb = FakeAdb()
    finder.lock_screen(adb, "s")
    assert adb.shell_calls == [f"input keyevent {finder.KEYCODE_SLEEP}"]


def test_read_location_combines_uptime_and_dump():
    dump = f"Location[fused 41.5,29.5 hAcc=20.0 et=+{ET_AGE_800}]"
    fix = finder.read_location(FakeAdb(joined(f"{UPTIME_S} 9.0", dump)), "s")
    assert fix is not None and fix.age_s == 800 and fix.accuracy_m == 20.0


def test_read_location_returns_none_when_the_dump_has_no_fix():
    assert finder.read_location(FakeAdb(joined("100.0 1.0", "")), "s") is None


def test_read_status_collects_battery_screen_lock_wifi_and_transport():
    battery = "  USB powered: true\n  level: 45\n  temperature: 300"
    output = joined(battery, "mWakefulness=Asleep", "isKeyguardShowing=true", 'Wifi is connected to "Net"')
    status = finder.read_status(FakeAdb(output), "192.168.1.7:5555")
    assert status == finder.PhoneStatus(
        battery_level=45,
        is_charging=True,
        ssid="Net",
        is_screen_on=False,
        is_locked=True,
        transport=finder.TRANSPORT_WIFI,
    )


def test_read_owner_info_parses_both_settings():
    info = finder.read_owner_info(FakeAdb(joined("Ara beni", "1")), "s")
    assert info == finder.OwnerInfo("Ara beni", True)


def test_set_owner_info_sends_quoted_text_and_returns_it():
    adb = FakeAdb()
    text = finder.set_owner_info(adb, "s", "Ara; reboot", "+90 555 000 00 00")
    assert text == "Ara; reboot +905550000000"
    assert f"lock_screen_owner_info {shlex.quote(text)}; " in adb.shell_calls[0]


def test_set_owner_info_rejects_bad_phone_without_calling_the_phone():
    adb = FakeAdb()
    with pytest.raises(ValueError):
        finder.set_owner_info(adb, "s", "x", "not a number")
    assert adb.shell_calls == []


def test_clear_owner_info_disables_and_deletes_the_message():
    adb = FakeAdb()
    finder.clear_owner_info(adb, "s")
    script = adb.shell_calls[0]
    assert "lock_screen_owner_info_enabled 0" in script
    assert "settings delete secure lock_screen_owner_info" in script


# --- flash and "everything" ---------------------------------------------------
def test_vibrate_command_length_follows_the_requested_time():
    short = finder.build_vibrate_command(10_000).split()[6:]
    long = finder.build_vibrate_command(60_000).split()[6:]

    assert sum(int(value) for value in short) <= 10_000 < sum(int(value) for value in long) <= 60_000


def test_torch_command_runs_the_helper_detached_and_reads_its_first_lines():
    command = finder.build_torch_command(30)

    assert f"phonedeck.PhoneTool torch 30 {finder.TORCH_ON_MS} {finder.TORCH_OFF_MS}" in command
    assert "nohup setsid sh -c" in command and command.endswith(f"cat {finder.TORCH_LOG}")


@pytest.mark.parametrize("seconds", [0, -5, finder.TORCH_MAX_S + 1])
def test_torch_rejects_out_of_range_durations_before_touching_the_phone(seconds):
    adb = FakeAdb()

    with pytest.raises(ValueError):
        finder.start_torch(adb, "s", seconds)
    assert adb.shell_calls == [] and adb.run_calls == []


def test_stop_torch_pattern_cannot_match_its_own_shell():
    command = finder.build_stop_torch_command()

    assert command.startswith("pkill -f ") and "PhoneTool torch" not in command


def test_start_torch_pushes_helper_stops_old_blinker_then_starts():
    adb = FakeAdb("OK torch camera=0")

    finder.start_torch(adb, "s", 10)

    assert adb.run_calls[0][0] == "push"
    assert adb.shell_calls[0] == finder.build_stop_torch_command()
    assert "PhoneTool torch 10" in adb.shell_calls[1]


def test_start_torch_reports_helper_errors():
    with pytest.raises(AdbError, match="no camera flash"):
        finder.start_torch(FakeAdb("ERROR IllegalStateException: no camera flash available"), "s", 10)


def test_blink_command_alternates_brightness_and_keeps_the_screen_awake():
    bright = finder.build_blink_command(SNAPSHOT, True)
    dim = finder.build_blink_command(SNAPSHOT, False)

    assert "screen_brightness 3319" in bright and f"screen_brightness {finder.BLINK_DIM_BRIGHTNESS};" in dim
    assert bright.endswith(f"input keyevent {finder.KEYCODE_WAKEUP}")


def everything_reply(command: str) -> str:
    return "Row: 0 _id=7" if command.startswith("content query") else "Starting: Intent"


def test_start_everything_rings_then_starts_screen_vibration_and_flash():
    adb = FakeAdb(everything_reply)

    session, skipped = finder.start_everything(adb, "s", SNAPSHOT, 10)

    commands = " || ".join(adb.shell_calls)
    assert session.uri.endswith("/7") and skipped == ()
    for expected in ("set-ringer-mode NORMAL", "screen_brightness 3319", "vibrator_manager synced", "PhoneTool torch 10"):
        assert expected in commands
    assert commands.index("set-ringer-mode NORMAL") < commands.index("vibrator_manager synced")


def test_start_everything_keeps_ringing_when_an_extra_effect_fails():
    def reply(command: str) -> str:
        if "PhoneTool torch" in command:
            return "ERROR IllegalStateException: no camera flash available"
        if "vibrator_manager synced" in command:
            return "Unknown command: synced"
        return everything_reply(command)

    session, skipped = finder.start_everything(FakeAdb(reply), "s", SNAPSHOT, 10)

    assert session is not None and skipped == ("titreşim", "flaş")


def test_stop_everything_restores_settings_before_stopping_vibration_and_flash():
    adb = FakeAdb()

    finder.stop_everything(adb, "s", SNAPSHOT)

    assert "set-ringer-mode VIBRATE" in adb.shell_calls[0] and "screen_brightness 3319" in adb.shell_calls[0]
    assert adb.shell_calls[1:] == ["cmd vibrator_manager cancel", finder.build_stop_torch_command()]
