"""Main window: sidebar navigation, pages and the live phone panel."""

from __future__ import annotations

import ctypes
import logging
import sys

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.adb import Adb
from ..core.device import DeviceService
from ..core.models import Device
from . import theme
from .async_runner import run_async
from .mirror import PhonePanel
from .pages.apps import AppsPage
from .pages.calls import CallsPage
from .pages.contacts import ContactsPage
from .pages.controls import ControlsPage
from .pages.dashboard import DashboardPage
from .pages.devtools import DevToolsPage
from .pages.files import FilesPage
from .pages.finder import FinderPage
from .pages.logcat import LogcatPage
from .pages.media import MediaPage
from .pages.network import NetworkPage
from .pages.notifications import NotificationsPage
from .pages.performance import PerformancePage
from .pages.sms import SmsPage
from .pages.system import SystemSettingsPage
from .pages.terminal import TerminalPage
from .widgets import AppContext, Toast, label

logger = logging.getLogger(__name__)

APP_NAME = "PhoneDeck"
SIDEBAR_WIDTH = 220
DEVICE_POLL_MS = 3000
WINDOW_SIZE = QSize(1680, 960)
MINIMUM_SIZE = QSize(1180, 720)
SCREEN_MARGIN = QSize(40, 70)
DWMWA_USE_IMMERSIVE_DARK_MODE = 20


class MainWindow(QMainWindow):
    def __init__(self, adb: Adb) -> None:
        super().__init__()
        self._adb = adb
        self._devices: tuple[Device, ...] = ()
        self._serial: str | None = None
        self._is_polling = False
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(MINIMUM_SIZE)
        # As large as the preferred size, but never larger than the screen.
        available = self.screen().availableGeometry().size() - SCREEN_MARGIN
        self.resize(WINDOW_SIZE.boundedTo(available).expandedTo(MINIMUM_SIZE))

        self._phone = PhonePanel(adb)
        self._phone.notify.connect(self.notify)
        context = AppContext(adb=adb, notify=self.notify, send_control=self._phone.send_control)
        # (section heading, text, glyph, page); a heading is shown where it changes.
        self._pages = (
            ("GENEL", "Genel Bakış", theme.Glyph.HOME, DashboardPage(context)),
            ("GENEL", "Kontroller", theme.Glyph.SLIDERS, ControlsPage(context)),
            ("GENEL", "Kamera ve Ses", theme.Glyph.CAMERA, MediaPage(context)),
            ("İLETİŞİM", "Bildirimler", theme.Glyph.BELL, NotificationsPage(context)),
            ("İLETİŞİM", "Aramalar", theme.Glyph.CALL, CallsPage(context)),
            ("İLETİŞİM", "Mesajlar", theme.Glyph.MESSAGE, SmsPage(context)),
            ("İLETİŞİM", "Rehber", theme.Glyph.CONTACT, ContactsPage(context)),
            ("YÖNETİM", "Uygulamalar", theme.Glyph.APPS, AppsPage(context)),
            ("YÖNETİM", "Dosyalar", theme.Glyph.FOLDER, FilesPage(context)),
            ("YÖNETİM", "Sistem Ayarları", theme.Glyph.BRIGHTNESS, SystemSettingsPage(context)),
            ("YÖNETİM", "Ağ ve Wi-Fi", theme.Glyph.WIFI, NetworkPage(context)),
            ("YÖNETİM", "Cihazımı Bul", theme.Glyph.SEARCH, FinderPage(context)),
            ("GELİŞTİRİCİ", "Terminal", theme.Glyph.TERMINAL, TerminalPage(context)),
            ("GELİŞTİRİCİ", "Logcat", theme.Glyph.HISTORY, LogcatPage(context)),
            ("GELİŞTİRİCİ", "Performans", theme.Glyph.CHART, PerformancePage(context)),
            ("GELİŞTİRİCİ", "Geliştirici Araçları", theme.Glyph.TOOLS, DevToolsPage(context)),
        )
        self._stack = QStackedWidget()
        self._device_combo = QComboBox()
        self._device_status = label("Cihaz aranıyor…", "muted")

        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # The live screen can be widened by dragging the divider.
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._stack)
        splitter.addWidget(self._phone)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(splitter, 1)
        self.setCentralWidget(root)
        self._toast = Toast(root)

        for *_, page in self._pages:
            page.set_service(None)
            self._stack.addWidget(page)
        self._device_combo.activated.connect(self._on_device_chosen)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_devices)
        self._poll_timer.start(DEVICE_POLL_MS)
        self._poll_devices()
        self._apply_dark_title_bar()

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(SIDEBAR_WIDTH)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 22, 16, 18)
        layout.setSpacing(6)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(theme.make_icon(theme.Glyph.PHONE, theme.ACCENT, 26).pixmap(26, 26))
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label(APP_NAME, "brand"))
        names.addWidget(label("Telefon kontrol merkezi", "brandSub"))
        brand.addWidget(logo)
        brand.addLayout(names)
        brand.addStretch()
        layout.addLayout(brand)
        layout.addSpacing(10)
        layout.addWidget(self._build_navigation(), 1)

        layout.addWidget(label("CİHAZ", "brandSub"))
        layout.addWidget(self._device_combo)
        self._device_status.setWordWrap(True)
        layout.addWidget(self._device_status)
        return sidebar

    def _build_navigation(self) -> QScrollArea:
        """Page buttons grouped under section headings; scrolls on short windows."""
        body = QWidget()
        body.setObjectName("scrollBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        group = QButtonGroup(self)
        current_section = ""
        for index, (section, text, glyph, _) in enumerate(self._pages):
            if section != current_section:
                current_section = section
                layout.addWidget(label(section, "navSection"))
            nav = QPushButton(f"  {text}")
            nav.setObjectName("navButton")
            nav.setCheckable(True)
            nav.setIcon(theme.make_icon(glyph, theme.TEXT, 16))
            nav.setCursor(Qt.CursorShape.PointingHandCursor)
            nav.clicked.connect(lambda _checked=False, i=index: self._show_page(i))
            group.addButton(nav)
            layout.addWidget(nav)
            if index == 0:
                nav.setChecked(True)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        return scroll

    def _show_page(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        page = self._pages[index][-1]
        if page.service is not None:
            page.on_shown()

    # --- devices ------------------------------------------------------------
    def _poll_devices(self) -> None:
        if self._is_polling:
            return
        self._is_polling = True
        run_async(self._adb.devices, self._on_devices, self._on_poll_error)

    def _on_poll_error(self, message: str) -> None:
        self._is_polling = False
        self._device_status.setText(message)

    def _on_devices(self, devices: tuple[Device, ...]) -> None:
        self._is_polling = False
        if devices == self._devices:
            return
        self._devices = devices
        ready = [device for device in devices if device.is_ready]
        self._device_combo.blockSignals(True)
        self._device_combo.clear()
        for device in ready:
            self._device_combo.addItem(device.title, device.serial)
        self._device_combo.blockSignals(False)

        serials = [device.serial for device in ready]
        if self._serial in serials:
            self._device_combo.setCurrentIndex(serials.index(self._serial))
        else:
            self._select_device(self._preferred_serial(ready))
        self._device_status.setText(self._status_text(devices, ready))

    @staticmethod
    def _preferred_serial(ready: list[Device]) -> str | None:
        """USB is faster and more stable than Wi-Fi, so pick it first."""
        ordered = sorted(ready, key=lambda device: device.is_wireless)
        return ordered[0].serial if ordered else None

    @staticmethod
    def _status_text(devices: tuple[Device, ...], ready: list[Device]) -> str:
        if ready:
            return f"{len(ready)} cihaz hazır"
        if any(device.state == "unauthorized" for device in devices):
            return "Telefonda USB hata ayıklama iznini onayla"
        return "Cihaz yok. USB hata ayıklamayı açıp bağla"

    def _on_device_chosen(self, index: int) -> None:
        self._select_device(self._device_combo.itemData(index))

    def _select_device(self, serial: str | None) -> None:
        if serial == self._serial:
            return
        self._serial = serial
        index = self._device_combo.findData(serial)
        if index >= 0:
            self._device_combo.setCurrentIndex(index)
        service = DeviceService(self._adb, serial) if serial else None
        for *_, page in self._pages:
            page.set_service(service)
        self._phone.set_device(serial)
        if serial:
            self.notify("Cihaz bağlandı", "success")

    # --- window -------------------------------------------------------------
    def notify(self, message: str, kind: str = "info") -> None:
        self._toast.show_message(message, kind)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._toast.isVisible():
            self._toast.reposition()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._poll_timer.stop()
        for *_, page in self._pages:
            # Pages that own background processes (shell, logcat) stop them here.
            shutdown = getattr(page, "shutdown", None)
            if shutdown is not None:
                shutdown()
        self._phone.shutdown()
        super().closeEvent(event)

    def _apply_dark_title_bar(self) -> None:
        if sys.platform != "win32":
            return
        try:
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                int(self.winId()), DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
            )
        except (AttributeError, OSError) as exc:
            logger.debug("dark title bar unavailable: %s", exc)
