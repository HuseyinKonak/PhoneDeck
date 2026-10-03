"""Find my phone: ring, vibrate, blink the flash and screen, show the location and secure the phone."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ...core import finder
from ...core.adb import AdbError
from ...core.finder import DeviceSnapshot, LocationFix, OwnerInfo, PhoneStatus, RingSession
from .. import theme
from ..widgets import AppContext, Card, Page, button, label

TICK_MS = 1_000
STATUS_REFRESH_MS = 5_000
DEFAULT_DURATION_S = 30
FLASH_DURATION_S = 10
KIND_RING = "ring"
KIND_FLASH = "flash"
KIND_ALL = "all"
EFFECT_TEXTS = {
    KIND_RING: "Telefon çalıyor",
    KIND_FLASH: "Ekran yanıp sönüyor",
    KIND_ALL: "Zil, titreşim, flaş ve ekran çalışıyor",
}
DEFAULT_OWNER_MESSAGE = "Bulan kişi lütfen ara:"
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3_600
SECONDS_PER_DAY = 86_400
STATUS_ITEMS = (
    ("battery", "Batarya"),
    ("screen", "Ekran"),
    ("lock", "Kilit"),
    ("wifi", "Wi-Fi ağı"),
    ("transport", "Bağlantı"),
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StartOutcome:
    """Result of starting a ring or flash: what was captured, or why it failed."""

    snapshot: DeviceSnapshot | None = None
    session: RingSession | None = None
    error: str = ""
    note: str = ""


def format_age(seconds: int | None) -> str:
    if seconds is None:
        return "zamanı bilinmiyor"
    if seconds < SECONDS_PER_MINUTE:
        return "az önce"
    if seconds < SECONDS_PER_HOUR:
        return f"{seconds // SECONDS_PER_MINUTE} dk önce"
    if seconds < SECONDS_PER_DAY:
        return f"{seconds // SECONDS_PER_HOUR} sa önce"
    return f"{seconds // SECONDS_PER_DAY} gün önce"


def hint(text: str) -> QLabel:
    widget = label(text, "muted")
    widget.setWordWrap(True)
    return widget


def button_row(*buttons: QPushButton) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(10)
    for item in buttons:
        row.addWidget(item)
    row.addStretch()
    return row


class FinderPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(
            context, "Cihazımı Bul", "Kayıp telefonu çaldır, titret, konumunu gör ve güvene al"
        )
        self._snapshot: DeviceSnapshot | None = None
        self._session: RingSession | None = None
        self._kind = ""
        self._remaining = 0
        self._elapsed = 0
        self._is_busy = False
        self._has_vibrated = False
        self._has_torch = False
        self._fix: LocationFix | None = None

        reload = button("Yenile", theme.Glyph.REFRESH)
        reload.clicked.connect(lambda: self.refresh())
        self.header.addWidget(reload)

        self._build_status_card()
        grid = QGridLayout()
        grid.setSpacing(16)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.addWidget(self._build_ring_card(), 0, 0)
        grid.addLayout(self._build_side_column(), 0, 1)
        grid.addWidget(self._build_location_card(), 1, 0)
        grid.addWidget(self._build_security_card(), 1, 1)
        self.content.addLayout(grid)
        self.content.addStretch()
        self._sync_buttons()

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_if_visible)
        self._poll.start(STATUS_REFRESH_MS)

    # --- layout -------------------------------------------------------------
    def _build_status_card(self) -> None:
        card = Card()
        card.body.addWidget(label("Durum", "sectionTitle"))
        row = QHBoxLayout()
        row.setSpacing(16)
        self._status_values: dict[str, QLabel] = {}
        for key, title in STATUS_ITEMS:
            column = QVBoxLayout()
            column.setSpacing(2)
            value = label("—")
            value.setWordWrap(True)
            self._status_values[key] = value
            column.addWidget(label(title, "muted"))
            column.addWidget(value)
            row.addLayout(column, 1)
        card.body.addLayout(row)
        self.content.addWidget(card)

    def _build_ring_card(self) -> Card:
        card = Card()
        card.body.addWidget(label("Çaldır", "sectionTitle"))
        card.body.addWidget(
            hint("Telefon sessizde olsa bile en yüksek sesle çalar; süre bitince ses ayarları geri yüklenir.")
        )
        self._duration_group = QButtonGroup(self)
        durations = QHBoxLayout()
        durations.setSpacing(10)
        for seconds in finder.RING_DURATIONS_S:
            choice = button(f"{seconds} sn", variant="tile")
            choice.setCheckable(True)
            choice.setChecked(seconds == DEFAULT_DURATION_S)
            self._duration_group.addButton(choice, seconds)
            durations.addWidget(choice)
        durations.addStretch()
        card.body.addLayout(durations)
        self._ring_button = button("Çaldır", theme.Glyph.BELL, "primary", icon_color="white")
        self._ring_button.clicked.connect(self._start_ring)
        self._all_button = button(
            "Her Şey", theme.Glyph.PLAY, "primary", icon_color="white",
            tooltip="Zil, titreşim, flaş ve ekranı seçili süre boyunca aynı anda çalıştırır",
        )
        self._all_button.clicked.connect(self._start_everything)
        self._stop_button = button("Durdur", theme.Glyph.STOP, "danger", icon_color=theme.DANGER)
        self._stop_button.clicked.connect(self._stop_effect)
        card.body.addLayout(button_row(self._ring_button, self._all_button, self._stop_button))
        self._progress = hint("Hazır")
        card.body.addWidget(self._progress)
        card.body.addStretch()
        return card

    def _build_side_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(16)
        vibrate = Card()
        vibrate.body.addWidget(label("Titret", "sectionTitle"))
        vibrate.body.addWidget(hint("Düzenli aralıklarla titretir; Rahatsız Etme açık olsa da çalışır."))
        start = button("Titret", theme.Glyph.PHONE, icon_color=theme.ACCENT_ALT)
        start.clicked.connect(self._start_vibration)
        stop = button("Durdur", theme.Glyph.STOP, "danger", icon_color=theme.DANGER)
        stop.clicked.connect(self._stop_vibration)
        vibrate.body.addLayout(button_row(start, stop))
        flash = Card()
        flash.body.addWidget(label("Flaş ve Ekran", "sectionTitle"))
        flash.body.addWidget(
            hint("Flaş, kamera ışığını seçili süre boyunca yakıp söndürür. Ekranı Yak, ekranı tam "
                 f"parlaklıkta {FLASH_DURATION_S} sn yakıp söndürür.")
        )
        torch = button("Flaşı Yak", theme.Glyph.BRIGHTNESS, icon_color=theme.WARNING)
        torch.clicked.connect(self._start_torch)
        torch_stop = button("Durdur", theme.Glyph.STOP, "danger", icon_color=theme.DANGER)
        torch_stop.setToolTip("Flaşı söndür")
        torch_stop.clicked.connect(self._stop_torch)
        self._flash_button = button("Ekranı Yak", theme.Glyph.BRIGHTNESS, icon_color=theme.WARNING)
        self._flash_button.clicked.connect(self._start_flash)
        flash.body.addLayout(button_row(torch, torch_stop, self._flash_button))
        column.addWidget(vibrate)
        column.addWidget(flash)
        return column

    def _build_location_card(self) -> Card:
        card = Card()
        card.body.addWidget(label("Konum", "sectionTitle"))
        self._location_text = label("Konum bilinmiyor")
        self._location_text.setWordWrap(True)
        self._location_detail = hint("Telefonun bildirdiği son bilinen konum; güncel olmayabilir.")
        card.body.addWidget(self._location_text)
        card.body.addWidget(self._location_detail)
        read = button("Konumu Oku", theme.Glyph.REFRESH)
        read.clicked.connect(self._read_location)
        self._map_button = button("Haritada Aç", theme.Glyph.GLOBE, "primary", icon_color="white")
        self._map_button.clicked.connect(self._open_map)
        self._copy_button = button("", theme.Glyph.COPY, tooltip="Koordinatları kopyala")
        self._copy_button.clicked.connect(self._copy_location)
        card.body.addLayout(button_row(read, self._map_button, self._copy_button))
        card.body.addStretch()
        return card

    def _build_security_card(self) -> Card:
        card = Card()
        card.body.addWidget(label("Güvenlik", "sectionTitle"))
        lock = button("Ekranı Kilitle", theme.Glyph.POWER, icon_color=theme.ACCENT_ALT)
        lock.clicked.connect(self._lock_screen)
        card.body.addLayout(button_row(lock))
        card.body.addWidget(hint("Kilit ekranında görünecek mesaj:"))
        self._owner_message = QLineEdit(DEFAULT_OWNER_MESSAGE)
        self._owner_message.setMaxLength(finder.OWNER_MESSAGE_MAX_CHARS)
        self._owner_phone = QLineEdit()
        self._owner_phone.setPlaceholderText("Aranacak telefon numarası")
        card.body.addWidget(self._owner_message)
        card.body.addWidget(self._owner_phone)
        apply = button("Mesajı Ayarla", theme.Glyph.SHIELD, "primary", icon_color="white")
        apply.clicked.connect(self._set_owner_message)
        clear = button("Mesajı Kaldır", theme.Glyph.CLOSE)
        clear.clicked.connect(self._clear_owner_message)
        card.body.addLayout(button_row(apply, clear))
        self._owner_state = hint("Kilit ekranı mesajı okunuyor…")
        card.body.addWidget(self._owner_state)
        card.body.addStretch()
        return card

    # --- device plumbing ----------------------------------------------------
    def _device(self):
        """Adb and serial of the selected phone, or None (with a message) when there is none."""
        if self.service is None:
            self.context.notify("Telefon bağlı değil.", "error")
            return None
        return self.context.adb, self.service.serial

    def set_service(self, service) -> None:
        is_other_phone = (
            self.service is not None
            and (service is None or service.serial != self.service.serial)
        )
        if is_other_phone:
            self._restore_now()
        super().set_service(service)
        if is_other_phone or service is None:
            self._fix = None
            self._show_fix()

    def on_shown(self) -> None:
        self.refresh()
        self._read_owner_info()
        if self._fix is None:
            self._read_location()

    def refresh(self, is_quiet: bool = False) -> None:
        if self.service is None:
            return
        adb, serial = self.context.adb, self.service.serial
        self.read(lambda: finder.read_status(adb, serial), self._show_status, is_quiet)

    def _refresh_if_visible(self) -> None:
        if self.isVisible() and self.service is not None and not self._is_busy:
            self.refresh(is_quiet=True)

    def _show_status(self, status: PhoneStatus) -> None:
        charging = "şarj oluyor" if status.is_charging else "pilde"
        texts = {
            "battery": f"%{status.battery_level} · {charging}",
            "screen": "Açık" if status.is_screen_on else "Kapalı",
            "lock": "Kilitli" if status.is_locked else "Kilit açık",
            "wifi": status.ssid or "Bağlı değil",
            "transport": "USB" if status.transport == finder.TRANSPORT_USB else "Wi-Fi (kablosuz)",
        }
        for key, text in texts.items():
            self._status_values[key].setText(text)

    # --- ring and screen flash ----------------------------------------------
    def _selected_seconds(self) -> int:
        return self._duration_group.checkedId()

    def _start_ring(self) -> None:
        seconds = self._selected_seconds()

        def work(adb, serial) -> StartOutcome:
            snapshot = finder.read_snapshot(adb, serial)
            session = finder.start_ring(adb, serial, snapshot, seconds)
            return StartOutcome(snapshot, session)

        self._begin_effect(KIND_RING, seconds, work)

    def _start_flash(self) -> None:
        def work(adb, serial) -> StartOutcome:
            snapshot = finder.read_snapshot(adb, serial)
            finder.prepare_flash(adb, serial, snapshot)
            finder.pulse_screen(adb, serial, True)
            return StartOutcome(snapshot)

        self._begin_effect(KIND_FLASH, FLASH_DURATION_S, work)

    def _start_everything(self) -> None:
        seconds = self._selected_seconds()

        def work(adb, serial) -> StartOutcome:
            snapshot = finder.read_snapshot(adb, serial)
            session, skipped = finder.start_everything(adb, serial, snapshot, seconds)
            note = f"Başlatılamayanlar: {', '.join(skipped)}" if skipped else ""
            return StartOutcome(snapshot, session, note=note)

        self._begin_effect(KIND_ALL, seconds, work)

    def _begin_effect(self, kind: str, seconds: int, work) -> None:
        if self._is_busy or self._kind:
            return
        device = self._device()
        if device is None:
            return
        self._is_busy = True
        self._sync_buttons()

        def guarded() -> StartOutcome:
            try:
                return work(*device)
            except (AdbError, ValueError) as exc:
                return StartOutcome(error=str(exc))

        self.run(guarded, lambda outcome: self._effect_started(kind, seconds, outcome))

    def _effect_started(self, kind: str, seconds: int, outcome: StartOutcome) -> None:
        self._is_busy = False
        if outcome.error:
            self.context.notify(outcome.error, "error")
        else:
            self._snapshot, self._session = outcome.snapshot, outcome.session
            self._kind, self._remaining, self._elapsed = kind, seconds, 0
            self._tick.start(TICK_MS)
            if outcome.note:
                self.context.notify(outcome.note, "info")
        self._sync_buttons()

    def _stop_effect(self) -> None:
        snapshot, kind = self._snapshot, self._kind
        if snapshot is None or self._is_busy:
            return
        device = self._device()
        if device is None:
            return
        self._is_busy = True
        self._tick.stop()
        self._sync_buttons()

        def work() -> str:
            try:
                if kind == KIND_ALL:
                    finder.stop_everything(*device, snapshot)
                else:
                    finder.restore_device(*device, snapshot, kind == KIND_RING)
                if kind == KIND_FLASH:
                    finder.pulse_screen(*device, True)
            except AdbError as exc:
                return str(exc)
            return ""

        self.run(work, self._effect_stopped)

    def _effect_stopped(self, error: str) -> None:
        self._is_busy = False
        if error:
            # Keep the snapshot so "Durdur" can try the restore again.
            self.context.notify(f"Ayarlar geri yüklenemedi: {error}", "error")
            self._tick.start(TICK_MS)
        else:
            self._clear_effect()
            self.context.notify("Ses ve ekran ayarları eski haline getirildi", "success")
        self._sync_buttons()

    def _clear_effect(self) -> None:
        self._tick.stop()
        self._snapshot, self._session, self._kind = None, None, ""
        self._remaining = self._elapsed = 0

    def _restore_now(self) -> None:
        """Blocking restore for shutdown and device changes; the phone also has a watchdog."""
        adb, service = self.context.adb, self.service
        if service is None:
            return
        try:
            if self._snapshot is not None and self._kind == KIND_ALL:
                finder.stop_everything(adb, service.serial, self._snapshot)
            elif self._snapshot is not None:
                finder.restore_device(adb, service.serial, self._snapshot, self._kind == KIND_RING)
            if self._has_vibrated:
                finder.stop_vibration(adb, service.serial)
            if self._has_torch:
                finder.stop_torch(adb, service.serial)
        except AdbError as exc:
            logger.warning("restore on shutdown failed: %s", exc)
        self._has_vibrated = self._has_torch = False
        self._clear_effect()
        self._is_busy = False
        self._sync_buttons()

    def shutdown(self) -> None:
        self._poll.stop()
        self._restore_now()

    def _on_tick(self) -> None:
        if self._is_busy or not self._kind:
            return
        self._elapsed += 1
        self._remaining -= 1
        if self._remaining <= 0:
            self._stop_effect()
            return
        self._sync_progress()
        self._pulse_effect()

    def _pulse_effect(self) -> None:
        adb, service = self.context.adb, self.service
        if service is None:
            return
        serial = service.serial
        is_on = self._elapsed % 2 == 0
        if self._kind == KIND_FLASH:
            self.run(lambda: finder.pulse_screen(adb, serial, is_on), is_quiet=True)
            return
        if self._kind == KIND_ALL and self._snapshot is not None:
            # Brightness blinking (not screen off/on) so the ring keeps playing.
            snapshot = self._snapshot
            self.run(lambda: finder.blink_screen(adb, serial, snapshot, is_on), is_quiet=True)
        if self._session is not None and self._elapsed % finder.REPLAY_INTERVAL_S == 0:
            session = self._session
            self.run(lambda: finder.replay_sound(adb, serial, session), is_quiet=True)

    def _sync_progress(self) -> None:
        if not self._kind:
            self._progress.setText("Hazır")
            return
        what = EFFECT_TEXTS[self._kind]
        self._progress.setText(f"{what} · {self._remaining} sn kaldı")

    def _sync_buttons(self) -> None:
        is_idle = not self._kind and not self._is_busy
        self._ring_button.setEnabled(is_idle)
        self._all_button.setEnabled(is_idle)
        self._flash_button.setEnabled(is_idle)
        self._stop_button.setEnabled(bool(self._kind) and not self._is_busy)
        for choice in self._duration_group.buttons():
            choice.setEnabled(is_idle)
        self._sync_progress()

    # --- vibration ----------------------------------------------------------
    def _start_vibration(self) -> None:
        device = self._device()
        if device is None:
            return
        self._has_vibrated = True
        self.run(lambda: finder.start_vibration(*device), success="Telefon titriyor")

    def _stop_vibration(self) -> None:
        device = self._device()
        if device is None:
            return
        self._has_vibrated = False
        self.run(lambda: finder.stop_vibration(*device), success="Titreşim durduruldu")

    # --- flash --------------------------------------------------------------
    def _start_torch(self) -> None:
        device = self._device()
        if device is None:
            return
        seconds = self._selected_seconds()
        self._has_torch = True
        self.run(lambda: finder.start_torch(*device, seconds), success=f"Flaş {seconds} sn yanıp sönecek")

    def _stop_torch(self) -> None:
        device = self._device()
        if device is None:
            return
        self._has_torch = False
        self.run(lambda: finder.stop_torch(*device), success="Flaş söndürüldü")

    # --- location -----------------------------------------------------------
    def _read_location(self) -> None:
        if self.service is None:
            return
        adb, serial = self.context.adb, self.service.serial
        self.run(lambda: finder.read_location(adb, serial), self._location_read)

    def _location_read(self, fix: LocationFix | None) -> None:
        self._fix = fix
        self._show_fix()

    def _show_fix(self) -> None:
        fix = self._fix
        has_fix = fix is not None
        self._map_button.setEnabled(has_fix)
        self._copy_button.setEnabled(has_fix)
        if fix is None:
            self._location_text.setText("Konum bilinmiyor")
            return
        accuracy = f" · yaklaşık {fix.accuracy_m:.0f} m" if fix.accuracy_m is not None else ""
        stale = (
            " · çok eski olabilir"
            if fix.age_s is not None and fix.age_s > finder.LOCATION_STALE_S
            else ""
        )
        self._location_text.setText(
            f"{fix.coordinates_text}\n{fix.provider} · {format_age(fix.age_s)}{accuracy}{stale}"
        )

    def _open_map(self) -> None:
        if self._fix is not None and not QDesktopServices.openUrl(QUrl(self._fix.maps_url)):
            self.context.notify("Tarayıcı açılamadı.", "error")

    def _copy_location(self) -> None:
        if self._fix is not None:
            QGuiApplication.clipboard().setText(self._fix.coordinates_text)
            self.context.notify("Koordinatlar kopyalandı", "success")

    # --- security -----------------------------------------------------------
    def _lock_screen(self) -> None:
        answer = QMessageBox.question(self, "Ekranı Kilitle", "Telefonun ekranı kilitlensin mi?")
        device = self._device() if answer == QMessageBox.StandardButton.Yes else None
        if device is not None:
            self.run(
                lambda: finder.lock_screen(*device),
                lambda _result: self.refresh(is_quiet=True),
                success="Ekran kilitlendi",
            )

    def _read_owner_info(self) -> None:
        if self.service is None:
            return
        adb, serial = self.context.adb, self.service.serial
        self.read(lambda: finder.read_owner_info(adb, serial), self._show_owner_info, is_quiet=True)

    def _show_owner_info(self, info: OwnerInfo) -> None:
        if info.is_enabled and info.message:
            self._owner_state.setText(f"Şu an kilit ekranında: “{info.message}”")
        else:
            self._owner_state.setText("Kilit ekranında mesaj gösterilmiyor.")

    def _set_owner_message(self) -> None:
        message, phone = self._owner_message.text(), self._owner_phone.text()
        try:
            finder.validate_owner_message(message, phone)
        except ValueError as exc:
            self.context.notify(str(exc), "error")
            return
        device = self._device()
        if device is not None:
            self.run(
                lambda: finder.set_owner_info(*device, message, phone),
                lambda _text: self._read_owner_info(),
                success="Kilit ekranı mesajı ayarlandı",
            )

    def _clear_owner_message(self) -> None:
        device = self._device()
        if device is not None:
            self.run(
                lambda: finder.clear_owner_info(*device),
                lambda _result: self._read_owner_info(),
                success="Kilit ekranı mesajı kaldırıldı",
            )
