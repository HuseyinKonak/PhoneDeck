"""Notifications page: the phone's notification shade, a session history and desktop pop-ups."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QSystemTrayIcon

from ...core import telephony
from ...core.adb import AdbError
from ...core.models import Notification
from ...core.scrcpy import protocol
from ...core.scrcpy.protocol import ControlType
from .. import theme
from ..listing import AVATAR_SIZE, Row, fill_rows, format_timestamp, make_row_list, selected_row
from ..widgets import AppContext, Page, button, label
from .apps import display_name, icon_cache_dir
from .devtools import confirm

logger = logging.getLogger(__name__)

REFRESH_INTERVAL_MS = 6_000
MAX_HISTORY = 500
POPUP_DURATION_MS = 6_000
POPUP_KEY = "notifications/desktop_popups"
VIEW_CURRENT = "current"
VIEW_HISTORY = "history"
MS_PER_SECOND = 1000
CLEAR_SETTLE_MS = 800


@dataclass(frozen=True)
class HistoryEntry:
    notification: Notification
    seen_ms: int


class NotificationsPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Bildirimler", "Güncel bildirimler, oturum geçmişi ve masaüstü uyarıları")
        self._notifications: tuple[Notification, ...] = ()
        self._history: tuple[HistoryEntry, ...] = ()
        self._has_baseline = False
        self._labels: dict[str, str] = {}
        self._icons: dict[str, QPixmap] = {}
        self._is_loading_labels = False
        self._tray: QSystemTrayIcon | None = None
        self._count = label("", "muted")
        self._view = QComboBox()
        self._search = QLineEdit()
        self._popups = QCheckBox("Masaüstü uyarısı göster")
        self._list = make_row_list()
        self._build_layout()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(REFRESH_INTERVAL_MS)

    def _build_layout(self) -> None:
        reload = button("Yenile", theme.Glyph.REFRESH)
        reload.clicked.connect(lambda: self.refresh())
        self.header.addWidget(self._count)
        self.header.addSpacing(12)
        self.header.addWidget(reload)

        self._view.addItem("Güncel bildirimler", VIEW_CURRENT)
        self._view.addItem("Bu oturumun geçmişi", VIEW_HISTORY)
        self._view.currentIndexChanged.connect(lambda _index: self._render())
        self._search.setPlaceholderText("Başlık, metin veya uygulama ara…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _text: self._render())
        self._popups.setToolTip("Yeni bildirim geldiğinde bilgisayarda uyarı çıkar (sayfa kapalıyken de)")
        self._popups.setChecked(QSettings().value(POPUP_KEY, False, type=bool))
        self._popups.toggled.connect(lambda is_on: QSettings().setValue(POPUP_KEY, is_on))
        filters = QHBoxLayout()
        filters.setSpacing(12)
        filters.addWidget(self._view)
        filters.addWidget(self._search, 1)
        filters.addWidget(self._popups)
        self.content.addLayout(filters)

        self._list.itemDoubleClicked.connect(lambda _item: self._open_app())
        self.content.addWidget(self._list, 1)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        open_app = button("Uygulamayı Aç", theme.Glyph.PLAY, "primary", icon_color="white")
        open_app.clicked.connect(self._open_app)
        panel = button("Telefonda Paneli Aç", theme.Glyph.BELL)
        panel.clicked.connect(self._open_panel)
        clear = button(
            "Bildirimleri Temizle", theme.Glyph.DELETE, "danger", icon_color=theme.DANGER,
            tooltip="Telefondaki silinebilir bildirimleri kapatır ve bu oturumun geçmişini boşaltır",
        )
        clear.clicked.connect(self._clear_all)
        for control in (open_app, panel, clear):
            actions.addWidget(control)
        actions.addStretch()
        self.content.addLayout(actions)

    # --- data ---------------------------------------------------------------
    def set_service(self, service) -> None:
        if service is not self.service:
            self._labels.clear()
            self._icons.clear()
            self._notifications = ()
            self._history = ()
            self._has_baseline = False
            self._list.clear()
        super().set_service(service)

    def _poll(self) -> None:
        """Poll while the page is open, or in the background when pop-ups are on."""
        if self.service is not None and (self.isVisible() or self._popups.isChecked()):
            self.refresh(is_quiet=True)

    def refresh(self, is_quiet: bool = False) -> None:
        if self.service is None:
            return
        self.read(self.service.notifications, self._show, is_quiet)
        if not self._labels and not self._is_loading_labels:
            self._load_labels()

    def _load_labels(self) -> None:
        """Fetch real app names and icons once; the list shows package names until then."""
        service = self.service
        icon_dir = icon_cache_dir(service.serial)
        self._is_loading_labels = True

        def load() -> dict[str, str]:
            apps = service.apps(include_system=True)
            icon_dir.mkdir(parents=True, exist_ok=True)
            try:
                service.pull_icons(str(icon_dir))
            except AdbError as exc:
                logger.warning("icon download failed: %s", exc)
            return {app.package: app.label for app in apps}

        def done(labels: dict[str, str]) -> None:
            self._is_loading_labels = False
            self._labels.update(labels)
            self._render()

        self.run(load, done, is_quiet=True)

    def _show(self, notifications: tuple[Notification, ...]) -> None:
        known = set(self._notifications)
        fresh = [item for item in notifications if item not in known]
        now_ms = int(time.time() * MS_PER_SECOND)
        if fresh:
            entries = tuple(HistoryEntry(item, now_ms) for item in fresh)
            self._history = (*entries, *self._history)[:MAX_HISTORY]
        if self._has_baseline and self._popups.isChecked():
            for item in fresh:
                self._popup(item)
        is_changed = notifications != self._notifications or self._list.count() == 0
        self._notifications = notifications
        self._has_baseline = True
        if is_changed or fresh:
            self._render()

    def _app_name(self, package: str) -> str:
        return self._labels.get(package) or display_name(package)

    def _popup(self, item: Notification) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._tray is None:
            self._tray = QSystemTrayIcon(theme.make_icon(theme.Glyph.PHONE, theme.ACCENT, 32), self)
            self._tray.setToolTip("PhoneDeck")
            self._tray.show()
        title = f"{self._app_name(item.package)} · {item.title}"
        self._tray.showMessage(title, item.text, QSystemTrayIcon.MessageIcon.Information, POPUP_DURATION_MS)

    def _icon(self, package: str) -> QPixmap | None:
        if package not in self._icons and self.service is not None:
            pixmap = QPixmap(str(icon_cache_dir(self.service.serial) / f"{package}.png"))
            if pixmap.isNull():
                return None
            ratio = self.devicePixelRatioF()
            side = round(AVATAR_SIZE * ratio)
            scaled = pixmap.scaled(
                side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            scaled.setDevicePixelRatio(ratio)
            self._icons[package] = scaled
        return self._icons.get(package)

    def _render(self) -> None:
        is_history = self._view.currentData() == VIEW_HISTORY
        query = self._search.text().strip().casefold()
        entries = (
            self._history if is_history else tuple(HistoryEntry(item, 0) for item in self._notifications)
        )
        rows = []
        for entry in entries:
            item = entry.notification
            app_name = self._app_name(item.package)
            if query and query not in f"{item.title} {item.text} {app_name}".casefold():
                continue
            meta = f"{app_name} · {format_timestamp(entry.seen_ms)}" if is_history else app_name
            rows.append(Row(item.title, item.text, meta, payload=item, icon=self._icon(item.package)))
        fill_rows(self._list, rows)
        noun = "kayıt" if is_history else "bildirim"
        self._count.setText(f"{len(rows)} {noun}")

    # --- actions ------------------------------------------------------------
    def _open_app(self) -> None:
        row = selected_row(self._list)
        if row is None:
            self.context.notify("Önce bir bildirim seç.", "error")
            return
        package = row.payload.package
        self.run(
            lambda: self.service.launch_app(package), success=f"{self._app_name(package)} açıldı"
        )

    def _open_panel(self) -> None:
        if not self.context.send_control(protocol.simple(ControlType.EXPAND_NOTIFICATION_PANEL)):
            self.context.notify("Ekran akışı bağlı değil.", "error")

    def _clear_all(self) -> None:
        """Dismiss the phone's notifications and empty the session history."""
        if self.service is None:
            return
        text = (
            "Telefondaki silinebilir tüm bildirimler kapatılacak ve bu oturumun geçmişi boşaltılacak.\n\n"
            "Kapatılan bildirimler geri getirilemez."
        )
        if not confirm(self, "Bildirimleri Temizle", text, "Temizle"):
            return
        adb, serial = self.context.adb, self.service.serial
        self.mark_changed()
        self.run(lambda: telephony.clear_notifications(adb, serial), self._cleared)

    def _cleared(self, _message: str) -> None:
        self._history = ()
        self._render()
        self.context.notify("Telefondaki bildirimler ve oturum geçmişi temizlendi", "success")
        # Ongoing notifications (music, calls) cannot be dismissed; reread what is left.
        QTimer.singleShot(CLEAR_SETTLE_MS, lambda: self.refresh(is_quiet=True))

    def shutdown(self) -> None:
        if self._tray is not None:
            self._tray.hide()
