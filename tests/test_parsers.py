from phonedeck.core import parsers
from phonedeck.core.models import Device, UsageInfo

DEVICES_OUTPUT = """List of devices attached
a1b2c3d4               device product:CPH2631TR model:CPH2631 device:OP5AE7L1 transport_id:25
192.168.1.7:5555       unauthorized transport_id:16
* daemon started successfully

"""

BATTERY_OUTPUT = """Current Battery Service state:
  AC powered: false
  USB powered: true
  level: 21
  temperature: 380
"""

LS_OUTPUT = """total 120
drwxrws--- 2 u0_a1 media_rw 3452 2024-01-01 08:02 .
drwxrws--- 2 u0_a1 media_rw 3452 2024-01-01 08:02 ..
-rw-rw---- 1 u0_a1 media_rw 2048 2025-03-04 10:11 my photo.jpg
drwxrws--- 2 u0_a1 media_rw 3452 2024-01-01 08:02 Download
lrwxrwxrwx 1 root root 21 2024-01-01 00:00 sdcard -> /storage/self/primary
garbage line
"""


def test_parse_devices_reads_serial_state_and_model():
    devices = parsers.parse_devices(DEVICES_OUTPUT)

    assert devices == (
        Device(serial="a1b2c3d4", state="device", model="CPH2631"),
        Device(serial="192.168.1.7:5555", state="unauthorized", model=""),
    )


def test_device_title_marks_connection_type():
    usb, wifi = parsers.parse_devices(DEVICES_OUTPUT)

    assert usb.title == "CPH2631 (USB)" and usb.is_ready
    assert wifi.title == "192.168.1.7:5555 (Wi-Fi)" and not wifi.is_ready


def test_parse_devices_returns_empty_when_no_devices():
    assert parsers.parse_devices("List of devices attached\n\n") == ()


def test_parse_battery_reads_level_charging_and_temperature():
    battery = parsers.parse_battery(BATTERY_OUTPUT)

    assert (battery.level, battery.is_charging, battery.temperature_c) == (21, True, 38.0)


def test_parse_battery_tolerates_garbage():
    battery = parsers.parse_battery("level: abc\nnonsense")

    assert (battery.level, battery.is_charging) == (0, False)


def test_parse_df_converts_kilobytes_to_bytes():
    output = "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/dm-1 1000 250 750 25% /data\n"

    usage = parsers.parse_df(output)

    assert usage == UsageInfo(total=1024000, used=256000)
    assert usage.percent == 25


def test_parse_df_returns_empty_usage_for_unexpected_output():
    assert parsers.parse_df("df: /data: Permission denied") == UsageInfo()
    assert UsageInfo().percent == 0


def test_parse_meminfo_computes_used_memory():
    usage = parsers.parse_meminfo("MemTotal: 8000 kB\nMemFree: 100 kB\nMemAvailable: 3000 kB\n")

    assert usage == UsageInfo(total=8000 * 1024, used=5000 * 1024)


def test_parse_packages_strips_prefix_and_sorts():
    assert parsers.parse_packages("package:org.b\npackage:com.a\n\n") == ("com.a", "org.b")


def test_parse_ls_sorts_directories_first_and_skips_dot_entries():
    entries = parsers.parse_ls(LS_OUTPUT)

    assert [e.name for e in entries] == ["Download", "sdcard", "my photo.jpg"]
    assert [e.is_dir for e in entries] == [True, True, False]
    assert entries[2].size == 2048 and entries[2].modified == "2025-03-04 10:11"


def test_parse_wm_size_prefers_override():
    assert parsers.parse_wm_size("Physical size: 720x1604\nOverride size: 1080x2400") == "1080x2400"
    assert parsers.parse_wm_size("") == ""


def test_parse_ip_extracts_address():
    assert parsers.parse_ip("    inet 192.168.1.7/24 brd 192.168.1.255 scope global wlan0") == "192.168.1.7"
    assert parsers.parse_ip("Device not found") == ""


def test_parse_volume_reads_current_and_maximum():
    volume = parsers.parse_volume("volume is 53 in range [0..160]")

    assert (volume.current, volume.maximum) == (53, 160)
    assert parsers.parse_volume("error").maximum == 15


def test_parse_app_list_reads_labels_and_sorts_by_label():
    output = (
        "icon failed for x: java.lang.RuntimeException\n"
        "APP\tcom.whatsapp\t0\tWhatsApp\r\n"
        "APP\tcom.android.settings\t1\tAyarlar\n"
        "APP\tcom.no.label\t0\t\n"
        "APP\tbroken line\n"
    )

    apps = parsers.parse_app_list(output)

    assert [(a.package, a.label, a.is_system) for a in apps] == [
        ("com.android.settings", "Ayarlar", True),
        ("com.no.label", "com.no.label", False),
        ("com.whatsapp", "WhatsApp", False),
    ]


def test_parse_app_list_keeps_tabs_out_of_fields_and_handles_empty_output():
    assert parsers.parse_app_list("") == ()
    assert parsers.parse_app_list("APP\t\t0\tNameless") == ()


def test_parse_notifications_groups_title_and_text_per_record():
    output = (
        "    NotificationRecord(0x01: pkg=com.music user=UserHandle{0} id=2 key=0|com.music|2\n"
        "                android.title=String (Song (Live))\n"
        "                android.text=String (Artist)\n"
        "    NotificationRecord(0x02: pkg=com.android.systemui user=UserHandle{0} id=1\n"
        "    NotificationRecord(0x03: pkg=com.chat user=UserHandle{0} id=7\n"
        "                android.title=SpannableString (Ali)\n"
        "                android.text=null\n"
        "    NotificationRecord(0x04: pkg=com.textonly user=UserHandle{0} id=9\n"
        "                android.text=String (Only text)\n"
    )

    items = parsers.parse_notifications(output)

    assert [(n.package, n.title, n.text) for n in items] == [
        ("com.music", "Song (Live)", "Artist"),
        ("com.chat", "Ali", ""),
        ("com.textonly", "Only text", ""),
    ]
    assert parsers.parse_notifications("") == ()


def test_parse_call_log_reads_rows_and_null_names():
    output = (
        "Row: 0 number=+905001112233, type=1, date=1791026009793, duration=48, name=Ayşe, Kaya\n"
        "Row: 1 number=05001112233, type=3, date=1791016888427, duration=0, name=NULL\n"
        "garbage\n"
    )

    calls = parsers.parse_call_log(output)

    assert [(c.number, c.name, c.kind, c.duration_s) for c in calls] == [
        ("+905001112233", "Ayşe, Kaya", 1, 48),
        ("05001112233", "", 3, 0),
    ]
    assert calls[0].timestamp_ms == 1791026009793
    assert parsers.parse_call_log("No result found.") == ()


def test_parse_sms_keeps_multiline_bodies_and_direction():
    output = (
        "Row: 0 address=BANK, date=1791026009793, type=1, read=0, body=Line one, with comma\n"
        "line two\n"
        "Row: 1 address=+905001112233, date=1791026000000, type=2, read=1, body=Tamam\n"
    )

    messages = parsers.parse_sms(output)

    assert messages[0].body == "Line one, with comma\nline two"
    assert (messages[0].is_incoming, messages[0].is_read) == (True, False)
    assert (messages[1].address, messages[1].is_incoming, messages[1].is_read) == ("+905001112233", False, True)


def test_parse_contacts_maps_number_keys_to_names():
    output = (
        "Row: 0 data1=+90 505 111 22 33, display_name=Ali Veli\n"
        "Row: 1 data1=112, display_name=Acil\n"
    )

    contacts = parsers.parse_contacts(output)

    assert contacts == {"5051112233": "Ali Veli"}
    assert contacts.get(parsers.phone_key("0505 111 22 33")) == "Ali Veli"
    assert parsers.phone_key("PENTI") == ""


def test_parse_audio_volume_returns_none_when_unsupported():
    volume = parsers.parse_audio_volume("getStreamVolume(3) -> 103", "getStreamMaxVolume(3) -> 160")

    assert (volume.current, volume.maximum) == (103, 160)
    assert parsers.parse_audio_volume("No shell command implementation.", "") is None


def test_parse_brightness_uses_vendor_range_or_android_default():
    vendor = parsers.parse_brightness("1600", "mScreenBrightnessRangeMinimum=8.0\nmScreenBrightnessNormalMaximum=3319.0")
    stock = parsers.parse_brightness("300", "")

    assert (vendor.current, vendor.minimum, vendor.maximum) == (1600, 8, 3319)
    assert (stock.current, stock.minimum, stock.maximum) == (255, 1, 255)


def test_parse_setting_enabled_treats_zero_and_null_as_off():
    assert parsers.parse_setting_enabled("1\n") is True
    assert parsers.parse_setting_enabled("0") is False
    assert parsers.parse_setting_enabled("null") is False


def test_parse_is_awake_defaults_to_awake_when_unknown():
    assert parsers.parse_is_awake("  mWakefulness=Awake") is True
    assert parsers.parse_is_awake("  mWakefulness=Asleep") is False
    assert parsers.parse_is_awake("") is True


def test_parse_is_playing_detects_any_playing_session():
    stopped = "state=PlaybackState {state=STOPPED(1), position=0}"
    playing = "state=PlaybackState {state=PLAYING(3), position=0}"

    assert parsers.parse_is_playing(stopped) is False
    assert parsers.parse_is_playing(f"{stopped}\n{playing}") is True
    assert parsers.parse_is_playing("state=PlaybackState {state=3, position=0}") is True


def test_parse_is_muted_reads_music_stream_block():
    assert parsers.parse_is_muted("- STREAM_MUSIC:\n   Muted: true") is True
    assert parsers.parse_is_muted("- STREAM_MUSIC:\n   Muted: false") is False
    assert parsers.parse_is_muted("") is False


def test_format_size_picks_readable_unit():
    assert parsers.format_size(512) == "512 B"
    assert parsers.format_size(1536) == "1.5 KB"
    assert parsers.format_size(5 * 1024**3) == "5.0 GB"
    assert parsers.format_size(3 * 1024**5) == "3072.0 TB"
