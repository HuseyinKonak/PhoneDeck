"""Conversations, contact lists and CSV export built from `content query` output."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .adb import Adb
from .csvsafe import safe_row
from .models import SmsMessage
from .parsers import SMS_TYPE_INBOX, _null_to_empty, _split_rows, phone_key

DEFAULT_SMS_LIMIT = 500
DEFAULT_CONTACT_LIMIT = 5000
MS_PER_SECOND = 1000
CSV_ENCODING = "utf-8-sig"
CSV_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
NUMBER_SEPARATOR = "; "
UNKNOWN_SENDER = "Bilinmeyen"

_SMS_PROJECTION = "thread_id:address:date:type:read:body"
_CONTACT_PROJECTION = "contact_id:data1:display_name"
_SMS_ROW = re.compile(
    r"thread_id=(?P<thread>.*?), address=(?P<address>.*?), date=(?P<date>\d+), "
    r"type=(?P<type>\d+), read=(?P<read>\d+), body=(?P<body>.*)",
    re.DOTALL,
)
_CONTACT_ROW = re.compile(
    r"contact_id=(?P<id>.*?), data1=(?P<number>.*?), display_name=(?P<name>.*)", re.DOTALL
)
# Turkish alphabet order, so "Çağla" sorts after "Cem" and "Işık" after "Hasan".
_TURKISH_ALPHABET = "abcçdefgğhıijklmnoöprsştuüvyz"
_TURKISH_RANK = {letter: index for index, letter in enumerate(_TURKISH_ALPHABET)}
_UNRANKED = len(_TURKISH_ALPHABET)


@dataclass(frozen=True)
class Contact:
    name: str
    numbers: tuple[str, ...]


@dataclass(frozen=True)
class Conversation:
    thread_id: str
    address: str
    display_name: str
    messages: tuple[SmsMessage, ...]  # oldest first
    last_timestamp_ms: int
    unread_count: int


@dataclass(frozen=True)
class ThreadedMessage:
    thread_id: str
    message: SmsMessage


def turkish_fold(text: str) -> str:
    """Case-fold with the Turkish dotted/dotless i rules."""
    return text.replace("İ", "i").replace("I", "ı").casefold()


def turkish_sort_key(text: str) -> tuple[tuple[int, str], ...]:
    return tuple((_TURKISH_RANK.get(char, _UNRANKED), char) for char in turkish_fold(text))


def _limited(limit: int) -> int:
    return max(int(limit), 1)


# --- parsing -----------------------------------------------------------------
def parse_threaded_sms(output: str) -> tuple[ThreadedMessage, ...]:
    """Parse rows projected as thread_id:address:date:type:read:body."""
    parsed = []
    for row in _split_rows(output):
        match = _SMS_ROW.match(row)
        if not match:
            continue
        message = SmsMessage(
            address=_null_to_empty(match.group("address")),
            body=_null_to_empty(match.group("body")),
            timestamp_ms=int(match.group("date")),
            is_incoming=match.group("type") == SMS_TYPE_INBOX,
            is_read=match.group("read") != "0",
        )
        parsed.append(ThreadedMessage(_null_to_empty(match.group("thread")), message))
    return tuple(parsed)


def parse_contact_list(output: str) -> tuple[Contact, ...]:
    """Merge phone rows of the same contact id into one `Contact`, sorted by name."""
    numbers_by_id: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for row in _split_rows(output):
        match = _CONTACT_ROW.match(row)
        if not match:
            continue
        number = match.group("number").strip()
        name = _null_to_empty(match.group("name").split("\n", 1)[0])
        if not number or not name:
            continue
        key = match.group("id").strip() or name
        names.setdefault(key, name)
        known = numbers_by_id.setdefault(key, [])
        if not any(_same_number(number, other) for other in known):
            known.append(number)
    contacts = [Contact(names[key], tuple(numbers)) for key, numbers in numbers_by_id.items()]
    return tuple(sorted(contacts, key=lambda contact: turkish_sort_key(contact.name)))


def _same_number(first: str, second: str) -> bool:
    key = phone_key(first)
    return first == second if not key else key == phone_key(second)


def names_by_phone_key(contacts: tuple[Contact, ...]) -> dict[str, str]:
    names = {}
    for contact in contacts:
        for number in contact.numbers:
            key = phone_key(number)
            if key:
                names.setdefault(key, contact.name)
    return names


# --- grouping ----------------------------------------------------------------
def group_conversations(
    messages: tuple[ThreadedMessage, ...], names: dict[str, str]
) -> tuple[Conversation, ...]:
    """Group by thread; newest conversation first, messages oldest first."""
    threads: dict[str, list[SmsMessage]] = {}
    for item in messages:
        # Messages without a thread id are grouped by their address instead.
        key = item.thread_id or f"addr:{item.message.address}"
        threads.setdefault(key, []).append(item.message)
    conversations = [_build_conversation(key, group, names) for key, group in threads.items()]
    return tuple(sorted(conversations, key=lambda c: c.last_timestamp_ms, reverse=True))


def _build_conversation(
    thread_id: str, group: list[SmsMessage], names: dict[str, str]
) -> Conversation:
    ordered = tuple(sorted(group, key=lambda message: message.timestamp_ms))
    address = ordered[-1].address
    return Conversation(
        thread_id=thread_id,
        address=address,
        display_name=names.get(phone_key(address)) or address or UNKNOWN_SENDER,
        messages=ordered,
        last_timestamp_ms=ordered[-1].timestamp_ms,
        unread_count=sum(1 for message in ordered if message.is_incoming and not message.is_read),
    )


# --- queries -----------------------------------------------------------------
def sms_query(limit: int = DEFAULT_SMS_LIMIT) -> str:
    return (
        "content query --uri content://sms "
        f"--projection {_SMS_PROJECTION} "
        f"--sort 'date DESC LIMIT {_limited(limit)}'"
    )


def contacts_query(limit: int = DEFAULT_CONTACT_LIMIT) -> str:
    return (
        "content query --uri content://com.android.contacts/data/phones "
        f"--projection {_CONTACT_PROJECTION} "
        f"--sort 'display_name LIMIT {_limited(limit)}'"
    )


def fetch_contacts(adb: Adb, serial: str, limit: int = DEFAULT_CONTACT_LIMIT) -> tuple[Contact, ...]:
    return parse_contact_list(adb.shell(serial, contacts_query(limit)))


def fetch_threaded_sms(
    adb: Adb, serial: str, limit: int = DEFAULT_SMS_LIMIT
) -> tuple[ThreadedMessage, ...]:
    return parse_threaded_sms(adb.shell(serial, sms_query(limit)))


def fetch_conversations(
    adb: Adb, serial: str, limit: int = DEFAULT_SMS_LIMIT
) -> tuple[Conversation, ...]:
    messages = fetch_threaded_sms(adb, serial, limit)
    names = names_by_phone_key(fetch_contacts(adb, serial))
    return group_conversations(messages, names)


# --- CSV export --------------------------------------------------------------
def _format_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / MS_PER_SECOND).strftime(CSV_TIME_FORMAT)


def _message_row(name: str, message: SmsMessage) -> list[str]:
    direction = "Gelen" if message.is_incoming else "Giden"
    return [name, message.address, direction, _format_time(message.timestamp_ms), message.body]


def _write_csv(path: Path | str, header: list[str], rows: list[list[str]]) -> int:
    """Write UTF-8 with BOM so Excel shows Turkish characters correctly."""
    with open(path, "w", encoding=CSV_ENCODING, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(safe_row(row) for row in rows)
    return len(rows)


_MESSAGE_HEADER = ["Kişi", "Numara", "Yön", "Tarih", "Mesaj"]


def export_conversation_csv(conversation: Conversation, path: Path | str) -> int:
    rows = [_message_row(conversation.display_name, m) for m in conversation.messages]
    return _write_csv(path, _MESSAGE_HEADER, rows)


def export_all_sms_csv(conversations: tuple[Conversation, ...], path: Path | str) -> int:
    rows = [
        _message_row(conversation.display_name, message)
        for conversation in conversations
        for message in conversation.messages
    ]
    return _write_csv(path, _MESSAGE_HEADER, rows)


def export_contacts_csv(contacts: tuple[Contact, ...], path: Path | str) -> int:
    rows = [[contact.name, NUMBER_SEPARATOR.join(contact.numbers)] for contact in contacts]
    return _write_csv(path, ["Ad", "Numaralar"], rows)
