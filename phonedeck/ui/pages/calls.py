"""Calls page: recent call log, dialing, answering and hanging up."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QMessageBox

from ...core import telephony
from ...core.device import validate_phone_number
from ...core.models import CallEntry
from ...core.telephony import call_subscriptions
from .. import theme
from ..listing import Row, fill_rows, format_timestamp, make_row_list
from ..sim import SimPicker
from ..widgets import AppContext, Card, Page, button, label

SECONDS_PER_MINUTE = 60
# Android CallLog.Calls.TYPE values -> (label, avatar color)
CALL_KINDS = {
    1: ("Gelen", theme.SUCCESS),
    2: ("Giden", theme.ACCENT_ALT),
    3: ("Cevapsız", theme.DANGER),
    5: ("Reddedildi", theme.WARNING),
    6: ("Engellendi", theme.MUTED),
}
UNKNOWN_KIND = ("Diğer", theme.MUTED)


def format_duration(seconds: int) -> str:
    minutes, rest = divmod(seconds, SECONDS_PER_MINUTE)
    return f"{minutes} dk {rest} sn" if minutes else f"{rest} sn"


class CallsPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Aramalar", "Arama geçmişi, numara çevirme ve çağrı kontrolü")
        reload = button("Yenile", theme.Glyph.REFRESH)
        reload.clicked.connect(lambda: self.refresh())
        self.header.addWidget(reload)

        self._sim = SimPicker()
        dialer = Card()
        dialer.body.addWidget(label("Numara Çevir", "sectionTitle"))
        line = QHBoxLayout()
        line.setSpacing(10)
        self._number = QLineEdit()
        self._number.setPlaceholderText("Telefon numarası (listeden seç ya da yaz)")
        self._number.returnPressed.connect(self._dial)
        line.addWidget(self._number, 1)
        line.addWidget(self._sim)
        for text, glyph, variant, color, handler in (
            ("Ara", theme.Glyph.CALL, "primary", "white", self._dial),
            ("Cevapla", theme.Glyph.CALL, "", theme.SUCCESS, self._answer),
            ("Kapat", theme.Glyph.CLOSE, "danger", theme.DANGER, self._hang_up),
        ):
            action = button(text, glyph, variant, icon_color=color)
            action.clicked.connect(handler)
            line.addWidget(action)
        dialer.body.addLayout(line)
        self.content.addWidget(dialer)

        self.content.addWidget(label("Son Aramalar", "sectionTitle"))
        self._list = make_row_list()
        self._list.itemClicked.connect(self._pick)
        self.content.addWidget(self._list, 1)

    # --- data ---------------------------------------------------------------
    def set_service(self, service) -> None:
        super().set_service(service)
        if service is not None:
            self._sim.load(self.context.adb, service.serial)

    def on_shown(self) -> None:
        if self._list.count() == 0:
            self.refresh()

    def refresh(self) -> None:
        service = self.service
        if service is None:
            return
        adb = self.context.adb

        def load() -> tuple[tuple[CallEntry, ...], dict[int, int]]:
            return service.call_log(), call_subscriptions(adb, service.serial)

        self.run(load, lambda result: self._show(*result))

    def _sim_text(self, sub_id: int | None) -> str:
        sim = self._sim.state.by_id(sub_id) if sub_id is not None else None
        return f"SIM {sim.slot + 1}" if sim and len(self._sim.state.sims) > 1 else ""

    def _show(self, calls: tuple[CallEntry, ...], subscriptions: dict[int, int]) -> None:
        rows = []
        for call in calls:
            kind, color = CALL_KINDS.get(call.kind, UNKNOWN_KIND)
            detail = f"{kind} · {format_duration(call.duration_s)}" if call.duration_s else kind
            sim_text = self._sim_text(subscriptions.get(call.timestamp_ms))
            if sim_text:
                detail = f"{detail} · {sim_text}"
            title = call.name or call.number or "Gizli numara"
            rows.append(
                Row(
                    title=title,
                    subtitle=f"{call.number} · {detail}" if call.name else detail,
                    meta=format_timestamp(call.timestamp_ms),
                    payload=call,
                    color=color,
                )
            )
        fill_rows(self._list, rows)

    def _pick(self, item) -> None:
        call: CallEntry = item.data(Qt.ItemDataRole.UserRole).payload
        if call.number:
            self._number.setText(call.number)

    # --- actions ------------------------------------------------------------
    def _dial(self) -> None:
        try:
            number = validate_phone_number(self._number.text())
        except ValueError as exc:
            self.context.notify(str(exc), "error")
            return
        sub_id = self._sim.sub_id()
        sim = self._sim.current_sim()
        line = sim.label if sim else "telefonun seçimi"
        answer = QMessageBox.question(self, "Ara", f"{number} aransın mı?\nHat: {line}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        adb, service = self.context.adb, self.service
        if sub_id == telephony.DEFAULT_SUBSCRIPTION:
            self.run(lambda: service.dial(number), success=f"{number} aranıyor")
        else:
            self.run(
                lambda: telephony.place_call(adb, service.serial, sub_id, number),
                success=f"{number} aranıyor · {line}",
            )

    def _answer(self) -> None:
        self.run(self.service.answer_call)

    def _hang_up(self) -> None:
        self.run(self.service.end_call, success="Çağrı sonlandırıldı")
