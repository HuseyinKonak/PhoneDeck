"""Contacts page: browse, add, edit and delete contacts; call or message them."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
)

from ...core import contacts, telephony
from ...core.contacts import Account, ContactDraft, ContactRecord
from ...core.device import validate_phone_number
from .. import theme
from ..listing import Row, fill_rows, make_row_list, selected_row
from ..sim import SimPicker
from ..widgets import AppContext, Card, Page, button, label

DETAILS_MIN_WIDTH = 320
FIELD_HEIGHT = 84
DIALOG_WIDTH = 440


@dataclass(frozen=True)
class AddressBook:
    records: tuple[ContactRecord, ...]
    accounts: tuple[Account, ...]


class ContactDialog(QDialog):
    """Form for a new contact or for editing an existing one."""

    def __init__(self, parent, accounts: tuple[Account, ...], record: ContactRecord | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Kişiyi düzenle" if record else "Yeni kişi")
        self.setMinimumWidth(DIALOG_WIDTH)
        self.draft: ContactDraft | None = None
        self._name = QLineEdit(record.name if record else "")
        self._numbers = self._multiline("\n".join(record.numbers) if record else "")
        self._emails = self._multiline("\n".join(record.emails) if record else "")
        self._account = QComboBox()
        for account in accounts:
            self._account.addItem(f"{account.label}  ({account.type})", account)
        self._error = label("", "muted")
        self._error.setWordWrap(True)

        form = QFormLayout()
        form.setSpacing(12)
        form.addRow("Ad", self._name)
        form.addRow("Telefonlar\n(her satıra bir tane)", self._numbers)
        form.addRow("E-postalar\n(her satıra bir tane)", self._emails)
        if record is None:
            form.addRow("Kaydedilecek hesap", self._account)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(self._error)
        layout.addWidget(buttons)

    @staticmethod
    def _multiline(text: str) -> QPlainTextEdit:
        field = QPlainTextEdit(text)
        field.setFixedHeight(FIELD_HEIGHT)
        field.setStyleSheet(f"font-family: 'Segoe UI'; font-size: 13px; background: {theme.BACKGROUND};")
        return field

    def account(self) -> Account | None:
        return self._account.currentData()

    def _accept(self) -> None:
        try:
            self.draft = contacts.validate_draft(
                self._name.text(),
                self._numbers.toPlainText().splitlines(),
                self._emails.toPlainText().splitlines(),
            )
        except ValueError as exc:
            self._error.setText(str(exc))
            self._error.setStyleSheet(f"color: {theme.DANGER};")
            return
        self.accept()


class ContactsPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Rehber", "Kişileri ara, ekle, düzenle, sil; ara veya mesaj gönder")
        self._book = AddressBook((), ())
        self._count = label("", "muted")
        self._search = QLineEdit()
        self._list = make_row_list()
        self._sim = SimPicker()
        self._title = label("Kişi seç", "sectionTitle")
        self._subtitle = label("Ayrıntıları görmek için listeden bir kişi seç.", "muted")
        self._numbers = QListWidget()
        self._emails = label("", "muted")
        self._actions: dict[str, object] = {}
        self._build_header()
        self._build_body()
        self._show_details(None)

    # --- layout -------------------------------------------------------------
    def _build_header(self) -> None:
        add = button("Yeni Kişi", theme.Glyph.ADD, "primary", icon_color="white")
        add.clicked.connect(self._add)
        export = button("CSV", theme.Glyph.DOWNLOAD, tooltip="Tüm kişileri CSV dosyasına aktar")
        export.clicked.connect(self._export)
        reload = button("Yenile", theme.Glyph.REFRESH)
        reload.clicked.connect(lambda: self.refresh())
        self.header.addWidget(self._count)
        self.header.addSpacing(12)
        for widget in (add, export, reload):
            self.header.addWidget(widget)

    def _build_body(self) -> None:
        self._search.setPlaceholderText("Ad, numara veya e-posta ara…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _text: self._render())
        self.content.addWidget(self._search)
        self._list.setMinimumHeight(420)
        self._list.currentItemChanged.connect(
            lambda _current, _previous: self._show_details(self._selected())
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._list)
        splitter.addWidget(self._build_details())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.content.addWidget(splitter, 1)

    def _build_details(self) -> Card:
        card = Card()
        card.setMinimumWidth(DETAILS_MIN_WIDTH)
        self._title.setWordWrap(True)
        self._subtitle.setWordWrap(True)
        self._emails.setWordWrap(True)
        self._emails.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._numbers.setMinimumHeight(110)
        card.body.addWidget(self._title)
        card.body.addWidget(self._subtitle)
        card.body.addWidget(label("Numaralar", "muted"))
        card.body.addWidget(self._numbers, 1)
        card.body.addWidget(self._emails)
        card.body.addWidget(self._sim)
        rows = (
            (("call", "Ara", theme.Glyph.CALL, "primary", self._call),
             ("message", "Mesaj Hazırla", theme.Glyph.MESSAGE, "", self._message)),
            (("copy", "Numarayı Kopyala", theme.Glyph.COPY, "", self._copy),
             ("edit", "Düzenle", theme.Glyph.RENAME, "", self._edit)),
            (("delete", "Sil", theme.Glyph.DELETE, "danger", self._delete),),
        )
        for row in rows:
            line = QHBoxLayout()
            line.setSpacing(8)
            for key, text, glyph, variant, handler in row:
                color = "white" if variant == "primary" else theme.DANGER if variant == "danger" else theme.TEXT
                control = button(text, glyph, variant, icon_color=color)
                control.clicked.connect(lambda _checked=False, h=handler: h())
                self._actions[key] = control
                line.addWidget(control)
            line.addStretch()
            card.body.addLayout(line)
        return card

    # --- data ---------------------------------------------------------------
    def set_service(self, service) -> None:
        if service is not self.service:
            self._book = AddressBook((), ())
            self._list.clear()
            self._show_details(None)
        super().set_service(service)
        if service is not None:
            self._sim.load(self.context.adb, service.serial)

    def on_shown(self) -> None:
        if not self._book.records:
            self.refresh()

    def refresh(self) -> None:
        service = self.service
        if service is None:
            return
        adb, serial = self.context.adb, service.serial
        if not self._book.records:
            self._count.setText("Yükleniyor…")
        self.run(
            lambda: AddressBook(contacts.list_contacts(adb, serial), contacts.list_accounts(adb, serial)),
            self._show,
        )

    def _show(self, book: AddressBook) -> None:
        self._book = book
        self._render()

    def _render(self) -> None:
        query = self._search.text().strip().casefold()
        selected = self._selected()
        rows = []
        for record in self._book.records:
            haystack = " ".join((record.name, *record.numbers, *record.emails)).casefold()
            if query and query not in haystack:
                continue
            extra = f"  +{len(record.numbers) - 1}" if len(record.numbers) > 1 else ""
            subtitle = (record.numbers[0] + extra) if record.numbers else ", ".join(record.emails)
            rows.append(Row(record.name, subtitle, "" if record.is_editable else "salt okunur", payload=record))
        fill_rows(self._list, rows)
        self._count.setText(f"{len(rows)} kişi")
        if selected is not None:
            for index, row in enumerate(rows):
                if row.payload.contact_id == selected.contact_id:
                    self._list.setCurrentRow(index)
                    break

    def _selected(self) -> ContactRecord | None:
        row = selected_row(self._list)
        return row.payload if row is not None else None

    def _show_details(self, record: ContactRecord | None) -> None:
        self._numbers.clear()
        has_number = bool(record and record.numbers)
        states = {
            "call": has_number, "message": has_number, "copy": has_number,
            "edit": bool(record and record.is_editable),
            "delete": bool(record and record.is_editable),
        }
        for key, is_enabled in states.items():
            self._actions[key].setEnabled(is_enabled)
        if record is None:
            self._title.setText("Kişi seç")
            self._subtitle.setText("Ayrıntıları görmek için listeden bir kişi seç.")
            self._emails.setText("")
            return
        self._title.setText(record.name)
        self._subtitle.setText(
            "Telefonda kayıtlı kişi" if record.is_editable
            else "Başka bir uygulamanın kişisi; buradan düzenlenemez"
        )
        self._numbers.addItems(record.numbers)
        if record.numbers:
            self._numbers.setCurrentRow(0)
        self._emails.setText("\n".join(record.emails))

    def _number(self) -> str | None:
        """The highlighted number of the selected contact, validated for dialing."""
        item = self._numbers.currentItem()
        if item is None:
            self.context.notify("Önce bir numara seç.", "error")
            return None
        try:
            return validate_phone_number(item.text())
        except ValueError as exc:
            self.context.notify(str(exc), "error")
            return None

    # --- communication ------------------------------------------------------
    def _call(self) -> None:
        record, number = self._selected(), self._number()
        if record is None or number is None:
            return
        sub_id, sim = self._sim.sub_id(), self._sim.current_sim()
        line = sim.label if sim else "telefonun seçimi"
        answer = QMessageBox.question(self, "Ara", f"{record.name} ({number}) aransın mı?\nHat: {line}")
        if answer != QMessageBox.StandardButton.Yes:
            return
        adb, service = self.context.adb, self.service
        if sub_id == telephony.DEFAULT_SUBSCRIPTION:
            self.run(lambda: service.dial(number), success=f"{record.name} aranıyor")
        else:
            self.run(
                lambda: telephony.place_call(adb, service.serial, sub_id, number),
                success=f"{record.name} aranıyor · {line}",
            )

    def _message(self) -> None:
        number = self._number()
        if number is not None:
            service = self.service
            self.run(
                lambda: service.compose_sms(number, ""),
                success="Mesaj ekranı telefonda açıldı; doğrudan göndermek için Mesajlar sayfasını kullan",
            )

    def _copy(self) -> None:
        item = self._numbers.currentItem()
        if item is not None:
            QGuiApplication.clipboard().setText(item.text())
            self.context.notify("Numara panoya kopyalandı", "success")

    # --- editing ------------------------------------------------------------
    def _add(self) -> None:
        if self.service is None:
            return
        if not self._book.accounts:
            self.context.notify("Kişi kaydedilecek hesap bulunamadı; önce rehberi yenile.", "error")
            return
        dialog = ContactDialog(self, self._book.accounts)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.draft is None:
            return
        draft, account = dialog.draft, dialog.account()
        adb, serial = self.context.adb, self.service.serial
        self.run(
            lambda: contacts.add_contact(adb, serial, draft, account),
            lambda _result: self.refresh(),
            success=f"{draft.name} eklendi · {account.label}",
        )

    def _edit(self) -> None:
        record = self._selected()
        if record is None or not record.is_editable:
            return
        dialog = ContactDialog(self, self._book.accounts, record)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.draft is None:
            return
        draft = dialog.draft
        adb, serial = self.context.adb, self.service.serial
        self.run(
            lambda: contacts.update_contact(adb, serial, record, draft),
            lambda _result: self.refresh(),
            success=f"{draft.name} güncellendi",
        )

    def _delete(self) -> None:
        record = self._selected()
        if record is None or not record.is_editable:
            return
        answer = QMessageBox.warning(
            self,
            "Kişiyi sil",
            f"{record.name}\n{', '.join(record.numbers)}\n\nBu kişi telefondan silinsin mi? "
            "Hesap eşitlemesi açıksa buluttaki rehberden de silinir.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        adb, serial = self.context.adb, self.service.serial
        self.run(
            lambda: contacts.delete_contact(adb, serial, record),
            lambda _result: self.refresh(),
            success=f"{record.name} silindi",
        )

    def _export(self) -> None:
        records = self._book.records
        if not records:
            self.context.notify("Aktarılacak kişi yok.", "error")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Kişileri aktar", "rehber.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            count = contacts.export_csv(records, path)
        except OSError as exc:
            self.context.notify(f"Kaydedilemedi: {exc}", "error")
            return
        self.context.notify(f"{count} kişi kaydedildi: {path}", "success")
