"""Network page: Wi-Fi status and switch, nearby and saved networks, mobile network, connection test."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ...core import wifi
from ...core.adb import Adb, AdbError
from .. import theme
from ..listing import ROW_HEIGHT, Row, fill_rows, make_row_list, selected_row
from ..widgets import AppContext, Card, Page, ToggleSwitch, button, label

POLL_INTERVAL_MS = 5_000
MOBILE_POLL_EVERY = 3
SCAN_WAIT_S = 3
SETTLE_MS = 3_000
LIST_ROWS = 4
LIST_PADDING = 8
STATUS_COLUMNS = 3
BARS_TOTAL = wifi.MAX_SIGNAL_BARS
BAR_WIDTH_RATIO = 0.7
BAR_RADIUS = 2
BAR_INACTIVE = "#2C3344"
STATUS_BARS_SIZE = 20
DIALOG_MIN_WIDTH = 380
DEFAULT_TEST_HOST = "8.8.8.8"
MISSING = "—"
LEVEL_NAMES = ("Yok", "Zayıf", "Orta", "İyi", "Çok iyi")

# (key, title) of the cells in the status card, filled row by row.
STATUS_FIELDS = (
    ("ssid", "Ağ adı"),
    ("bssid", "BSSID"),
    ("band", "Bant"),
    ("speed", "Bağlantı hızı"),
    ("signal", "Sinyal"),
    ("ip", "IP adresi"),
    ("gateway", "Ağ geçidi"),
    ("dns", "DNS sunucuları"),
    ("mac", "MAC adresi"),
)


@dataclass(frozen=True)
class Snapshot:
    """What one refresh read from the phone; `None` parts were not requested or failed."""

    status: wifi.WifiStatus
    mobile: tuple[wifi.MobileLine, ...] | None = None
    saved: tuple[wifi.SavedNetwork, ...] | None = None
    scan: tuple[wifi.ScanResult, ...] | None = None


# --- drawing and text helpers -------------------------------------------------------
def bars_pixmap(level: int, size: int, ratio: float) -> QPixmap:
    """Four rising signal bars, the first `level` of them lit."""
    pixmap = QPixmap(round(size * ratio), round(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    step = size / BARS_TOTAL
    for index in range(BARS_TOTAL):
        height = size * (0.3 + 0.7 * (index + 1) / BARS_TOTAL)
        width = step * BAR_WIDTH_RATIO
        rect = QRectF(index * step + (step - width) / 2, size - height, width, height)
        painter.setBrush(QColor(theme.ACCENT_ALT if index < level else BAR_INACTIVE))
        painter.drawRoundedRect(rect, BAR_RADIUS, BAR_RADIUS)
    painter.end()
    return pixmap


def scan_key(row: Row) -> str:
    """Identity of a scan row across refreshes: network name and band."""
    return f"{row.payload.ssid}|{wifi.band_label(row.payload.frequency_mhz)}"


def decimal(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def describe_ping(result: wifi.PingResult) -> str:
    target = result.host
    if result.resolved_address and result.resolved_address != result.host:
        target = f"{result.host} ({result.resolved_address})"
    text = (
        f"{target} · {result.transmitted} gönderildi, {result.received} alındı · "
        f"%{decimal(result.loss_percent).removesuffix(',0')} kayıp"
    )
    if result.avg_ms is None:
        return f"{text} · yanıt yok"
    return f"{text} · en az / ort. / en çok {decimal(result.min_ms)} / {decimal(result.avg_ms)} / {decimal(result.max_ms)} ms"


def describe_mobile(lines: tuple[wifi.MobileLine, ...]) -> str:
    if not lines:
        return "Mobil ağ bilgisi okunamadı."
    out = []
    for index, line in enumerate(lines, start=1):
        parts = [
            f"SIM {index}" if len(lines) > 1 else "SIM",
            line.operator or "Operatör yok",
            line.network_type or MISSING,
            f"Sinyal: {LEVEL_NAMES[line.signal_level]} ({line.signal_level}/{BARS_TOTAL})",
            f"Veri: {line.data_state}",
            "Dolaşımda" if line.is_roaming else "Dolaşım yok",
        ]
        out.append(" · ".join(parts))
    return "\n".join(out)


def band_text(frequency_mhz: int) -> str:
    band = wifi.band_label(frequency_mhz)
    return f"{band} ({frequency_mhz} MHz)" if band else MISSING


def attempt(work: Callable[[], object]) -> Callable[[], tuple[object, str]]:
    """Wrap a device call so its failure comes back as a message instead of an exception."""

    def wrapped() -> tuple[object, str]:
        try:
            return work(), ""
        except (AdbError, ValueError) as exc:
            return None, str(exc)

    return wrapped


def confirm(parent: QWidget, title: str, text: str, accept: str) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    accepted = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Vazgeç", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is accepted


# --- password dialog ----------------------------------------------------------------
class PasswordDialog(QDialog):
    """Asks for a network password in a masked field with a show toggle."""

    def __init__(self, parent: QWidget, ssid: str, security: str) -> None:
        super().__init__(parent)
        self._security = security
        self.setWindowTitle("Ağa bağlan")
        self.setMinimumWidth(DIALOG_MIN_WIDTH)
        self._field = QLineEdit()
        self._field.setEchoMode(QLineEdit.EchoMode.Password)
        self._field.setPlaceholderText("Parola")
        self._field.setInputMethodHints(
            Qt.InputMethodHint.ImhHiddenText
            | Qt.InputMethodHint.ImhNoPredictiveText
            | Qt.InputMethodHint.ImhSensitiveData
        )
        self._show = button("Göster")
        self._show.setCheckable(True)
        self._show.toggled.connect(self._toggle_visibility)
        self._connect = button("Bağlan", variant="primary")
        self._connect.setEnabled(False)
        self._connect.clicked.connect(self.accept)
        cancel = button("İptal")
        cancel.clicked.connect(self.reject)
        self._field.textChanged.connect(self._validate)
        self._field.returnPressed.connect(self._accept_if_valid)

        title = label(f"“{ssid}” ağının parolası", "sectionTitle")
        title.setWordWrap(True)
        hint = label(f"{security} · {wifi.MIN_PASSWORD_LENGTH}-{wifi.MAX_PASSWORD_LENGTH} karakter", "muted")
        field_row = QHBoxLayout()
        field_row.setSpacing(8)
        field_row.addWidget(self._field, 1)
        field_row.addWidget(self._show)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch()
        actions.addWidget(cancel)
        actions.addWidget(self._connect)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        for item in (title, hint):
            layout.addWidget(item)
        layout.addLayout(field_row)
        layout.addLayout(actions)

    def _toggle_visibility(self, is_visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if is_visible else QLineEdit.EchoMode.Password
        self._field.setEchoMode(mode)
        self._show.setText("Gizle" if is_visible else "Göster")

    def _is_valid(self) -> bool:
        try:
            wifi.validate_password(self._field.text(), self._security)
        except ValueError:
            return False
        return True

    def _validate(self) -> None:
        self._connect.setEnabled(self._is_valid())

    def _accept_if_valid(self) -> None:
        if self._is_valid():
            self.accept()

    def password(self) -> str:
        return self._field.text()

    def wipe(self) -> None:
        """Drop the typed text so nothing readable stays in the widget."""
        self._field.clear()


# --- page ---------------------------------------------------------------------------
class NetworkPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(
            context, "Ağ", "Wi-Fi durumu, çevredeki ve kayıtlı ağlar, mobil ağ ve bağlantı testi"
        )
        self._status = wifi.WifiStatus()
        self._scan: tuple[wifi.ScanResult, ...] = ()
        self._saved: tuple[wifi.SavedNetwork, ...] = ()
        self._is_polling = False
        self._is_scanning = False
        self._is_testing = False
        self._has_stale_lists = True
        self._poll_count = 0
        self._signatures: dict[QListWidget, tuple] = {}
        self._values: dict[str, QLabel] = {}
        self._signal_icon = QLabel()
        self._state = label("", "muted")
        self._switch = ToggleSwitch()
        self._mobile = label("", "muted")
        self._scan_count = label("", "muted")
        self._saved_count = label("", "muted")
        self._scan_list = make_row_list()
        self._saved_list = make_row_list()
        self._scan_button = button("Tara", theme.Glyph.SEARCH)
        self._host = QLineEdit(DEFAULT_TEST_HOST)
        self._ping_button = button("Ping", theme.Glyph.SEND, "primary", icon_color="white")
        self._resolve_button = button("Ad çöz", theme.Glyph.GLOBE)
        self._test_result = label("", "muted")
        self._build_layout()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(POLL_INTERVAL_MS)

    # --- layout ---------------------------------------------------------------
    def _build_layout(self) -> None:
        reload = button("Yenile", theme.Glyph.REFRESH)
        reload.clicked.connect(lambda: self.refresh())
        self.header.addWidget(reload)
        self.content.addWidget(self._build_status_card())
        self.content.addWidget(self._build_scan_card())
        self.content.addWidget(self._build_saved_card())
        self.content.addWidget(self._build_mobile_card())
        self.content.addWidget(self._build_test_card())
        self.content.addStretch()
        self._switch.toggled.connect(self._on_wifi_toggled)
        self._set_unavailable_text()

    def _build_status_card(self) -> Card:
        card = Card()
        icon = QLabel()
        icon.setPixmap(theme.make_icon(theme.Glyph.WIFI, theme.ACCENT_ALT, 18).pixmap(18, 18))
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(icon)
        head.addWidget(label("Wi-Fi Durumu", "sectionTitle"))
        head.addStretch()
        head.addWidget(self._state)
        head.addWidget(self._switch)
        card.body.addLayout(head)
        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(14)
        for index, (key, title) in enumerate(STATUS_FIELDS):
            grid.addLayout(self._status_cell(key, title), index // STATUS_COLUMNS, index % STATUS_COLUMNS)
        for column in range(STATUS_COLUMNS):
            grid.setColumnStretch(column, 1)
        card.body.addLayout(grid)
        return card

    def _status_cell(self, key: str, title: str) -> QVBoxLayout:
        value = label(MISSING)
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._values[key] = value
        line = QHBoxLayout()
        line.setSpacing(8)
        if key == "signal":
            line.addWidget(self._signal_icon)
        line.addWidget(value, 1)
        cell = QVBoxLayout()
        cell.setSpacing(2)
        cell.addWidget(label(title, "muted"))
        cell.addLayout(line)
        return cell

    def _list_card(self, title: str, count: QLabel, widget: QListWidget, buttons: list) -> Card:
        card = Card()
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label(title, "sectionTitle"))
        head.addWidget(count)
        head.addStretch()
        for control in buttons:
            head.addWidget(control)
        card.body.addLayout(head)
        widget.setFixedHeight(ROW_HEIGHT * LIST_ROWS + LIST_PADDING)
        card.body.addWidget(widget)
        return card

    def _build_scan_card(self) -> Card:
        self._scan_button.clicked.connect(self._start_scan)
        connect = button("Bağlan", theme.Glyph.LINK, "primary", icon_color="white")
        connect.clicked.connect(self._connect_scanned)
        self._scan_list.itemDoubleClicked.connect(lambda _item: self._connect_scanned())
        return self._list_card(
            "Çevredeki Ağlar", self._scan_count, self._scan_list, [self._scan_button, connect]
        )

    def _build_saved_card(self) -> Card:
        connect = button("Bağlan", theme.Glyph.LINK, "primary", icon_color="white")
        connect.clicked.connect(self._connect_saved)
        forget = button("Unut", theme.Glyph.DELETE, "danger", icon_color=theme.DANGER)
        forget.clicked.connect(self._forget_saved)
        self._saved_list.itemDoubleClicked.connect(lambda _item: self._connect_saved())
        return self._list_card("Kayıtlı Ağlar", self._saved_count, self._saved_list, [connect, forget])

    def _build_mobile_card(self) -> Card:
        card = Card()
        card.body.addWidget(label("Mobil Ağ", "sectionTitle"))
        self._mobile.setWordWrap(True)
        self._mobile.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.body.addWidget(self._mobile)
        return card

    def _build_test_card(self) -> Card:
        card = Card()
        card.body.addWidget(label("Bağlantı Testi", "sectionTitle"))
        self._host.setPlaceholderText("Ana makine adı veya IP adresi")
        self._host.setClearButtonEnabled(True)
        self._host.returnPressed.connect(self._ping)
        self._ping_button.clicked.connect(self._ping)
        self._resolve_button.clicked.connect(self._resolve)
        line = QHBoxLayout()
        line.setSpacing(10)
        line.addWidget(self._host, 1)
        line.addWidget(self._ping_button)
        line.addWidget(self._resolve_button)
        card.body.addLayout(line)
        self._test_result.setWordWrap(True)
        self._test_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.body.addWidget(self._test_result)
        return card

    # --- data -----------------------------------------------------------------
    def set_service(self, service) -> None:
        if service is not self.service:
            self._status, self._scan, self._saved = wifi.WifiStatus(), (), ()
            self._has_stale_lists = True
            self._is_scanning = self._is_testing = False
            self._scan_button.setEnabled(True)
            self._set_test_busy(False)
            self._scan_list.clear()
            self._saved_list.clear()
            self._signatures.clear()
            self._set_unavailable_text()
        super().set_service(service)

    def _poll(self) -> None:
        if self.service is not None and self.isVisible():
            self.refresh(is_quiet=True)

    def refresh(self, is_quiet: bool = False) -> None:
        """Read everything on demand; polls read the status and, now and then, the mobile line."""
        if self.service is None or self._is_polling:
            return
        self._is_polling = True
        adb, serial = self.context.adb, self.service.serial
        self._poll_count += 1
        with_mobile = not is_quiet or self._poll_count % MOBILE_POLL_EVERY == 1
        with_lists = not is_quiet or self._has_stale_lists
        self._has_stale_lists = False

        def work() -> Snapshot:
            try:
                return self._read_snapshot(adb, serial, with_mobile, with_lists)
            finally:
                self._is_polling = False

        self.read(work, self._apply_snapshot, is_quiet)

    @staticmethod
    def _read_snapshot(adb: Adb, serial: str, with_mobile: bool, with_lists: bool) -> Snapshot:
        status = wifi.read_status(adb, serial)
        mobile = wifi.read_mobile(adb, serial) if with_mobile else None
        saved = scan = None
        if with_lists:
            saved = wifi.list_saved_networks(adb, serial)
            scan = wifi.list_scan_results(adb, serial)
        return Snapshot(status, mobile, saved, scan)

    def _apply_snapshot(self, snapshot: Snapshot) -> None:
        self._show_status(snapshot.status)
        if snapshot.mobile is not None:
            self._mobile.setText(describe_mobile(snapshot.mobile))
        if snapshot.saved is not None:
            self._saved = snapshot.saved
        if snapshot.scan is not None and not self._is_scanning:
            self._scan = snapshot.scan
        self._render_lists()

    def _show_status(self, status: wifi.WifiStatus) -> None:
        if status != self._status:
            self._status = status
            self._render_status()
        self._switch.set_checked_silently(status.is_enabled)

    def _set_unavailable_text(self) -> None:
        self._state.setText("")
        self._mobile.setText("Okunuyor…")
        self._render_status()

    def _render_status(self) -> None:
        status = self._status
        if not status.is_enabled:
            state = "Kapalı"
        else:
            state = "Bağlı" if status.is_connected else "Bağlı değil"
        self._state.setText(state)
        bars = wifi.signal_bars(status.rssi)
        rssi_text = f"{status.rssi} dBm · {LEVEL_NAMES[bars]}" if status.rssi is not None else MISSING
        values = {
            "ssid": status.ssid or MISSING,
            "bssid": status.bssid or MISSING,
            "band": band_text(status.frequency_mhz) if status.is_connected else MISSING,
            "speed": f"{status.link_speed_mbps} Mbps" if status.link_speed_mbps else MISSING,
            "signal": rssi_text,
            "ip": status.ip_address or MISSING,
            "gateway": status.gateway or MISSING,
            "dns": ", ".join(status.dns_servers) or MISSING,
            "mac": status.mac or MISSING,
        }
        for key, text in values.items():
            self._values[key].setText(text)
        self._signal_icon.setPixmap(bars_pixmap(bars, STATUS_BARS_SIZE, self.devicePixelRatioF()))
        self._render_lists()

    # --- lists ----------------------------------------------------------------
    def _is_current(self, ssid: str, frequency_mhz: int = 0) -> bool:
        status = self._status
        if not status.is_connected or status.ssid != ssid:
            return False
        return not frequency_mhz or wifi.band_label(frequency_mhz) == wifi.band_label(status.frequency_mhz)

    def _render_lists(self) -> None:
        ratio = self.devicePixelRatioF()
        scan_rows = [self._scan_row(item, ratio) for item in self._scan]
        saved_rows = [
            Row(
                item.ssid,
                f"{item.security}{' · Bağlı' if self._is_current(item.ssid) else ''}",
                f"No. {item.network_id}",
                payload=item,
            )
            for item in self._saved
        ]
        self._fill_if_changed(self._scan_list, scan_rows, scan_key)
        self._fill_if_changed(self._saved_list, saved_rows, lambda row: str(row.payload.network_id))
        self._scan_count.setText(f"{len(scan_rows)} ağ")
        self._saved_count.setText(f"{len(saved_rows)} ağ")

    def _scan_row(self, item: wifi.ScanResult, ratio: float) -> Row:
        parts = [item.security, wifi.band_label(item.frequency_mhz) or MISSING]
        if self._is_current(item.ssid, item.frequency_mhz):
            parts.insert(0, "Bağlı")
        return Row(
            item.ssid,
            " · ".join(parts),
            f"{item.rssi} dBm",
            payload=item,
            icon=bars_pixmap(wifi.signal_bars(item.rssi), STATUS_BARS_SIZE + 12, ratio),
        )

    def _fill_if_changed(self, widget: QListWidget, rows: list[Row], key: Callable[[Row], str]) -> None:
        """Refill only when the visible text changed, so polls keep hover and selection."""
        signature = tuple((row.title, row.subtitle, row.meta) for row in rows)
        if self._signatures.get(widget) == signature:
            return
        self._signatures[widget] = signature
        previous = selected_row(widget)
        wanted = key(previous) if previous is not None else None
        fill_rows(widget, rows)
        for index, row in enumerate(rows):
            if wanted is not None and key(row) == wanted:
                widget.setCurrentRow(index)
                return

    # --- wifi switch ----------------------------------------------------------
    def _on_wifi_toggled(self, is_on: bool) -> None:
        if not is_on and self._would_cut_adb() and not confirm(
            self,
            "Wi-Fi kapatılsın mı?",
            "Telefona Wi-Fi üzerinden bağlısın. Wi-Fi kapanırsa bağlantı kopar.",
            "Kapat",
        ):
            self._switch.set_checked_silently(True)
            return
        self.mark_changed()
        self._status = wifi.WifiStatus(is_enabled=is_on)
        self._render_status()
        adb, serial = self.context.adb, self.service.serial
        work = attempt(lambda: wifi.set_wifi_enabled(adb, serial, is_on))
        self.run(work, self._switch_done)

    def _would_cut_adb(self) -> bool:
        """adb over Wi-Fi uses an `address:port` serial."""
        return self.service is not None and ":" in self.service.serial

    def _switch_done(self, outcome: tuple[object, str]) -> None:
        _, error = outcome
        if error:
            self.context.notify(error, "error")
        self._has_stale_lists = True
        QTimer.singleShot(SETTLE_MS, lambda: self.refresh(is_quiet=True))

    # --- scan and connect -----------------------------------------------------
    def _start_scan(self) -> None:
        if self._is_scanning or self.service is None:
            return
        self._is_scanning = True
        self._scan_button.setEnabled(False)
        self._scan_button.setText(" Taranıyor…")
        adb, serial = self.context.adb, self.service.serial

        def work() -> tuple[wifi.ScanResult, ...]:
            wifi.start_scan(adb, serial)
            time.sleep(SCAN_WAIT_S)
            return wifi.list_scan_results(adb, serial)

        self.run(attempt(work), self._scan_done)

    def _scan_done(self, outcome: tuple[tuple[wifi.ScanResult, ...] | None, str]) -> None:
        results, error = outcome
        self._is_scanning = False
        self._scan_button.setEnabled(True)
        self._scan_button.setText(" Tara")
        if error:
            self.context.notify(error, "error")
            return
        self._scan = results or ()
        self._render_lists()

    def _connect_scanned(self) -> None:
        row = selected_row(self._scan_list)
        if row is None:
            self.context.notify("Önce çevredeki ağlardan birini seç.", "error")
            return
        item: wifi.ScanResult = row.payload
        self._connect(item.ssid, item.connect_type, item.security)

    def _connect_saved(self) -> None:
        row = selected_row(self._saved_list)
        if row is None:
            self.context.notify("Önce kayıtlı ağlardan birini seç.", "error")
            return
        item: wifi.SavedNetwork = row.payload
        self._connect(item.ssid, item.connect_type, item.security)

    def _connect(self, ssid: str, connect_type: str, security: str) -> None:
        if not connect_type:
            self.context.notify(f"{security} ağlarına buradan bağlanılamıyor.", "error")
            return
        password = ""
        if connect_type in wifi.PASSWORD_TYPES:
            password = self._ask_password(ssid, security)
            if not password:
                return
        adb, serial = self.context.adb, self.service.serial
        self.mark_changed()
        self.context.notify(f"{ssid} ağına bağlanılıyor…", "info")
        work = attempt(lambda: wifi.connect_network(adb, serial, ssid, connect_type, password))
        self.run(work, lambda outcome: self._connect_done(ssid, outcome))

    def _ask_password(self, ssid: str, security: str) -> str:
        """Password from the dialog, or '' if cancelled. The dialog is wiped either way."""
        dialog = PasswordDialog(self, ssid, security)
        try:
            return dialog.password() if dialog.exec() == QDialog.DialogCode.Accepted else ""
        finally:
            dialog.wipe()
            dialog.deleteLater()

    def _connect_done(self, ssid: str, outcome: tuple[object, str]) -> None:
        _, error = outcome
        if error:
            self.context.notify(error, "error")
            return
        self.context.notify(f"{ssid} için bağlantı isteği gönderildi.", "success")
        self._has_stale_lists = True
        QTimer.singleShot(SETTLE_MS, lambda: self.refresh(is_quiet=True))

    # --- saved networks -------------------------------------------------------
    def _forget_saved(self) -> None:
        row = selected_row(self._saved_list)
        if row is None:
            self.context.notify("Önce kayıtlı ağlardan birini seç.", "error")
            return
        item: wifi.SavedNetwork = row.payload
        text = f"“{item.ssid}” kayıtlı ağlardan silinecek; tekrar bağlanmak için parola gerekir."
        if self._is_current(item.ssid):
            text += " Şu an bu ağa bağlısın, bağlantı kopacak."
        if not confirm(self, "Ağ unutulsun mu?", text, "Unut"):
            return
        adb, serial = self.context.adb, self.service.serial
        self.mark_changed()
        work = attempt(lambda: wifi.forget_network(adb, serial, item.network_id))
        self.run(work, lambda outcome: self._forget_done(item.ssid, outcome))

    def _forget_done(self, ssid: str, outcome: tuple[object, str]) -> None:
        _, error = outcome
        if error:
            self.context.notify(error, "error")
            return
        self._saved = tuple(item for item in self._saved if item.ssid != ssid)
        self._render_lists()
        self.context.notify(f"{ssid} unutuldu.", "success")
        self._has_stale_lists = True
        QTimer.singleShot(SETTLE_MS, lambda: self.refresh(is_quiet=True))

    # --- connection test ------------------------------------------------------
    def _set_test_busy(self, is_busy: bool) -> None:
        self._is_testing = is_busy
        self._ping_button.setEnabled(not is_busy)
        self._resolve_button.setEnabled(not is_busy)

    def _start_test(self, work: Callable[[], str], waiting: str) -> None:
        if self._is_testing or self.service is None:
            return
        self._set_test_busy(True)
        self._test_result.setText(waiting)
        self.run(attempt(work), self._test_done)

    def _valid_host(self) -> str:
        try:
            return wifi.validate_host(self._host.text())
        except ValueError as exc:
            self.context.notify(str(exc), "error")
            return ""

    def _ping(self) -> None:
        host = self._valid_host()
        if not host:
            return
        adb, serial = self.context.adb, self.service.serial
        self._start_test(
            lambda: describe_ping(wifi.ping_host(adb, serial, host)), f"{host} için ping atılıyor…"
        )

    def _resolve(self) -> None:
        host = self._valid_host()
        if not host:
            return
        adb, serial = self.context.adb, self.service.serial
        self._start_test(
            lambda: f"{host} → {wifi.resolve_host(adb, serial, host)}", f"{host} çözümleniyor…"
        )

    def _test_done(self, outcome: tuple[str | None, str]) -> None:
        text, error = outcome
        self._set_test_busy(False)
        self._test_result.setText(error or text or "")
        if error:
            self.context.notify(error, "error")
