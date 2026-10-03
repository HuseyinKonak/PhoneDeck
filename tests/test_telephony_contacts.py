import base64
from pathlib import Path

import pytest

from phonedeck.core import contacts, telephony
from phonedeck.core.adb import AdbError
from phonedeck.core.contacts import Account, ContactRecord
from phonedeck.core.scrcpy.client import AudioClient, ScrcpyOptions

ISUB = """defaultSubId=1
defaultVoiceSubId=-1
defaultDataSubId=1
defaultSmsSubId=4
  [SubscriptionInfoInternal: id=1 iccId=89[****] simSlotIndex=0 portIndex=0 isEmbedded=0 carrierId=1737 displayName=SIM1 carrierName=Turk Telekom isOpportunistic=0 groupUuid= displayNameSource=CARRIER iconTint=-16746133 number=[****] dataRoaming=1
  [SubscriptionInfoInternal: id=4 iccId=89[****] simSlotIndex=1 portIndex=0 isEmbedded=0 carrierId=1736 displayName=Vodafone carrierName=vodafone TR isOpportunistic=0 groupUuid= displayNameSource=CARRIER iconTint=-9033797 number=[****] dataRoaming=1
  [SubscriptionInfoInternal: id=2 iccId=89[****] simSlotIndex=-1 portIndex=-1 isEmbedded=0 carrierId=1737 displayName=SIM1 carrierName=Hizmet yok isOpportunistic=0 groupUuid= displayNameSource=CARRIER iconTint=-16746133 number= dataRoaming=1
  [SubscriptionInfoInternal: id=1 iccId=89[****] simSlotIndex=0 portIndex=0 isEmbedded=0 carrierId=1737 displayName=SIM1 carrierName=Turk Telekom isOpportunistic=0 groupUuid= displayNameSource=CARRIER iconTint=-16746133 number=[****] dataRoaming=1
"""

CONTACT_ROWS = (
    "Row: 0 contact_id=10, raw_contact_id=100, _id=1, account_type=com.whatsapp, mimetype=vnd.android.cursor.item/name, data1=Ali W\n"
    "Row: 1 contact_id=10, raw_contact_id=100, _id=2, account_type=com.whatsapp, mimetype=vnd.android.cursor.item/phone_v2, data1=+90 505 111 22 33\n"
    "Row: 2 contact_id=10, raw_contact_id=101, _id=3, account_type=com.google, mimetype=vnd.android.cursor.item/name, data1=Ali Veli\n"
    "Row: 3 contact_id=10, raw_contact_id=101, _id=4, account_type=com.google, mimetype=vnd.android.cursor.item/phone_v2, data1=+90 505 111 22 33\n"
    "Row: 4 contact_id=10, raw_contact_id=101, _id=5, account_type=com.google, mimetype=vnd.android.cursor.item/email_v2, data1=ali@example.com\n"
    "Row: 5 contact_id=11, raw_contact_id=102, _id=6, account_type=com.whatsapp, mimetype=vnd.android.cursor.item/name, data1=Zeynep\n"
    "Row: 6 contact_id=11, raw_contact_id=102, _id=7, account_type=com.whatsapp, mimetype=vnd.android.cursor.item/phone_v2, data1=0555\n"
    "Row: 7 contact_id=12, raw_contact_id=103, _id=8, account_type=com.google, mimetype=vnd.android.cursor.item/photo, data1=NULL\n"
)


class FakeAdb:
    """Replies to shell calls from a queue (last reply repeats) and records every command."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or [""]
        self.shell_calls: list[str] = []
        self.run_calls: list[list[str]] = []

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.shell_calls.append(command)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def run(self, args: list[str], serial: str | None = None, timeout: float = 30) -> str:
        self.run_calls.append(args)
        return ""


def decode(value: str) -> str:
    return base64.urlsafe_b64decode(value).decode()


# --- SIMs -------------------------------------------------------------------
def test_parse_sims_keeps_inserted_sims_once_and_reads_defaults():
    state = telephony.parse_sims(ISUB)

    assert [(s.sub_id, s.slot, s.carrier, s.label) for s in state.sims] == [
        (1, 0, "Turk Telekom", "SIM 1 · Turk Telekom"),
        (4, 1, "vodafone TR", "SIM 2 · vodafone TR"),
    ]
    assert state.sims[1].color == "#7627BB"
    assert (state.default_voice, state.default_sms, state.default_data) == (-1, 4, 1)
    assert state.by_id(4).slot == 1 and state.by_id(9) is None
    assert telephony.parse_sims("") == telephony.SimState()


def test_send_sms_encodes_arguments_and_returns_helper_message():
    adb = FakeAdb("WARNING: noise\nOK sent sendTextForSubscriber sub=4 parts=1 params=10\n")

    result = telephony.send_sms(adb, "s", 4, "0555 000 00 00", "Merhaba; $(reboot) 'çğ'")

    command = adb.shell_calls[0]
    _, sub_id, number, text = command.split("phonedeck.PhoneTool ")[1].split(" ")
    assert result.startswith("sent sendTextForSubscriber")
    assert (sub_id, decode(number), decode(text)) == ("4", "05550000000", "Merhaba; $(reboot) 'çğ'")
    assert "reboot" not in command and adb.run_calls[0][0] == "push"


def test_send_sms_dry_run_and_validation():
    adb = FakeAdb("OK dry sendTextForSubscriber sub=1 parts=1 params=10")

    telephony.send_sms(adb, "s", 1, "0555", "x", is_dry_run=True)

    assert adb.shell_calls[0].endswith(" dry")
    for number, text in (("abc", "x"), ("0555", "  "), ("0555", "x" * 1601), ("0555; reboot", "x")):
        with pytest.raises(ValueError):
            telephony.send_sms(adb, "s", 1, number, text)
    assert len(adb.shell_calls) == 1


def test_helper_errors_become_exceptions():
    with pytest.raises(AdbError, match="SecurityException"):
        telephony.send_sms(FakeAdb("ERROR SecurityException: denied"), "s", 1, "0555", "x")
    with pytest.raises(AdbError, match="Yardımcı program"):
        telephony.place_call(FakeAdb(""), "s", 1, "0555")


def test_place_call_and_call_subscriptions():
    adb = FakeAdb("OK dialing placeCall sim=sub 4 params=4")

    assert telephony.place_call(adb, "s", 4, "+90 (555) 000-00-00").startswith("dialing")
    assert decode(adb.shell_calls[0].split(" ")[-1]) == "+905550000000"
    rows = "Row: 0 date=1791026009793, subscription_id=1\nRow: 1 date=1791016888427, subscription_id=4\n"
    assert telephony.parse_call_subscriptions(rows) == {1791026009793: 1, 1791016888427: 4}


def test_last_sms_state_maps_provider_types():
    assert telephony.last_sms_state(FakeAdb("Row: 0 type=2"), "s", "0555") == "gönderildi"
    assert telephony.last_sms_state(FakeAdb("Row: 0 type=5"), "s", "0555") == "gönderilemedi"
    assert telephony.last_sms_state(FakeAdb("No result found."), "s", "0555") == ""


# --- contacts ---------------------------------------------------------------
def test_parse_contacts_targets_only_user_editable_rows():
    records = contacts.parse_contacts(CONTACT_ROWS)

    ali, zeynep = records
    assert (ali.name, ali.numbers, ali.emails) == ("Ali W", ("+90 505 111 22 33",), ("ali@example.com",))
    assert (ali.raw_contact_id, ali.name_row_id, ali.detail_row_ids) == (101, 3, (4, 5))
    assert (zeynep.name, zeynep.is_editable, zeynep.detail_row_ids) == ("Zeynep", False, ())


def test_parse_accounts_lists_editable_accounts_by_usage():
    rows = (
        "Row: 0 account_type=com.whatsapp, account_name=WhatsApp\n"
        "Row: 1 account_type=com.google, account_name=a@gmail.com\n"
        "Row: 2 account_type=com.google, account_name=a@gmail.com\n"
        "Row: 3 account_type=com.android.local, account_name=Phone\n"
    )

    accounts = contacts.parse_accounts(rows)

    assert accounts == (Account("com.google", "a@gmail.com"), Account("com.android.local", "Phone"))
    assert accounts[0].label == "a@gmail.com" and Account("NULL", "NULL").label == "Telefon"


def test_validate_draft_rejects_bad_input():
    draft = contacts.validate_draft("  Ali   Veli ", [" 0555 000 ", ""], ["a@b.co"])

    assert (draft.name, draft.numbers, draft.emails) == ("Ali Veli", ("0555 000",), ("a@b.co",))
    for name, numbers, emails in (("", ["1"], []), ("A", [], []), ("A", ["12; rm"], []), ("A", ["1"], ["nope"])):
        with pytest.raises(ValueError):
            contacts.validate_draft(name, numbers, emails)


def test_add_contact_never_writes_into_an_existing_raw_contact():
    # The insert is "accepted" but the newest id does not change: nothing was created.
    adb = FakeAdb("Row: 0 _id=1790", "", "Row: 0 _id=1790")
    draft = contacts.validate_draft("Yeni Kişi", ["0555"], [])

    with pytest.raises(AdbError, match="oluşturulamadı"):
        contacts.add_contact(adb, "s", draft, Account("com.google", "a@gmail.com"))
    assert not any("/data " in call for call in adb.shell_calls)


def test_add_contact_surfaces_provider_rejection():
    adb = FakeAdb(
        "Row: 0 _id=1790",
        "Error while accessing provider:com.android.contacts\n"
        "java.lang.IllegalArgumentException: Cannot add contacts to local accounts\n",
    )

    with pytest.raises(AdbError, match="Cannot add contacts"):
        contacts.add_contact(adb, "s", contacts.validate_draft("A", ["1"], []), Account("NULL", "NULL"))
    assert len(adb.shell_calls) == 2


def test_add_contact_inserts_rows_into_the_new_raw_contact():
    adb = FakeAdb("Row: 0 _id=1790", "", "Row: 0 _id=1791", "")
    draft = contacts.validate_draft("Ali O'Neil", ["0555 1"], ["a@b.co"])

    raw_id = contacts.add_contact(adb, "s", draft, Account("com.google", "a@gmail.com"))

    assert raw_id == 1791
    assert "account_type:s:com.google" in adb.shell_calls[1] and "a@gmail.com" in adb.shell_calls[1]
    inserts = adb.shell_calls[3:]
    assert len(inserts) == 3 and all("raw_contact_id:i:1791" in call for call in inserts)
    assert "'data1:s:Ali O'\"'\"'Neil'" in inserts[0]


def test_update_contact_adds_new_rows_before_removing_old_ones():
    adb = FakeAdb("")
    record = ContactRecord(10, "Ali", ("1",), (), raw_contact_id=101, name_row_id=3, detail_row_ids=(4, 5))

    contacts.update_contact(adb, "s", record, contacts.validate_draft("Ali Veli", ["2", "3"], []))

    kinds = [call.split(" ")[1] for call in adb.shell_calls]
    assert kinds == ["insert", "insert", "delete", "update"]
    assert "_id IN (4,5) AND raw_contact_id=101" in adb.shell_calls[2]
    assert "_id=3 AND raw_contact_id=101" in adb.shell_calls[3]


def test_read_only_contacts_cannot_be_changed():
    adb = FakeAdb("")
    record = ContactRecord(11, "Zeynep", ("0555",))
    draft = contacts.validate_draft("Z", ["1"], [])

    with pytest.raises(ValueError):
        contacts.update_contact(adb, "s", record, draft)
    with pytest.raises(ValueError):
        contacts.delete_contact(adb, "s", record)
    assert adb.shell_calls == []


def test_delete_contact_and_csv_export(tmp_path: Path):
    adb = FakeAdb("")
    record = ContactRecord(10, "Çağrı", ("1", "2"), ("a@b.co",), raw_contact_id=101)

    contacts.delete_contact(adb, "s", record)
    path = tmp_path / "rehber.csv"
    count = contacts.export_csv((record,), str(path))

    assert "raw_contacts --where _id=101" in adb.shell_calls[0]
    assert count == 1 and path.read_text(encoding="utf-8-sig").splitlines()[1] == "Çağrı,1; 2,a@b.co"


# --- camera / audio options -------------------------------------------------
def test_camera_options_disable_control_and_screen_flags():
    arguments = ScrcpyOptions(camera_facing="front", max_size=1280, stay_awake=True).server_arguments()

    assert arguments[:4] == ["audio=false", "video_source=camera", "camera_facing=front", "control=false"]
    assert "stay_awake=true" not in arguments
    assert "control=true" in ScrcpyOptions().server_arguments()
    with pytest.raises(ValueError):
        ScrcpyOptions(camera_facing="side").server_arguments()


def test_audio_client_arguments_and_source_validation():
    mic = AudioClient(FakeAdb(), "s", lambda data: None, lambda state, detail: None, source="mic")
    both = AudioClient(FakeAdb(), "s", lambda d: None, lambda s, d: None, source="playback", keep_on_phone=True)

    assert mic._server_arguments() == ["video=false", "control=false", "audio=true", "audio_codec=raw", "audio_source=mic"]
    assert both._server_arguments()[-2:] == ["audio_source=playback", "audio_dup=true"]
    with pytest.raises(ValueError):
        AudioClient(FakeAdb(), "s", lambda d: None, lambda s, d: None, source="speaker; reboot")


# --- notifications ----------------------------------------------------------
def test_clear_notifications_runs_the_helper_and_supports_dry_run():
    adb = FakeAdb("OK cleared onClearAllNotifications user=0 params=1")

    result = telephony.clear_notifications(adb, "s")
    telephony.clear_notifications(adb, "s", is_dry_run=True)

    assert result.startswith("cleared") and adb.run_calls[0][0] == "push"
    assert adb.shell_calls[0].endswith("phonedeck.PhoneTool clearnotifications")
    assert adb.shell_calls[1].endswith("phonedeck.PhoneTool clearnotifications dry")


def test_clear_notifications_reports_helper_errors():
    with pytest.raises(AdbError, match="SecurityException"):
        telephony.clear_notifications(FakeAdb("ERROR SecurityException: denied"), "s")
