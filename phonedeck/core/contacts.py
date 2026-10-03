"""Read and edit the phone's contacts through the contacts provider.

Every write is checked: the `content` tool exits with 0 even when the provider
rejects a change, so its output is inspected after each command, and rows are only
ever added to a raw contact this module has positively identified.
"""

from __future__ import annotations

import csv
import re
import shlex
from dataclasses import dataclass

from .adb import Adb, AdbError
from .csvsafe import safe_row

CONTACTS = "content://com.android.contacts"
MIME_NAME = "vnd.android.cursor.item/name"
MIME_PHONE = "vnd.android.cursor.item/phone_v2"
MIME_EMAIL = "vnd.android.cursor.item/email_v2"
PHONE_TYPE_MOBILE = 2
EMAIL_TYPE_HOME = 1
MAX_NAME_LENGTH = 200
QUERY_TIMEOUT = 60
NULL = "NULL"
# Accounts whose contacts the user can edit. Everything else (WhatsApp, Telegram…)
# is written by that app's own sync and must be left alone.
EDITABLE_ACCOUNT_PREFIXES = (
    "com.google", "com.android.", "com.oplus", "com.oppo", "com.coloros",
    "com.samsung", "vnd.sec", "com.xiaomi", "com.huawei", "com.osp.app",
)

_ROW_START = re.compile(r"^Row: \d+ ", re.MULTILINE)
_DATA_ROW = re.compile(
    r"contact_id=(?P<contact>\d+), raw_contact_id=(?P<raw>\d+), _id=(?P<row>\d+), "
    r"account_type=(?P<account>\S+), mimetype=(?P<mime>\S+), data1=(?P<value>.*)",
    re.DOTALL,
)
_ACCOUNT_ROW = re.compile(r"account_type=(?P<type>\S+), account_name=(?P<name>.*)")
_PHONE = re.compile(r"\+?[0-9*#() \-]{1,30}")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_FAILURE = re.compile(r"Error while accessing provider|Exception")


@dataclass(frozen=True)
class Account:
    type: str
    name: str

    @property
    def label(self) -> str:
        return self.name if self.name and self.name != NULL else "Telefon"


@dataclass(frozen=True)
class ContactRecord:
    contact_id: int
    name: str
    numbers: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    # The raw contact edits are applied to, and its current rows (0 / empty = read-only).
    raw_contact_id: int = 0
    name_row_id: int = 0
    detail_row_ids: tuple[int, ...] = ()

    @property
    def is_editable(self) -> bool:
        return self.raw_contact_id > 0


@dataclass(frozen=True)
class ContactDraft:
    """Validated values for a new or edited contact."""

    name: str
    numbers: tuple[str, ...]
    emails: tuple[str, ...]


def is_editable_account(account_type: str) -> bool:
    return account_type == NULL or account_type.startswith(EDITABLE_ACCOUNT_PREFIXES)


def validate_draft(name: str, numbers: list[str], emails: list[str]) -> ContactDraft:
    clean_name = " ".join(name.split())
    if not clean_name or len(clean_name) > MAX_NAME_LENGTH:
        raise ValueError("Kişi adı boş veya çok uzun.")
    clean_numbers = tuple(number.strip() for number in numbers if number.strip())
    clean_emails = tuple(email.strip() for email in emails if email.strip())
    for number in clean_numbers:
        if not _PHONE.fullmatch(number):
            raise ValueError(f"Geçersiz telefon numarası: {number}")
    for email in clean_emails:
        if not _EMAIL.fullmatch(email):
            raise ValueError(f"Geçersiz e-posta: {email}")
    if not clean_numbers and not clean_emails:
        raise ValueError("En az bir telefon numarası veya e-posta gerekli.")
    return ContactDraft(clean_name, clean_numbers, clean_emails)


def _new_group() -> dict:
    return {"name": "", "numbers": [], "emails": [], "raw": 0, "name_row": 0, "details": {}}


def _add_row(group: dict, match: re.Match) -> None:
    value = match.group("value").strip()
    if not value or value == NULL:
        return
    raw, row, mime = int(match.group("raw")), int(match.group("row")), match.group("mime")
    is_editable = is_editable_account(match.group("account"))
    if mime == MIME_NAME:
        group["name"] = group["name"] or value
        if is_editable and not group["raw"]:
            # Edits go to the first user-editable raw contact that carries a name.
            group["raw"], group["name_row"] = raw, row
        return
    target = group["numbers"] if mime == MIME_PHONE else group["emails"]
    if value not in target:
        target.append(value)
    if is_editable:
        group["details"].setdefault(raw, []).append(row)


def parse_contacts(output: str) -> tuple[ContactRecord, ...]:
    """Group name / phone / e-mail data rows into one record per contact."""
    groups: dict[int, dict] = {}
    for row in _ROW_START.split(output)[1:]:
        match = _DATA_ROW.match(row.strip())
        if match and match.group("mime") in (MIME_NAME, MIME_PHONE, MIME_EMAIL):
            _add_row(groups.setdefault(int(match.group("contact")), _new_group()), match)
    records = (
        ContactRecord(
            contact_id=contact_id,
            name=group["name"] or (group["numbers"] or group["emails"] or ["?"])[0],
            numbers=tuple(group["numbers"]),
            emails=tuple(group["emails"]),
            raw_contact_id=group["raw"],
            name_row_id=group["name_row"],
            detail_row_ids=tuple(group["details"].get(group["raw"], ())),
        )
        for contact_id, group in groups.items()
    )
    return tuple(sorted(records, key=lambda record: record.name.casefold()))


def list_contacts(adb: Adb, serial: str) -> tuple[ContactRecord, ...]:
    where = f"mimetype IN ('{MIME_NAME}','{MIME_PHONE}','{MIME_EMAIL}')"
    output = adb.shell(
        serial,
        f"content query --uri {CONTACTS}/data "
        "--projection contact_id:raw_contact_id:_id:account_type:mimetype:data1 "
        f"--where {shlex.quote(where)}",
        timeout=QUERY_TIMEOUT,
    )
    return parse_contacts(output)


def parse_accounts(output: str) -> tuple[Account, ...]:
    """Distinct user-editable accounts, most used first."""
    counts: dict[Account, int] = {}
    for row in _ROW_START.split(output)[1:]:
        match = _ACCOUNT_ROW.match(row.strip())
        if match and is_editable_account(match.group("type")):
            account = Account(match.group("type"), match.group("name").strip())
            counts[account] = counts.get(account, 0) + 1
    return tuple(sorted(counts, key=lambda account: -counts[account]))


def list_accounts(adb: Adb, serial: str) -> tuple[Account, ...]:
    """Accounts new contacts can be saved to, taken from the existing contacts."""
    output = adb.shell(
        serial,
        f"content query --uri {CONTACTS}/raw_contacts "
        "--projection account_type:account_name --where 'deleted=0'",
        timeout=QUERY_TIMEOUT,
    )
    return parse_accounts(output)


def export_csv(records: tuple[ContactRecord, ...], path: str) -> int:
    """Write contacts to a CSV file Excel opens correctly (UTF-8 with BOM). Returns the count."""
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Ad", "Telefonlar", "E-postalar"])
        for record in records:
            writer.writerow(safe_row([record.name, "; ".join(record.numbers), "; ".join(record.emails)]))
    return len(records)


def _run(adb: Adb, serial: str, command: str) -> str:
    """Run one provider command and raise if the provider reported a failure."""
    output = adb.shell(serial, command + " 2>&1", timeout=QUERY_TIMEOUT)
    if _FAILURE.search(output):
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        detail = next((line for line in lines if "Exception" in line), lines[0])
        raise AdbError(f"Rehber değişikliği reddedildi: {detail.split(': ', 1)[-1]}")
    return output


def _bind(column: str, kind: str, value: object) -> str:
    return "--bind " + shlex.quote(f"{column}:{kind}:{value}")


def _insert_data(adb: Adb, serial: str, raw_id: int, mimetype: str, value: str, kind: int = 0) -> None:
    extra = f" {_bind('data2', 'i', kind)}" if kind else ""
    _run(
        adb,
        serial,
        f"content insert --uri {CONTACTS}/data {_bind('raw_contact_id', 'i', int(raw_id))} "
        f"{_bind('mimetype', 's', mimetype)} {_bind('data1', 's', value)}{extra}",
    )


def _insert_details(adb: Adb, serial: str, raw_id: int, draft: ContactDraft) -> None:
    for number in draft.numbers:
        _insert_data(adb, serial, raw_id, MIME_PHONE, number, PHONE_TYPE_MOBILE)
    for email in draft.emails:
        _insert_data(adb, serial, raw_id, MIME_EMAIL, email, EMAIL_TYPE_HOME)


def _newest_raw_contact(adb: Adb, serial: str) -> int:
    output = _run(
        adb, serial,
        f"content query --uri {CONTACTS}/raw_contacts --projection _id --sort '_id DESC LIMIT 1'",
    )
    match = re.search(r"_id=(\d+)", output)
    return int(match.group(1)) if match else 0


def add_contact(adb: Adb, serial: str, draft: ContactDraft, account: Account) -> int:
    """Create a contact in `account`. Returns the new raw contact id."""
    previous = _newest_raw_contact(adb, serial)
    if account.type == NULL:
        binds = f"{_bind('account_type', 'n', '')} {_bind('account_name', 'n', '')}"
    else:
        binds = f"{_bind('account_type', 's', account.type)} {_bind('account_name', 's', account.name)}"
    _run(adb, serial, f"content insert --uri {CONTACTS}/raw_contacts {binds}")
    raw_id = _newest_raw_contact(adb, serial)
    if raw_id <= previous:
        # Never fall through to an existing contact: nothing new was created.
        raise AdbError("Kişi oluşturulamadı: telefon yeni kayıt açmadı.")
    _insert_data(adb, serial, raw_id, MIME_NAME, draft.name)
    _insert_details(adb, serial, raw_id, draft)
    return raw_id


def update_contact(adb: Adb, serial: str, record: ContactRecord, draft: ContactDraft) -> None:
    """Replace the name, numbers and e-mails of an editable contact.

    New rows are written first and the old ones removed afterwards, so a failure
    midway leaves duplicates rather than a contact without numbers.
    """
    if not record.is_editable:
        raise ValueError("Bu kişi başka bir uygulamaya ait; düzenlenemez.")
    raw_id = int(record.raw_contact_id)
    _insert_details(adb, serial, raw_id, draft)
    if record.detail_row_ids:
        ids = ",".join(str(int(row_id)) for row_id in record.detail_row_ids)
        where = shlex.quote(f"_id IN ({ids}) AND raw_contact_id={raw_id}")
        _run(adb, serial, f"content delete --uri {CONTACTS}/data --where {where}")
    if draft.name != record.name and record.name_row_id:
        where = shlex.quote(f"_id={int(record.name_row_id)} AND raw_contact_id={raw_id}")
        _run(
            adb, serial,
            f"content update --uri {CONTACTS}/data {_bind('data1', 's', draft.name)} --where {where}",
        )


def delete_contact(adb: Adb, serial: str, record: ContactRecord) -> None:
    """Delete the user-editable part of a contact (app-owned entries are left to their app)."""
    if not record.is_editable:
        raise ValueError("Bu kişi başka bir uygulamaya ait; silinemez.")
    where = shlex.quote(f"_id={int(record.raw_contact_id)}")
    _run(adb, serial, f"content delete --uri {CONTACTS}/raw_contacts --where {where}")
