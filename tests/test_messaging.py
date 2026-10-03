from phonedeck.core.messaging import (
    Contact,
    ThreadedMessage,
    contacts_query,
    export_all_sms_csv,
    export_contacts_csv,
    export_conversation_csv,
    fetch_conversations,
    group_conversations,
    names_by_phone_key,
    parse_contact_list,
    parse_threaded_sms,
    sms_query,
    turkish_fold,
)
import pytest

from phonedeck.core.csvsafe import safe_cell
from phonedeck.core.models import SmsMessage


class FakeAdb:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.commands: list[str] = []

    def shell(self, serial: str, command: str, timeout: float = 30) -> str:
        self.commands.append(command)
        key = "sms" if "content://sms" in command else "contacts"
        return self.outputs[key]


SMS_OUTPUT = (
    "Row: 0 thread_id=2, address=+905551112233, date=3000, type=1, read=0, body=Son, mesaj\n"
    "Row: 1 thread_id=1, address=BANKA, date=2000, type=1, read=1, body=Merhaba\n"
    "ikinci satır\n"
    "Row: 2 thread_id=2, address=05551112233, date=1000, type=2, read=1, body=Selam\n"
)
CONTACT_OUTPUT = (
    "Row: 0 contact_id=7, data1=0555 111 22 33, display_name=Çağla Yılmaz\n"
    "Row: 1 contact_id=7, data1=+90 555 111 22 33, display_name=Çağla Yılmaz\n"
    "Row: 2 contact_id=7, data1=0532 000 00 00, display_name=Çağla Yılmaz\n"
    "Row: 3 contact_id=8, data1=0544 999 88 77, display_name=Cem\n"
    "Row: 4 contact_id=9, data1=0533 123 45 67, display_name=Işık\n"
    "Row: 5 contact_id=10, data1=0533 765 43 21, display_name=Hasan\n"
)


def message(timestamp: int, is_incoming: bool = True, is_read: bool = True) -> SmsMessage:
    return SmsMessage("+905551112233", "x", timestamp, is_incoming, is_read)


def test_parse_threaded_sms_keeps_commas_and_newlines_in_body():
    parsed = parse_threaded_sms(SMS_OUTPUT)

    assert [item.thread_id for item in parsed] == ["2", "1", "2"]
    assert parsed[0].message.body == "Son, mesaj"
    assert parsed[1].message.body == "Merhaba\nikinci satır"
    assert parsed[2].message.is_incoming is False


def test_group_conversations_orders_threads_and_messages():
    conversations = group_conversations(parse_threaded_sms(SMS_OUTPUT), {})

    assert [c.thread_id for c in conversations] == ["2", "1"]
    assert [m.timestamp_ms for m in conversations[0].messages] == [1000, 3000]
    assert conversations[0].last_timestamp_ms == 3000


def test_group_conversations_counts_only_unread_incoming():
    items = (
        ThreadedMessage("1", message(1, True, False)),
        ThreadedMessage("1", message(2, False, False)),
        ThreadedMessage("1", message(3, True, True)),
    )

    (conversation,) = group_conversations(items, {})

    assert conversation.unread_count == 1


def test_group_conversations_resolves_names_across_number_formats():
    names = names_by_phone_key(parse_contact_list(CONTACT_OUTPUT))

    conversations = group_conversations(parse_threaded_sms(SMS_OUTPUT), names)

    assert conversations[0].display_name == "Çağla Yılmaz"
    assert conversations[1].display_name == "BANKA"


def test_group_conversations_falls_back_to_address_when_thread_is_missing():
    items = (
        ThreadedMessage("", SmsMessage("A", "x", 1, True, True)),
        ThreadedMessage("", SmsMessage("B", "y", 2, True, True)),
    )

    conversations = group_conversations(items, {})

    assert len(conversations) == 2


def test_parse_contact_list_merges_numbers_of_one_contact():
    contacts = parse_contact_list(CONTACT_OUTPUT)

    cagla = next(c for c in contacts if c.name == "Çağla Yılmaz")
    assert cagla.numbers == ("0555 111 22 33", "0532 000 00 00")


def test_parse_contact_list_sorts_with_turkish_alphabet():
    contacts = parse_contact_list(CONTACT_OUTPUT)

    assert [c.name for c in contacts] == ["Cem", "Çağla Yılmaz", "Hasan", "Işık"]


def test_turkish_fold_handles_dotted_and_dotless_i():
    assert turkish_fold("IŞIK") == "ışık"
    assert turkish_fold("İSTANBUL") == "istanbul"


def test_sms_query_uses_thread_id_body_last_and_limit():
    command = sms_query(25)

    assert "thread_id" in command
    assert command.index("body") > command.index("read")
    assert "--sort 'date DESC LIMIT 25'" in command


def test_queries_clamp_limit_to_at_least_one():
    assert "LIMIT 1'" in sms_query(0)
    assert "LIMIT 1'" in contacts_query(-5)


def test_fetch_conversations_queries_both_providers():
    fake = FakeAdb({"sms": SMS_OUTPUT, "contacts": CONTACT_OUTPUT})

    conversations = fetch_conversations(fake, "serial", limit=10)

    assert len(conversations) == 2
    assert any("LIMIT 10'" in command for command in fake.commands)
    assert any("data/phones" in command for command in fake.commands)


def test_export_conversation_csv_writes_bom_and_quotes_special_characters(tmp_path):
    (conversation,) = group_conversations(
        (ThreadedMessage("1", SmsMessage("+90555", 'Şöyle, "böyle"\nsatır', 1000, True, True)),),
        {},
    )
    target = tmp_path / "konusma.csv"

    count = export_conversation_csv(conversation, target)

    raw = target.read_bytes()
    assert count == 1
    assert raw.startswith(b"\xef\xbb\xbf")
    assert 'Şöyle, ""böyle""\nsatır' in raw.decode("utf-8-sig")


def test_export_all_sms_csv_has_one_row_per_message(tmp_path):
    conversations = group_conversations(parse_threaded_sms(SMS_OUTPUT), {})
    target = tmp_path / "hepsi.csv"

    count = export_all_sms_csv(conversations, target)

    assert count == 3


def test_export_contacts_csv_joins_numbers(tmp_path):
    target = tmp_path / "kisiler.csv"

    export_contacts_csv((Contact("Ayşe", ("1", "2")),), target)

    lines = target.read_text(encoding="utf-8-sig").splitlines()
    assert lines == ["Ad,Numaralar", "Ayşe,1; 2"]


# --- spreadsheet formula protection ---------------------------------------------
@pytest.mark.parametrize("value", ['=HYPERLINK("http://x")', "+cmd|' /C calc'!A0", "-2+3+cmd", "@SUM(1)", "\t=1"])
def test_safe_cell_marks_formula_like_text_as_plain_text(value):
    assert safe_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["+90 555 111 22 33", "+905551112233; 0555", "-", "Merhaba = selam", "a@b.co", ""])
def test_safe_cell_leaves_phone_numbers_and_ordinary_text_alone(value):
    assert safe_cell(value) == value


def test_exported_message_body_cannot_start_a_formula(tmp_path):
    message = SmsMessage("+90555", "=1+1", 1000, True, True)
    conversation = group_conversations((ThreadedMessage("1", message),), {})[0]
    target = tmp_path / "sms.csv"

    export_conversation_csv(conversation, target)

    assert target.read_text(encoding="utf-8-sig").splitlines()[1].endswith(",'=1+1")
