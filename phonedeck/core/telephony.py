"""Actions done by the on-phone helper: SIM cards, direct SMS, SIM-specific calls, clearing notifications."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass

from .adb import Adb, AdbError
from .device import APP_LISTER_ASSET, REMOTE_APP_LISTER, validate_phone_number

DEFAULT_SUBSCRIPTION = -1
MAX_SMS_CHARACTERS = 1600
HELPER_TIMEOUT = 60
RGB_MASK = 0xFFFFFF
# Values of the `type` column in the SMS provider (Telephony.TextBasedSmsColumns).
SMS_STATES = {
    "2": "gönderildi",
    "4": "gönderiliyor",
    "5": "gönderilemedi",
    "6": "kuyrukta",
}
_SIM_LINE = re.compile(
    r"SubscriptionInfoInternal: id=(?P<id>\d+) .*?simSlotIndex=(?P<slot>-?\d+) .*?"
    r"displayName=(?P<name>.*?) carrierName=(?P<carrier>.*?) isOpportunistic=.*?"
    r"iconTint=(?P<tint>-?\d+)"
)


@dataclass(frozen=True)
class Sim:
    sub_id: int
    slot: int
    display_name: str
    carrier: str
    color: str

    @property
    def label(self) -> str:
        return f"SIM {self.slot + 1} · {self.carrier or self.display_name}"


@dataclass(frozen=True)
class SimState:
    """Inserted SIMs (by slot) and the phone's default SIM per purpose (-1 = ask / none)."""

    sims: tuple[Sim, ...] = ()
    default_voice: int = DEFAULT_SUBSCRIPTION
    default_sms: int = DEFAULT_SUBSCRIPTION
    default_data: int = DEFAULT_SUBSCRIPTION

    def by_id(self, sub_id: int) -> Sim | None:
        return next((sim for sim in self.sims if sim.sub_id == sub_id), None)


def _default(name: str, output: str) -> int:
    match = re.search(rf"{name}=(-?\d+)", output)
    return int(match.group(1)) if match else DEFAULT_SUBSCRIPTION


def parse_sims(output: str) -> SimState:
    """Parse `dumpsys isub`: only subscriptions that sit in a slot are active SIMs."""
    sims: dict[int, Sim] = {}
    for match in _SIM_LINE.finditer(output):
        slot = int(match.group("slot"))
        sub_id = int(match.group("id"))
        if slot < 0 or sub_id in sims:
            continue
        sims[sub_id] = Sim(
            sub_id=sub_id,
            slot=slot,
            display_name=match.group("name").strip(),
            carrier=match.group("carrier").strip(),
            color=f"#{int(match.group('tint')) & RGB_MASK:06X}",
        )
    return SimState(
        sims=tuple(sorted(sims.values(), key=lambda sim: sim.slot)),
        default_voice=_default("defaultVoiceSubId", output),
        default_sms=_default("defaultSmsSubId", output),
        default_data=_default("defaultDataSubId", output),
    )


def read_sims(adb: Adb, serial: str) -> SimState:
    return parse_sims(adb.shell(serial, "dumpsys isub | grep -E 'SubscriptionInfoInternal|default[A-Za-z]*SubId='"))


def _encode(value: str) -> str:
    """URL-safe Base64, so text of any content reaches the helper without shell quoting."""
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii")


def _run_helper(adb: Adb, serial: str, arguments: str) -> str:
    """Run PhoneTool on the phone; returns its message or raises with its error."""
    if not APP_LISTER_ASSET.is_file():
        raise AdbError(f"applister.jar dosyası eksik: {APP_LISTER_ASSET}")
    adb.run(["push", str(APP_LISTER_ASSET), REMOTE_APP_LISTER], serial, HELPER_TIMEOUT)
    try:
        output = adb.shell(
            serial,
            f"CLASSPATH={REMOTE_APP_LISTER} app_process / phonedeck.PhoneTool {arguments}",
            timeout=HELPER_TIMEOUT,
        )
    except AdbError as exc:
        output = str(exc)  # The helper exits non-zero on failure; its line is still the message.
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    result = next((line for line in reversed(lines) if line.startswith(("OK ", "ERROR "))), "")
    if result.startswith("OK "):
        return result[3:]
    raise AdbError(result[6:] if result else (lines[-1] if lines else "Yardımcı program yanıt vermedi."))


def send_sms(adb: Adb, serial: str, sub_id: int, number: str, text: str, is_dry_run: bool = False) -> str:
    """Send an SMS right away through the given SIM. Long texts go out as several parts."""
    target = validate_phone_number(number)
    if not text.strip():
        raise ValueError("Mesaj metni boş.")
    if len(text) > MAX_SMS_CHARACTERS:
        raise ValueError(f"Mesaj çok uzun (en fazla {MAX_SMS_CHARACTERS} karakter).")
    suffix = " dry" if is_dry_run else ""
    return _run_helper(adb, serial, f"sms {int(sub_id)} {_encode(target)} {_encode(text)}{suffix}")


def place_call(adb: Adb, serial: str, sub_id: int, number: str, is_dry_run: bool = False) -> str:
    """Dial through the given SIM (`DEFAULT_SUBSCRIPTION` lets the phone decide)."""
    target = validate_phone_number(number)
    suffix = " dry" if is_dry_run else ""
    return _run_helper(adb, serial, f"call {int(sub_id)} {_encode(target)}{suffix}")


def clear_notifications(adb: Adb, serial: str, is_dry_run: bool = False) -> str:
    """Dismiss every clearable notification on the phone (ongoing ones stay)."""
    return _run_helper(adb, serial, "clearnotifications" + (" dry" if is_dry_run else ""))


def parse_call_subscriptions(output: str) -> dict[int, int]:
    """Map call timestamp (ms) -> subscription id from `date:subscription_id` rows."""
    pairs = re.findall(r"date=(\d+), subscription_id=(-?\d+)", output)
    return {int(date): int(sub_id) for date, sub_id in pairs}


def call_subscriptions(adb: Adb, serial: str, limit: int = 200) -> dict[int, int]:
    """Which SIM each recent call used, keyed by the call's timestamp."""
    output = adb.shell(
        serial,
        "content query --uri content://call_log/calls --projection date:subscription_id "
        f"--sort 'date DESC LIMIT {int(limit)}'",
    )
    return parse_call_subscriptions(output)


def last_sms_state(adb: Adb, serial: str, number: str) -> str:
    """State of the newest message to `number` as the phone records it ("" if unknown)."""
    target = validate_phone_number(number)
    output = adb.shell(
        serial,
        "content query --uri content://sms --projection type "
        f"--where \"address='{target}'\" --sort 'date DESC LIMIT 1'",
    )
    match = re.search(r"type=(\d+)", output)
    return SMS_STATES.get(match.group(1), "") if match else ""
