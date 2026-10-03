"""Overview page: device identity, live stats and quick actions."""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QMessageBox, QPushButton

from ...core.device import TCPIP_PORT
from ...core.models import DeviceInfo, ToggleState
from ...core.parsers import format_size
from ...core.scrcpy import protocol
from ...core.scrcpy.protocol import ControlType, Key
from .. import theme
from ..widgets import AppContext, Card, Page, StatCard, button, label

REFRESH_INTERVAL_MS = 5_000
ACTION_SETTLE_MS = 900
ACTION_COLUMNS = 4


@dataclass(frozen=True)
class Snapshot:
    """Everything the page shows, read in one background call."""

    info: DeviceInfo
    state: ToggleState
    is_wireless_connected: bool


class DashboardPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Genel Bakış", "Telefonunun anlık durumu ve hızlı işlemler")
        self._info = DeviceInfo()
        self._state = ToggleState()
        self._is_wireless_connected = False
        refresh = button("Yenile", theme.Glyph.REFRESH)
        refresh.clicked.connect(self.refresh)
        self.header.addWidget(refresh)

        self._lock_tile = self._tile(theme.Glyph.POWER, self._toggle_lock)
        self._display_tile = self._tile(theme.Glyph.SCREEN_OFF, self._toggle_display)
        self._mute_tile = self._tile(theme.Glyph.MUTE, self._toggle_mute)
        self._wireless_tile = self._tile(theme.Glyph.WIFI, self._toggle_wireless)
        self._build_hero()
        self._build_stats()
        self._build_actions()
        self._show_tiles()
        self.content.addStretch()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_if_visible)
        self._timer.start(REFRESH_INTERVAL_MS)

    def _build_hero(self) -> None:
        hero = Card("heroCard", spacing=6)
        hero.body.setContentsMargins(24, 22, 24, 22)
        self._title = label("Cihaz bağlı değil", "heroTitle")
        self._subtitle = label("USB hata ayıklamayı açıp telefonu bağla", "muted")
        chips = QHBoxLayout()
        chips.setSpacing(8)
        self._chips = [label("", "chip") for _ in range(3)]
        for chip in self._chips:
            chip.hide()
            chips.addWidget(chip)
        chips.addStretch()
        hero.body.addWidget(self._title)
        hero.body.addWidget(self._subtitle)
        hero.body.addSpacing(6)
        hero.body.addLayout(chips)
        self.content.addWidget(hero)

    def _build_stats(self) -> None:
        row = QHBoxLayout()
        row.setSpacing(16)
        self._battery = StatCard("Batarya", theme.Glyph.POWER)
        self._storage = StatCard("Depolama", theme.Glyph.FOLDER)
        self._memory = StatCard("Bellek", theme.Glyph.APPS)
        for card in (self._battery, self._storage, self._memory):
            row.addWidget(card)
        self.content.addLayout(row)

    @staticmethod
    def _tile(glyph: str, handler, text: str = "", is_toggle: bool = True) -> QPushButton:
        tile = button(text, glyph, "tile", icon_color=theme.ACCENT_ALT, icon_size=18)
        tile.setCheckable(is_toggle)
        tile.clicked.connect(lambda _checked=False: handler())
        return tile

    def _build_actions(self) -> None:
        self.content.addWidget(label("Hızlı İşlemler", "sectionTitle"))
        grid = QGridLayout()
        grid.setSpacing(12)
        send = self.context.send_control
        tiles = (
            self._lock_tile,
            self._display_tile,
            self._mute_tile,
            self._wireless_tile,
            self._tile(theme.Glyph.BELL, lambda: send(protocol.simple(ControlType.EXPAND_NOTIFICATION_PANEL)), "Bildirimler", False),
            self._tile(theme.Glyph.SLIDERS, lambda: send(protocol.simple(ControlType.EXPAND_SETTINGS_PANEL)), "Hızlı Ayarlar", False),
            self._tile(theme.Glyph.UP, lambda: send(protocol.simple(ControlType.COLLAPSE_PANELS)), "Panelleri Kapat", False),
            self._tile(theme.Glyph.RESTART, self._reboot, "Yeniden Başlat", False),
        )
        for index, tile in enumerate(tiles):
            grid.addWidget(tile, index // ACTION_COLUMNS, index % ACTION_COLUMNS)
        self.content.addLayout(grid)

    # --- data ---------------------------------------------------------------
    def set_service(self, service) -> None:
        super().set_service(service)
        self._display_tile.setChecked(False)
        if service is None:
            self._title.setText("Cihaz bağlı değil")
            self._subtitle.setText("USB hata ayıklamayı açıp telefonu bağla")
            for chip in self._chips:
                chip.hide()

    def _refresh_if_visible(self) -> None:
        if self.isVisible() and self.service is not None:
            self.refresh(is_quiet=True)

    def refresh(self, is_quiet: bool = False) -> None:
        service = self.service
        if service is None:
            return
        adb = self.context.adb

        def read() -> Snapshot:
            info = service.info()
            address = f"{info.ip_address}:{TCPIP_PORT}"
            is_wireless = any(d.serial == address and d.is_ready for d in adb.devices())
            return Snapshot(info, service.toggles(), is_wireless)

        self.read(read, self._show_snapshot, is_quiet)

    def _refresh_soon(self) -> None:
        """Confirm against the phone once it has had time to apply an action."""
        QTimer.singleShot(ACTION_SETTLE_MS, lambda: self.refresh(is_quiet=True))

    def _show_snapshot(self, snapshot: Snapshot) -> None:
        self._state = snapshot.state
        self._is_wireless_connected = snapshot.is_wireless_connected
        self._show_info(snapshot.info)
        self._show_tiles()

    def _show_tiles(self) -> None:
        """Make each toggle tile reflect what the phone reports."""
        state = self._state
        if not state.is_awake:
            # Sleeping resets the "panel off while mirroring" mode.
            self._display_tile.setChecked(False)
        self._display_tile.setEnabled(state.is_awake)
        is_dimmed = self._display_tile.isChecked()
        for tile, is_checked, on_text, off_text in (
            (self._lock_tile, not state.is_awake, "Kilitli · Uyandır", "Ekranı Kilitle"),
            (self._display_tile, is_dimmed, "Ekran Karartıldı · Aç", "Ekranı Karart"),
            (self._mute_tile, state.is_muted, "Sessizde · Sesi Aç", "Sesi Kapat"),
            (self._wireless_tile, self._is_wireless_connected, "Kablosuz Bağlı · Kes", "Kablosuz Bağlan"),
        ):
            tile.setChecked(is_checked)
            tile.setText(f" {on_text if is_checked else off_text}")

    def _show_info(self, info: DeviceInfo) -> None:
        self._info = info
        self._title.setText(f"{info.brand} {info.model}".strip())
        self._subtitle.setText(f"Seri: {self.service.serial}" if self.service else "")
        chip_texts = (
            f"Android {info.android_version} · API {info.sdk}",
            info.resolution,
            info.ip_address or "Wi-Fi kapalı",
        )
        for chip, text in zip(self._chips, chip_texts):
            chip.setText(text)
            chip.setVisible(bool(text))
        battery = info.battery
        charging = "Şarj oluyor" if battery.is_charging else "Pilde"
        self._battery.update_stat(
            f"%{battery.level}", f"{charging} · {battery.temperature_c:.0f}°C", battery.level
        )
        self._storage.update_stat(
            f"%{info.storage.percent}",
            f"{format_size(info.storage.used)} / {format_size(info.storage.total)}",
            info.storage.percent,
        )
        self._memory.update_stat(
            f"%{info.memory.percent}",
            f"{format_size(info.memory.used)} / {format_size(info.memory.total)}",
            info.memory.percent,
        )

    # --- actions ------------------------------------------------------------
    def _send_key(self, key: Key, expected: ToggleState) -> None:
        """Show the expected state at once, press the key, then confirm with the phone."""
        self.mark_changed()
        self._state = expected
        self._show_tiles()
        if not self.context.send_control(protocol.key_press(key)):
            service = self.service
            self.run(lambda: service.keyevent(key))
        self._refresh_soon()

    def _toggle_lock(self) -> None:
        self._send_key(Key.POWER, replace(self._state, is_awake=not self._state.is_awake))

    def _toggle_mute(self) -> None:
        self._send_key(Key.VOLUME_MUTE, replace(self._state, is_muted=not self._state.is_muted))

    def _toggle_display(self) -> None:
        should_dim = self._display_tile.isChecked()
        if not self.context.send_control(protocol.set_display_power(not should_dim)):
            self._display_tile.setChecked(not should_dim)
            self.context.notify("Ekran akışı bağlı değil.", "error")
        self._show_tiles()

    def _toggle_wireless(self) -> None:
        self._show_tiles()
        ip_address = self._info.ip_address
        if not ip_address:
            self.context.notify("Telefon Wi-Fi'ye bağlı değil.", "error")
            return
        address = f"{ip_address}:{TCPIP_PORT}"
        service = self.service
        if not self._is_wireless_connected:
            self.run(
                lambda: service.enable_wireless(ip_address),
                lambda _result: self._refresh_soon(),
                success=f"Kablosuz bağlantı açıldı: {ip_address}",
            )
        elif service.serial == address:
            self.context.notify("Şu an bu kablosuz bağlantı kullanılıyor; önce USB cihazı seç.", "error")
        else:
            adb = self.context.adb
            self.run(
                lambda: adb.disconnect(address),
                lambda _result: self._refresh_soon(),
                success="Kablosuz bağlantı kesildi",
            )

    def _reboot(self) -> None:
        answer = QMessageBox.question(
            self, "Yeniden başlat", "Telefon yeniden başlatılsın mı?"
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.run(self.service.reboot, success="Telefon yeniden başlatılıyor")
