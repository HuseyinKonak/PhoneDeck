"""Apps page: browse apps and manage one in depth (permissions, state, data, APK)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRect, QSize, QStandardPaths, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
)

from ...core import appinfo
from ...core.adb import AdbError
from ...core.appinfo import AppDetails, Permission
from ...core.models import AppInfo
from ...core.parsers import format_size, parse_packages
from .. import theme
from ..widgets import AppContext, Page, button, label

logger = logging.getLogger(__name__)

ROW_HEIGHT = 56
AVATAR_SIZE = 36
DETAIL_ICON_SIZE = 56
DETAILS_MIN_WIDTH = 330
AVATAR_COLORS = ("#7C5CFF", "#22D3EE", "#34D399", "#FBBF24", "#F472B6", "#60A5FA")
_GENERIC_SEGMENTS = frozenset({"android", "app", "apps", "mobile", "client", "main"})
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]")

FILTER_USER = "user"
FILTER_SYSTEM = "system"
FILTER_DISABLED = "disabled"
FILTER_ALL = "all"
FILTERS = (
    ("Kullanıcı uygulamaları", FILTER_USER),
    ("Sistem uygulamaları", FILTER_SYSTEM),
    ("Devre dışı olanlar", FILTER_DISABLED),
    ("Tümü", FILTER_ALL),
)


def display_name(package: str) -> str:
    """Readable title derived from the package name (fallback when labels are unavailable)."""
    segments = [s for s in package.split(".")[1:] if s not in _GENERIC_SEGMENTS]
    name = segments[-1] if segments else package.rsplit(".", 1)[-1]
    return name.replace("_", " ").title()


def icon_cache_dir(serial: str) -> Path:
    """Per-device folder on this computer holding `<package>.png` icons."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
    return Path(base) / "icons" / _UNSAFE_PATH_CHARS.sub("_", serial)


@dataclass(frozen=True)
class AppList:
    apps: tuple[AppInfo, ...]
    disabled: frozenset[str]


class AppDelegate(QStyledItemDelegate):
    """Draws the app icon (or an initial avatar), its name and its package."""

    def __init__(self, parent, icons: dict[str, QPixmap], disabled: set[str]) -> None:
        super().__init__(parent)
        self._icons = icons
        self._disabled = disabled

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        app: AppInfo = index.data(Qt.ItemDataRole.UserRole)
        is_disabled = app.package in self._disabled
        rect = option.rect.adjusted(4, 3, -4, -3)
        painter.save()
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        painter.setPen(Qt.PenStyle.NoPen)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setBrush(QColor(124, 92, 255, 56))
            painter.drawRoundedRect(rect, 10, 10)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setBrush(QColor(theme.CARD_HOVER))
            painter.drawRoundedRect(rect, 10, 10)

        avatar = QRect(rect.x() + 10, rect.center().y() - AVATAR_SIZE // 2 + 1, AVATAR_SIZE, AVATAR_SIZE)
        bold = QFont(option.font)
        bold.setBold(True)
        painter.setFont(bold)
        painter.setOpacity(0.45 if is_disabled else 1.0)
        icon = self._icons.get(app.package)
        if icon is not None:
            painter.drawPixmap(avatar, icon)
        else:
            self._paint_avatar(painter, avatar, app)

        text_x = avatar.right() + 14
        text_width = rect.right() - text_x - 8
        painter.setPen(QColor(theme.TEXT))
        suffix = "  · devre dışı" if is_disabled else ""
        title = painter.fontMetrics().elidedText(app.label + suffix, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(
            QRect(text_x, rect.y() + 7, text_width, 20), Qt.AlignmentFlag.AlignVCenter, title
        )
        painter.setFont(option.font)
        painter.setPen(QColor(theme.MUTED))
        package = painter.fontMetrics().elidedText(app.package, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(
            QRect(text_x, rect.y() + 27, text_width, 18), Qt.AlignmentFlag.AlignVCenter, package
        )
        painter.restore()

    @staticmethod
    def _paint_avatar(painter: QPainter, avatar: QRect, app: AppInfo) -> None:
        color = AVATAR_COLORS[sum(map(ord, app.package)) % len(AVATAR_COLORS)]
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(avatar, 11, 11)
        painter.setPen(QColor("#0B0D12"))
        painter.drawText(avatar, Qt.AlignmentFlag.AlignCenter, app.label[:1].upper())


class DetailsPanel(QFrame):
    """Everything about the selected app plus its management actions."""

    action_requested = Signal(str)
    permission_toggled = Signal(str, bool)

    ACTIONS = (
        ("launch", "Aç", theme.Glyph.PLAY, "primary"),
        ("stop", "Durdur", theme.Glyph.STOP, ""),
        ("settings", "Telefonda Ayarlar", theme.Glyph.SLIDERS, ""),
        ("extract", "APK Çıkar", theme.Glyph.DOWNLOAD, ""),
        ("toggle_enabled", "Devre Dışı Bırak", theme.Glyph.SHIELD, ""),
        ("clear", "Verileri Temizle", theme.Glyph.DELETE, "danger"),
        ("uninstall", "Kaldır", theme.Glyph.DELETE, "danger"),
    )
    FACTS = ("Sürüm", "Boyut", "Hedef / En düşük SDK", "Kurulum", "Güncelleme", "Kaynak")

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("card")
        self.setMinimumWidth(DETAILS_MIN_WIDTH)
        self._is_filling = False
        self._buttons = {}
        self._icon = QLabel()
        self._icon.setFixedSize(DETAIL_ICON_SIZE, DETAIL_ICON_SIZE)
        self._title = label("Uygulama seç", "sectionTitle")
        self._title.setWordWrap(True)
        self._package = label("Ayrıntıları görmek için listeden bir uygulama seç.", "muted")
        self._package.setWordWrap(True)
        self._package.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._facts = {name: label("—") for name in self.FACTS}
        self._permission_title = label("İzinler", "sectionTitle")
        self._permissions = QListWidget()
        self._permissions.setMinimumHeight(140)
        self._permissions.itemChanged.connect(self._on_permission_changed)
        self._build_layout()
        self.clear()

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self._title)
        titles.addWidget(self._package)
        head.addWidget(self._icon)
        head.addLayout(titles, 1)
        layout.addLayout(head)

        facts = QGridLayout()
        facts.setHorizontalSpacing(14)
        facts.setVerticalSpacing(6)
        for row, (name, value) in enumerate(self._facts.items()):
            value.setWordWrap(True)
            facts.addWidget(label(name, "muted"), row, 0, Qt.AlignmentFlag.AlignTop)
            facts.addWidget(value, row, 1)
        facts.setColumnStretch(1, 1)
        layout.addLayout(facts)

        grid = QGridLayout()
        grid.setSpacing(8)
        for index, (key, text, glyph, variant) in enumerate(self.ACTIONS):
            color = "white" if variant == "primary" else theme.DANGER if variant == "danger" else theme.TEXT
            control = button(text, glyph, variant, icon_color=color)
            control.clicked.connect(lambda _checked=False, k=key: self.action_requested.emit(k))
            self._buttons[key] = control
            grid.addWidget(control, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addWidget(self._permission_title)
        layout.addWidget(self._permissions, 1)

    def clear(self) -> None:
        for control in self._buttons.values():
            control.setEnabled(False)
        self._permissions.clear()
        self._permission_title.setText("İzinler")
        for value in self._facts.values():
            value.setText("—")

    def show_app(self, app: AppInfo, icon: QPixmap | None) -> None:
        """Show what is known immediately; details arrive through `show_details`."""
        self.clear()
        self._title.setText(app.label)
        self._package.setText(app.package)
        if icon is not None:
            self._icon.setPixmap(icon.scaled(
                DETAIL_ICON_SIZE, DETAIL_ICON_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            self._icon.clear()
        for key in ("launch", "stop", "settings", "uninstall"):
            self._buttons[key].setEnabled(True)

    def show_details(self, details: AppDetails) -> None:
        sizes = details.sizes
        facts = {
            "Sürüm": f"{details.version_name} ({details.version_code})",
            "Boyut": (
                f"{format_size(sizes.total)}  ·  uygulama {format_size(sizes.app)}, "
                f"veri {format_size(sizes.data)}, önbellek {format_size(sizes.cache)}"
                if sizes.total else "Bilinmiyor"
            ),
            "Hedef / En düşük SDK": f"{details.target_sdk} / {details.min_sdk}",
            "Kurulum": details.first_install or "—",
            "Güncelleme": details.last_update or "—",
            "Kaynak": details.installer or ("Sistem" if details.is_system else "Bilinmiyor"),
        }
        for name, text in facts.items():
            self._facts[name].setText(text)
        for control in self._buttons.values():
            control.setEnabled(True)
        self._buttons["toggle_enabled"].setText(
            " Devre Dışı Bırak" if details.is_enabled else " Etkinleştir"
        )
        self._fill_permissions(details.permissions)

    def _fill_permissions(self, permissions: tuple[Permission, ...]) -> None:
        self._is_filling = True
        self._permissions.clear()
        granted = sum(1 for permission in permissions if permission.is_granted)
        self._permission_title.setText(f"İzinler  ·  {granted}/{len(permissions)} verildi")
        for permission in permissions:
            item = QListWidgetItem(permission.short_name)
            item.setToolTip(permission.name)
            item.setData(Qt.ItemDataRole.UserRole, permission.name)
            state = Qt.CheckState.Checked if permission.is_granted else Qt.CheckState.Unchecked
            item.setCheckState(state)
            if not permission.is_runtime:
                # Install-time permissions cannot be changed after installation.
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self._permissions.addItem(item)
        self._is_filling = False

    def _on_permission_changed(self, item: QListWidgetItem) -> None:
        if not self._is_filling:
            self.permission_toggled.emit(
                item.data(Qt.ItemDataRole.UserRole), item.checkState() == Qt.CheckState.Checked
            )


class AppsPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Uygulamalar", "İzinler, veriler, APK ve durum dahil ayrıntılı uygulama yönetimi")
        self._apps: tuple[AppInfo, ...] = ()
        self._icons: dict[str, QPixmap] = {}
        self._disabled: set[str] = set()
        self._details: AppDetails | None = None
        reload = button("Yenile", theme.Glyph.REFRESH)
        reload.clicked.connect(lambda: self.refresh())
        install = button("APK Yükle", theme.Glyph.ADD, "primary", icon_color="white")
        install.clicked.connect(self._install)
        self.header.addWidget(reload)
        self.header.addSpacing(8)
        self.header.addWidget(install)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Ad veya paket ara…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _text: self._apply_filter())
        self._filter = QComboBox()
        for text, value in FILTERS:
            self._filter.addItem(text, value)
        self._filter.currentIndexChanged.connect(lambda _index: self._on_filter_changed())
        self._count = label("", "muted")
        filters = QHBoxLayout()
        filters.setSpacing(12)
        filters.addWidget(self._search, 1)
        filters.addWidget(self._filter)
        filters.addWidget(self._count)
        self.content.addLayout(filters)

        self._list = QListWidget()
        self._list.setItemDelegate(AppDelegate(self._list, self._icons, self._disabled))
        self._list.setMouseTracking(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setMinimumHeight(420)
        self._list.currentItemChanged.connect(lambda _current, _previous: self._on_selected())
        self._list.itemDoubleClicked.connect(lambda _item: self._handle_action("launch"))
        self._panel = DetailsPanel()
        self._panel.action_requested.connect(self._handle_action)
        self._panel.permission_toggled.connect(self._toggle_permission)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._list)
        splitter.addWidget(self._panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.content.addWidget(splitter, 1)

    # --- list ---------------------------------------------------------------
    def set_service(self, service) -> None:
        if service is not self.service:
            # Icons and state belong to one device; never show them for another.
            self._icons.clear()
            self._disabled.clear()
            self._apps = ()
            self._details = None
            self._list.clear()
            self._panel.clear()
        super().set_service(service)

    def on_shown(self) -> None:
        # The list is cached; reopening the page must not reload every app.
        if not self._apps:
            self.refresh()

    def _needs_system_apps(self) -> bool:
        return self._filter.currentData() != FILTER_USER

    def _on_filter_changed(self) -> None:
        has_system = any(app.is_system for app in self._apps)
        if self._needs_system_apps() and not has_system:
            self.refresh()
        else:
            self._apply_filter()

    def refresh(self) -> None:
        service = self.service
        if service is None:
            return
        include_system = self._needs_system_apps()
        icon_dir = icon_cache_dir(service.serial)
        adb = self.context.adb
        if not self._apps:
            self._count.setText("Yükleniyor…")

        def load() -> AppList:
            disabled = frozenset(parse_packages(adb.shell(service.serial, "pm list packages -d")))
            try:
                apps = service.apps(include_system)
            except AdbError as exc:
                # Helper unsupported on this phone: fall back to package-derived names.
                logger.warning("app label helper failed: %s", exc)
                names = service.packages(include_system)
                return AppList(tuple(AppInfo(p, display_name(p)) for p in names), disabled)
            if any(not (icon_dir / f"{app.package}.png").is_file() for app in apps):
                icon_dir.mkdir(parents=True, exist_ok=True)
                try:
                    service.pull_icons(str(icon_dir))
                except AdbError as exc:
                    logger.warning("icon download failed: %s", exc)
            return AppList(apps, disabled)

        self.run(load, lambda result: self._show_apps(result, icon_dir))

    def _show_apps(self, result: AppList, icon_dir: Path) -> None:
        self._apps = tuple(sorted(result.apps, key=lambda app: (app.label.casefold(), app.package)))
        self._disabled.clear()
        self._disabled.update(result.disabled)
        ratio = self.devicePixelRatioF()
        side = round(DETAIL_ICON_SIZE * ratio)
        for app in self._apps:
            if app.package in self._icons:
                continue
            pixmap = QPixmap(str(icon_dir / f"{app.package}.png"))
            if pixmap.isNull():
                continue
            scaled = pixmap.scaled(
                side, side, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            scaled.setDevicePixelRatio(ratio)
            self._icons[app.package] = scaled
        self._apply_filter()

    def _matches(self, app: AppInfo, query: str, mode: str) -> bool:
        if mode == FILTER_USER and app.is_system:
            return False
        if mode == FILTER_SYSTEM and not app.is_system:
            return False
        if mode == FILTER_DISABLED and app.package not in self._disabled:
            return False
        return not query or query in app.package.casefold() or query in app.label.casefold()

    def _apply_filter(self) -> None:
        query = self._search.text().strip().casefold()
        mode = self._filter.currentData()
        selected = self._selected(is_silent=True)
        self._list.blockSignals(True)
        self._list.clear()
        for app in self._apps:
            if not self._matches(app, query, mode):
                continue
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, app)
            self._list.addItem(item)
            if selected is not None and app.package == selected.package:
                self._list.setCurrentItem(item)
        self._list.blockSignals(False)
        self._count.setText(f"{self._list.count()} uygulama")

    def _selected(self, is_silent: bool = False) -> AppInfo | None:
        item = self._list.currentItem()
        if item is None:
            if not is_silent:
                self.context.notify("Önce bir uygulama seç.", "error")
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    # --- details ------------------------------------------------------------
    def _on_selected(self) -> None:
        app = self._selected(is_silent=True)
        if app is None:
            return
        self._details = None
        self._panel.show_app(app, self._icons.get(app.package))
        self._load_details(app.package)

    def _load_details(self, package: str) -> None:
        adb, serial = self.context.adb, self.service.serial

        def show(details: AppDetails) -> None:
            current = self._selected(is_silent=True)
            if current is not None and current.package == details.package:
                self._details = details
                self._panel.show_details(details)

        self.run(lambda: appinfo.read_details(adb, serial, package), show)

    def _toggle_permission(self, permission: str, is_granted: bool) -> None:
        app = self._selected(is_silent=True)
        if app is None:
            return
        adb, serial = self.context.adb, self.service.serial
        verb = "verildi" if is_granted else "kaldırıldı"

        def change() -> str:
            """Returns an error message, or "" on success."""
            try:
                appinfo.set_permission(adb, serial, app.package, permission, is_granted)
            except (AdbError, ValueError) as exc:
                return str(exc)
            return ""

        def done(error: str) -> None:
            if error:
                self.context.notify(f"İzin değiştirilemedi: {error}", "error")
            else:
                self.context.notify(f"{permission.rsplit('.', 1)[-1]} izni {verb}", "success")
            # Either way, show what the phone really has now.
            self._load_details(app.package)

        self.run(change, done)

    # --- actions ------------------------------------------------------------
    def _handle_action(self, key: str) -> None:
        app = self._selected()
        if app is None:
            return
        handlers = {
            "launch": self._launch,
            "stop": self._stop,
            "settings": self._open_settings,
            "extract": self._extract,
            "toggle_enabled": self._toggle_enabled,
            "clear": self._clear_data,
            "uninstall": self._uninstall,
        }
        handlers[key](app)

    def _confirm(self, title: str, text: str) -> bool:
        answer = QMessageBox.warning(
            self, title, text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _launch(self, app: AppInfo) -> None:
        self.run(lambda: self.service.launch_app(app.package), success=f"{app.label} açıldı")

    def _stop(self, app: AppInfo) -> None:
        self.run(lambda: self.service.stop_app(app.package), success=f"{app.label} durduruldu")

    def _open_settings(self, app: AppInfo) -> None:
        adb, serial = self.context.adb, self.service.serial
        self.run(lambda: appinfo.open_settings(adb, serial, app.package))

    def _extract(self, app: AppInfo) -> None:
        details = self._details
        if details is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "APK'nın kaydedileceği klasör")
        if not folder:
            return
        adb, serial = self.context.adb, self.service.serial
        self.context.notify("APK çıkarılıyor…", "info")
        self.run(
            lambda: appinfo.extract_apks(adb, serial, details, folder),
            lambda saved: self.context.notify(f"{len(saved)} APK dosyası kaydedildi: {folder}", "success"),
        )

    def _toggle_enabled(self, app: AppInfo) -> None:
        details = self._details
        if details is None:
            return
        should_enable = not details.is_enabled
        if not should_enable and not self._confirm(
            "Devre dışı bırak",
            f"{app.label}\n{app.package}\n\nUygulama devre dışı bırakılsın mı? "
            "Sistem uygulamalarını kapatmak telefonun çalışmasını bozabilir.",
        ):
            return
        adb, serial = self.context.adb, self.service.serial

        def done(_result) -> None:
            (self._disabled.discard if should_enable else self._disabled.add)(app.package)
            self._list.viewport().update()
            self._load_details(app.package)

        self.run(
            lambda: appinfo.set_enabled(adb, serial, app.package, should_enable),
            done,
            success=f"{app.label} {'etkinleştirildi' if should_enable else 'devre dışı bırakıldı'}",
        )

    def _clear_data(self, app: AppInfo) -> None:
        if not self._confirm(
            "Verileri temizle",
            f"{app.label}\n{app.package}\n\nUygulamanın tüm verileri, hesapları ve ayarları "
            "kalıcı olarak silinecek. Bu işlem geri alınamaz.",
        ):
            return
        adb, serial = self.context.adb, self.service.serial
        self.run(
            lambda: appinfo.clear_data(adb, serial, app.package),
            lambda _result: self._load_details(app.package),
            success=f"{app.label} verileri temizlendi",
        )

    def _uninstall(self, app: AppInfo) -> None:
        if not self._confirm(
            "Uygulamayı kaldır",
            f"{app.label}\n{app.package}\n\nBu uygulama telefondan kaldırılsın mı?",
        ):
            return

        def done(_result) -> None:
            self._panel.clear()
            self.refresh()

        self.run(
            lambda: self.service.uninstall_app(app.package), done, success=f"{app.label} kaldırıldı"
        )

    def _install(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "APK seç", "", "Android paketi (*.apk)")
        if not paths:
            return
        service = self.service
        self.context.notify(f"{len(paths)} APK yükleniyor…", "info")

        def install() -> None:
            for path in paths:
                service.install_apk(path)

        self.run(install, lambda _result: self.refresh(), success="APK yüklendi")
