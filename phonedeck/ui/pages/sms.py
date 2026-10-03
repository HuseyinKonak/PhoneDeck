"""SMS page: a two-pane messenger (conversation list and chat bubbles)."""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta

from PySide6.QtCore import QModelIndex, QRect, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QKeyEvent, QPainter, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from ...core import telephony
from ...core.device import validate_phone_number
from ...core.messaging import (
    Conversation,
    export_all_sms_csv,
    export_conversation_csv,
    fetch_conversations,
    turkish_fold,
)
from ...core.models import SmsMessage
from ...core.parsers import phone_key
from .. import theme
from ..listing import AVATAR_SIZE, MS_PER_SECOND, Row, RowDelegate, fill_rows, format_timestamp, make_row_list, selected_row
from ..sim import SimPicker
from ..widgets import AppContext, Card, Page, button, label

logger = logging.getLogger(__name__)

COMPOSE_HINT = "Mesaj yaz (Enter: gönder, Shift+Enter: yeni satır)"
CONFIRM_SEND_KEY = "sms/confirm_before_send"
SEND_SETTLE_SECONDS = 2.5

OUTGOING_PREFIX = "Sen: "
LIST_WIDTH = 340
PANE_MIN_HEIGHT = 580
INITIAL_MESSAGES = 60
OLDER_BATCH = 100
BUBBLE_WIDTH_RATIO = 0.72
BUBBLE_MIN_WIDTH_LIMIT = 220
BUBBLE_HORIZONTAL_PADDING = 26
TEXT_SLACK = 2
BADGE_HEIGHT = 18
BADGE_PADDING = 12
BADGE_OVERHANG = 6
SCROLL_SETTLE_MS = 150
COMPOSE_MAX_HEIGHT = 84
MONTHS = (
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)
_FILENAME_UNSAFE = re.compile(r'[\\/:*?"<>|\s]+')
CHAT_STYLE = f"""
#bubbleIn {{ background: {theme.CARD_HOVER}; border: 1px solid {theme.BORDER}; border-radius: 14px; }}
#bubbleOut {{ background: {theme.ACCENT}; border: 1px solid {theme.ACCENT}; border-radius: 14px; }}
#bubbleTextIn, #bubbleTimeIn {{ background: transparent; border: none; }}
#bubbleTextOut, #bubbleTimeOut {{ background: transparent; border: none; color: white; }}
#bubbleTimeIn {{ color: {theme.MUTED}; font-size: 11px; }}
#bubbleTimeOut {{ color: rgba(255, 255, 255, 0.75); font-size: 11px; }}
#daySeparator {{
    background: {theme.BACKGROUND}; color: {theme.MUTED}; border-radius: 10px;
    padding: 3px 12px; font-size: 11px; font-weight: 600;
}}
"""
COMPOSE_STYLE = f"""
QPlainTextEdit {{
    font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif; font-size: 13px;
    background: {theme.BACKGROUND}; border: 1px solid {theme.BORDER};
    border-radius: 12px; padding: 8px 12px;
}}
QPlainTextEdit:focus {{ border-color: {theme.ACCENT}; }}
QPlainTextEdit:disabled {{ color: #565D70; }}
"""


def day_label(day: date, today: date | None = None) -> str:
    """'Bugün', 'Dün' or a long Turkish date."""
    today = today or date.today()
    if day == today:
        return "Bugün"
    if day == today - timedelta(days=1):
        return "Dün"
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def message_day(message: SmsMessage) -> date:
    return datetime.fromtimestamp(message.timestamp_ms / MS_PER_SECOND).date()


def message_clock(message: SmsMessage) -> str:
    return datetime.fromtimestamp(message.timestamp_ms / MS_PER_SECOND).strftime("%H:%M")


def conversation_row(conversation: Conversation) -> Row:
    last = conversation.messages[-1] if conversation.messages else None
    prefix = "" if last is None or last.is_incoming else OUTGOING_PREFIX
    return Row(
        title=conversation.display_name,
        subtitle=prefix + (last.body if last else ""),
        meta=format_timestamp(conversation.last_timestamp_ms) if last else "",
        payload=conversation,
        is_bold=conversation.unread_count > 0,
    )


def search_text(conversation: Conversation) -> str:
    bodies = " ".join(message.body for message in conversation.messages)
    return turkish_fold(f"{conversation.display_name} {conversation.address} {bodies}")


def safe_filename(name: str) -> str:
    return _FILENAME_UNSAFE.sub("_", name).strip("_") or "mesajlar"


class ConversationDelegate(RowDelegate):
    """Row delegate that adds an unread badge on the avatar corner."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        row: Row = index.data(Qt.ItemDataRole.UserRole)
        unread = getattr(row.payload, "unread_count", 0)
        if not unread:
            return
        text = str(unread) if unread < 100 else "99+"
        font = QFont(option.font)
        font.setBold(True)
        font.setPixelSize(11)
        width = QFontMetrics(font).horizontalAdvance(text) + BADGE_PADDING
        rect = option.rect.adjusted(4, 3, -4, -3)
        avatar_right = rect.x() + 10 + AVATAR_SIZE
        avatar_top = rect.center().y() - AVATAR_SIZE // 2 + 1
        pill = QRect(avatar_right + BADGE_OVERHANG - width, avatar_top - BADGE_OVERHANG, width, BADGE_HEIGHT)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.ACCENT))
        painter.drawRoundedRect(pill, BADGE_HEIGHT / 2, BADGE_HEIGHT / 2)
        painter.setFont(font)
        painter.setPen(QColor("white"))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class ComposeEdit(QPlainTextEdit):
    """Enter submits, Shift+Enter inserts a new line."""

    submitted = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        is_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        if is_enter and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit()
            return
        super().keyPressEvent(event)


class ChatView(QScrollArea):
    """Chat bubbles. Only the newest messages are built up front; older ones load on demand."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._messages: tuple[SmsMessage, ...] = ()
        self._first_loaded = 0
        self._bubbles: list[QFrame] = []
        self._texts: list[QLabel] = []
        self._first_separator: QLabel | None = None
        self._older_button = button("Daha eski mesajlar", variant="ghost")
        self._older_button.clicked.connect(self._load_older)
        self._stick_mode = ""
        self._anchor_gap = 0
        self.verticalScrollBar().rangeChanged.connect(self._on_range_changed)
        self._layout = QVBoxLayout()
        self.set_messages(())

    # --- public ------------------------------------------------------------
    def set_messages(self, messages: tuple[SmsMessage, ...]) -> None:
        self._messages = messages
        self._first_loaded = max(len(messages) - INITIAL_MESSAGES, 0)
        self._bubbles = []
        self._texts = []
        self._first_separator = None
        body = QWidget()
        body.setObjectName("scrollBody")
        body.setStyleSheet(CHAT_STYLE)
        self._layout = QVBoxLayout(body)
        self._layout.setContentsMargins(16, 12, 16, 12)
        self._layout.setSpacing(6)
        self._layout.addStretch()
        self._older_button.setParent(body)
        self._older_button.setVisible(self._first_loaded > 0)
        self._layout.addWidget(self._older_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self._stick_mode = "bottom"
        self.setWidget(body)
        self._add_widgets(self._build(self._messages[self._first_loaded :]), self._older_button)
        QTimer.singleShot(SCROLL_SETTLE_MS, self._release_stick)

    # --- building ----------------------------------------------------------
    def _build(self, messages: tuple[SmsMessage, ...]) -> list[QWidget]:
        widgets: list[QWidget] = []
        previous_day: date | None = None
        for message in messages:
            day = message_day(message)
            if day != previous_day:
                separator = self._separator(day)
                widgets.append(separator)
                previous_day = day
            bubble = self._bubble(message)
            widgets.append(bubble)
            self._bubbles.append(bubble)
        if widgets:
            self._first_separator = widgets[0]
        return widgets

    def _add_widgets(self, widgets: list[QWidget], before: QWidget | None = None) -> None:
        """Insert widgets after `before` (or at the end), keeping chat alignment."""
        position = self._layout.count() if before is None else self._layout.indexOf(before) + 1
        for widget in widgets:
            is_incoming = widget.objectName() == "bubbleIn"
            alignment = (
                Qt.AlignmentFlag.AlignLeft if is_incoming
                else Qt.AlignmentFlag.AlignRight if widget.objectName() == "bubbleOut"
                else Qt.AlignmentFlag.AlignHCenter
            )
            self._layout.insertWidget(position, widget, 0, alignment)
            position += 1
        self._apply_bubble_width()

    def _separator(self, day: date) -> QLabel:
        separator = QLabel(day_label(day))
        separator.setObjectName("daySeparator")
        return separator

    def _bubble(self, message: SmsMessage) -> QFrame:
        suffix = "In" if message.is_incoming else "Out"
        bubble = QFrame()
        bubble.setObjectName(f"bubble{suffix}")
        inner = QVBoxLayout(bubble)
        inner.setContentsMargins(12, 8, 12, 6)
        inner.setSpacing(2)
        text = QLabel(message.body)
        text.setObjectName(f"bubbleText{suffix}")
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._texts.append(text)
        clock = QLabel(message_clock(message))
        clock.setObjectName(f"bubbleTime{suffix}")
        clock.setAlignment(Qt.AlignmentFlag.AlignRight)
        inner.addWidget(text)
        inner.addWidget(clock)
        return bubble

    # --- older messages ----------------------------------------------------
    def _load_older(self) -> None:
        if self._first_loaded == 0:
            return
        scrollbar = self.verticalScrollBar()
        self._anchor_gap = scrollbar.maximum() - scrollbar.value()
        self._stick_mode = "anchor"
        start = max(self._first_loaded - OLDER_BATCH, 0)
        previous_first = self._messages[self._first_loaded]
        batch = self._messages[start : self._first_loaded]
        old_separator = self._first_separator
        widgets = self._build(batch)
        if message_day(batch[-1]) == message_day(previous_first) and old_separator is not None:
            self._layout.removeWidget(old_separator)
            old_separator.deleteLater()
        self._first_loaded = start
        self._older_button.setVisible(start > 0)
        self._add_widgets(widgets, self._older_button)
        QTimer.singleShot(SCROLL_SETTLE_MS, self._release_stick)

    # --- scrolling and sizing ----------------------------------------------
    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        scrollbar = self.verticalScrollBar()
        if self._stick_mode == "bottom":
            scrollbar.setValue(maximum)
        elif self._stick_mode == "anchor":
            scrollbar.setValue(max(maximum - self._anchor_gap, 0))

    def _release_stick(self) -> None:
        self._stick_mode = ""

    def _apply_bubble_width(self) -> None:
        width = max(int(self.viewport().width() * BUBBLE_WIDTH_RATIO), BUBBLE_MIN_WIDTH_LIMIT)
        text_width = width - BUBBLE_HORIZONTAL_PADDING
        for bubble in self._bubbles:
            bubble.setMaximumWidth(width)
        for text in self._texts:
            self._fit_text(text, text_width)

    @staticmethod
    def _fit_text(text: QLabel, max_width: int) -> None:
        """Size a wrapped label explicitly; QLabel height-for-width is unreliable in a scroll area."""
        text.ensurePolished()
        flags = int(Qt.TextFlag.TextWordWrap)
        box = text.fontMetrics().boundingRect(0, 0, max_width, 0, flags, text.text())
        text.setFixedSize(min(box.width() + TEXT_SLACK, max_width), box.height() + TEXT_SLACK)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._apply_bubble_width()


class SmsPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Mesajlar", "Konuşmaları oku, telefonda yeni mesaj hazırla")
        self._conversations: tuple[Conversation, ...] = ()
        self._search_index: dict[str, str] = {}
        self._active: Conversation | None = None
        self._can_reply = False
        self._build_header()
        panes = QHBoxLayout()
        panes.setSpacing(16)
        panes.addLayout(self._build_list_pane())
        panes.addWidget(self._build_chat_pane(), 1)
        holder = QWidget()
        holder.setMinimumHeight(PANE_MIN_HEIGHT)
        holder.setLayout(panes)
        panes.setContentsMargins(0, 0, 0, 0)
        self.content.addWidget(holder, 1)
        self._show_conversation(None)

    # --- layout ------------------------------------------------------------
    def _build_header(self) -> None:
        new_message = button("Yeni mesaj", theme.Glyph.ADD, "primary", icon_color="white")
        new_message.clicked.connect(self._new_message)
        export_all = button("Tümünü aktar", theme.Glyph.DOWNLOAD)
        export_all.setToolTip("Yüklü tüm mesajları CSV dosyasına aktarır")
        export_all.clicked.connect(self._export_all)
        reload = button("Yenile", theme.Glyph.REFRESH)
        reload.clicked.connect(lambda: self.refresh())
        for widget in (new_message, export_all, reload):
            self.header.addWidget(widget)

    def _build_list_pane(self) -> QVBoxLayout:
        pane = QVBoxLayout()
        pane.setSpacing(10)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Kişi, numara veya mesaj içinde ara…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(LIST_WIDTH)
        self._search.textChanged.connect(lambda _text: self._render_list())
        self._list = make_row_list()
        self._list.setItemDelegate(ConversationDelegate(self._list))
        self._list.setMinimumHeight(0)
        self._list.setFixedWidth(LIST_WIDTH)
        self._list.currentItemChanged.connect(lambda _current, _previous: self._open_selected())
        pane.addWidget(self._search)
        pane.addWidget(self._list, 1)
        return pane

    def _build_chat_pane(self) -> Card:
        card = Card(spacing=10)
        card.body.setContentsMargins(16, 14, 16, 14)
        top = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self._chat_title = label("Konuşma seç", "sectionTitle")
        self._chat_subtitle = label("Soldaki listeden bir konuşma aç.", "muted")
        titles.addWidget(self._chat_title)
        titles.addWidget(self._chat_subtitle)
        self._call_button = button("Ara", theme.Glyph.CALL, icon_color=theme.SUCCESS)
        self._call_button.clicked.connect(self._call)
        self._export_button = button("CSV", theme.Glyph.DOWNLOAD, tooltip="Bu konuşmayı CSV olarak kaydet")
        self._export_button.clicked.connect(self._export_active)
        top.addLayout(titles, 1)
        top.addWidget(self._call_button)
        top.addWidget(self._export_button)
        self._chat = ChatView()
        self._compose = ComposeEdit()
        self._compose.setStyleSheet(COMPOSE_STYLE)
        self._compose.setMaximumHeight(COMPOSE_MAX_HEIGHT)
        self._compose.setPlaceholderText(COMPOSE_HINT)
        self._compose.submitted.connect(self._send_now)
        self._send = button("Gönder", theme.Glyph.SEND, "primary", icon_color="white")
        self._send.setToolTip("Mesajı seçili SIM üzerinden hemen gönderir")
        self._send.clicked.connect(self._send_now)
        # Short captions keep the options row inside the pane on the default window width.
        self._prepare_button = button("Hazırla", theme.Glyph.PHONE)
        self._prepare_button.setToolTip(
            "Mesajı telefonun SMS uygulamasında hazırlar; göndermek için canlı ekranda Gönder'e bas"
        )
        self._prepare_button.clicked.connect(self._prepare)
        self._sim = SimPicker()
        self._confirm = QCheckBox("Önce sor")
        self._confirm.setToolTip("Mesajı göndermeden önce onay iste")
        self._confirm.setChecked(QSettings().value(CONFIRM_SEND_KEY, True, type=bool))
        self._confirm.toggled.connect(lambda is_on: QSettings().setValue(CONFIRM_SEND_KEY, is_on))
        self._counter = label("", "muted")
        self._compose.textChanged.connect(self._update_counter)
        options = QHBoxLayout()
        options.setSpacing(10)
        options.addWidget(self._sim)
        options.addWidget(self._confirm)
        options.addWidget(self._counter)
        options.addStretch()
        options.addWidget(self._prepare_button)
        options.addWidget(self._send)
        card.body.addLayout(top)
        card.body.addWidget(self._chat, 1)
        card.body.addWidget(self._compose)
        card.body.addLayout(options)
        return card

    # --- data ----------------------------------------------------------------
    def on_shown(self) -> None:
        if not self._conversations:
            self.refresh()

    def refresh(self) -> None:
        service = self.service
        if service is None:
            return
        adb = self.context.adb
        self.run(lambda: fetch_conversations(adb, service.serial), self._show)

    def _show(self, conversations: tuple[Conversation, ...]) -> None:
        self._conversations = conversations
        self._search_index = {c.thread_id: search_text(c) for c in conversations}
        self._render_list()
        if self._active is not None and self._active.thread_id:
            fresh = next((c for c in conversations if c.thread_id == self._active.thread_id), None)
            if fresh is not None:
                self._show_conversation(fresh)

    def _render_list(self) -> None:
        query = turkish_fold(self._search.text().strip())
        wanted = self._active.thread_id if self._active else ""
        rows = [
            conversation_row(c)
            for c in self._conversations
            if not query or query in self._search_index.get(c.thread_id, "")
        ]
        blocked = self._list.blockSignals(True)
        fill_rows(self._list, rows)
        self._reselect(wanted)
        self._list.blockSignals(blocked)

    def _reselect(self, thread_id: str) -> None:
        for index in range(self._list.count()):
            row: Row = self._list.item(index).data(Qt.ItemDataRole.UserRole)
            if thread_id and row.payload.thread_id == thread_id:
                self._list.setCurrentRow(index)
                return

    # --- chat pane -------------------------------------------------------------
    def _open_selected(self) -> None:
        row = selected_row(self._list)
        if row is not None:
            self._show_conversation(row.payload)

    def _show_conversation(self, conversation: Conversation | None) -> None:
        self._active = conversation
        self._can_reply = False
        if conversation is not None:
            try:
                validate_phone_number(conversation.address)
                self._can_reply = True
            except ValueError:
                logger.info("conversation address is not a dialable number")
        has_chat = conversation is not None
        self._chat_title.setText(conversation.display_name if conversation else "Konuşma seç")
        self._chat_subtitle.setText(self._subtitle_for(conversation))
        self._chat.set_messages(conversation.messages if conversation else ())
        for widget in (self._call_button, self._compose, self._send, self._prepare_button):
            widget.setEnabled(self._can_reply)
        self._export_button.setEnabled(bool(has_chat and conversation.messages))
        self._compose.setPlaceholderText(
            COMPOSE_HINT
            if self._can_reply or not has_chat
            else "Bu gönderene telefondan yanıt verilemez"
        )

    @staticmethod
    def _subtitle_for(conversation: Conversation | None) -> str:
        if conversation is None:
            return "Soldaki listeden bir konuşma aç."
        count = len(conversation.messages)
        number = conversation.address if conversation.address != conversation.display_name else ""
        return " · ".join(part for part in (number, f"{count} mesaj" if count else "Yeni konuşma") if part)

    # --- actions ---------------------------------------------------------------
    def _new_message(self) -> None:
        text, accepted = QInputDialog.getText(self, "Yeni mesaj", "Telefon numarası:")
        if not accepted:
            return
        try:
            number = validate_phone_number(text)
        except ValueError as exc:
            self.context.notify(str(exc), "error")
            return
        existing = next(
            (c for c in self._conversations if phone_key(c.address) and phone_key(c.address) == phone_key(number)),
            None,
        )
        self._search.clear()
        self._show_conversation(existing or Conversation("", number, number, (), 0, 0))
        if existing is not None:
            self._reselect(existing.thread_id)
        self._compose.setFocus()

    def _prepare(self) -> None:
        conversation, service = self._active, self.service
        text = self._compose.toPlainText()
        if conversation is None or service is None or not self._can_reply:
            return
        if not text.strip():
            self.context.notify("Mesaj metni boş.", "error")
            return
        self.run(
            lambda: service.compose_sms(conversation.address, text),
            lambda _result: self._compose.clear(),
            success="Mesaj telefonda hazır; canlı ekranda Gönder'e bas",
        )

    def set_service(self, service) -> None:
        super().set_service(service)
        if service is not None:
            self._sim.load(self.context.adb, service.serial)

    def _update_counter(self) -> None:
        length = len(self._compose.toPlainText())
        self._counter.setText(f"{length} karakter" if length else "")

    def _send_now(self) -> None:
        """Send the message straight from the chosen SIM, then report what the phone recorded."""
        conversation, service = self._active, self.service
        text = self._compose.toPlainText()
        if conversation is None or service is None or not self._can_reply:
            return
        if not text.strip():
            self.context.notify("Mesaj metni boş.", "error")
            return
        sub_id = self._sim.sms_sub_id()
        sim = self._sim.state.by_id(sub_id)
        sim_text = sim.label if sim else "varsayılan SIM"
        if self._confirm.isChecked():
            answer = QMessageBox.question(
                self,
                "SMS gönder",
                f"Alıcı: {conversation.display_name} ({conversation.address})\n"
                f"Hat: {sim_text}\n{len(text)} karakter\n\nMesaj şimdi gönderilsin mi? "
                "Operatörün ücret uygulayabilir.",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        adb, serial, address = self.context.adb, service.serial, conversation.address

        def send() -> str:
            telephony.send_sms(adb, serial, sub_id, address, text)
            time.sleep(SEND_SETTLE_SECONDS)
            return telephony.last_sms_state(adb, serial, address)

        def done(state: str) -> None:
            self._compose.clear()
            kind = "error" if state == telephony.SMS_STATES["5"] else "success"
            self.context.notify(f"SMS {state or 'gönderime verildi'} · {sim_text}", kind)
            self.refresh()

        self.context.notify("SMS gönderiliyor…", "info")
        self.run(send, done)

    def _call(self) -> None:
        conversation, service = self._active, self.service
        if conversation is None or service is None or not self._can_reply:
            return
        sub_id = self._sim.sub_id()
        sim = self._sim.current_sim()
        answer = QMessageBox.question(
            self,
            "Ara",
            f"{conversation.display_name} ({conversation.address}) aransın mı?\n"
            f"Hat: {sim.label if sim else 'telefonun seçimi'}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        adb, serial, address = self.context.adb, service.serial, conversation.address
        if sub_id == telephony.DEFAULT_SUBSCRIPTION:
            self.run(lambda: service.dial(address), success="Arama başlatıldı")
        else:
            self.run(
                lambda: telephony.place_call(adb, serial, sub_id, address), success="Arama başlatıldı"
            )

    def _export_active(self) -> None:
        conversation = self._active
        if conversation is None or not conversation.messages:
            return
        name = f"mesajlar_{safe_filename(conversation.display_name)}.csv"
        path = self._ask_save_path(name)
        if path:
            self.run(
                lambda: export_conversation_csv(conversation, path),
                lambda count: self.context.notify(f"{count} mesaj dışa aktarıldı", "success"),
            )

    def _export_all(self) -> None:
        conversations = self._conversations
        if not conversations:
            self.context.notify("Aktarılacak mesaj yok.", "error")
            return
        path = self._ask_save_path("tum_mesajlar.csv")
        if path:
            self.run(
                lambda: export_all_sms_csv(conversations, path),
                lambda count: self.context.notify(f"{count} mesaj dışa aktarıldı", "success"),
            )

    def _ask_save_path(self, default_name: str) -> str:
        path, _filter = QFileDialog.getSaveFileName(
            self, "CSV olarak kaydet", default_name, "CSV dosyası (*.csv)"
        )
        return path
