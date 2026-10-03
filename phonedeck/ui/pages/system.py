"""System settings page: connectivity, sound, screen, battery, date and privacy."""

from __future__ import annotations

import logging
from typing import Callable, Mapping

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import sysconfig
from ...core.adb import AdbError
from .. import theme
from ..widgets import AppContext, Card, Page, ToggleSwitch, button, label
from .devtools import TAB_STYLE, confirm, make_spin

logger = logging.getLogger(__name__)

ACTION_SETTLE_MS = 700
SLIDER_DEBOUNCE_MS = 250
SLIDER_PAGE_STEPS = 10
SLIDER_LABEL_WIDTH = 90
PERCENT_LABEL_WIDTH = 44
COMBO_MIN_WIDTH = 200
UNSUPPORTED_TIP = "Bu telefon bu ayarı desteklemiyor."
UNKNOWN_TEXT = "—"
NOT_REPORTED_TEXT = "Telefon bildirmiyor"
DNS_CHOICES = (
    ("Kapalı", sysconfig.DNS_OFF),
    ("Otomatik", sysconfig.DNS_AUTOMATIC),
    ("Özel sağlayıcı ana makine adı", sysconfig.DNS_HOSTNAME),
)
RINGER_CHOICES = (("Normal", "NORMAL"), ("Titreşim", "VIBRATE"), ("Sessiz", "SILENT"))
ROTATION_CHOICES = (("Dikey", 0), ("Yatay · 90°", 1), ("Ters dikey · 180°", 2), ("Yatay · 270°", 3))
NO_LIMIT_TEXT = "Sınır yok"
SIMULATION_WARNING = (
    "Dikkat: Bu işlemler yalnızca test içindir. Telefon, gerçek pil değerleri yerine "
    "buradaki değerleri bildirir ve gerçek şarj durumunu göstermez. Bitirince Sıfırla'ya basın."
)
BATTERY_STATUS_CHOICES = tuple((text, code) for code, text in sysconfig.BATTERY_STATUS.items())
SIMULATION_WARNING_STYLE = f"color: {theme.WARNING};"
PRIVACY_NOTE = (
    "Bilinmeyen kaynaklardan kurulum izni artık uygulama başına verilir: "
    "Ayarlar > Uygulamalar > Özel uygulama erişimi."
)
BLUETOOTH_NOTE = "Bluetooth adı bu uygulamadan değiştirilemez; telefonun Ayarlar bölümünü kullanın."


def yes_no(value: bool | None) -> str:
    return UNKNOWN_TEXT if value is None else ("Açık" if value else "Kapalı")


def text_or_dash(value: str) -> str:
    return value or UNKNOWN_TEXT


def note(text: str) -> QLabel:
    widget = label(text, "muted")
    widget.setWordWrap(True)
    return widget


def select_silently(combo: QComboBox, value: object) -> None:
    """Show the phone's choice without firing `activated`-style handlers."""
    index = combo.findData(value)
    if index < 0:
        return
    was_blocked = combo.blockSignals(True)
    combo.setCurrentIndex(index)
    combo.blockSignals(was_blocked)


def make_combo(choices: tuple[tuple[str, object], ...]) -> QComboBox:
    combo = QComboBox()
    combo.setMinimumWidth(COMBO_MIN_WIDTH)
    for text, data in choices:
        combo.addItem(text, data)
    return combo


class LevelSlider(QWidget):
    """Debounced slider row: the label and percent follow the handle, the phone follows later."""

    def __init__(self, text: str, on_change: Callable[[], None], on_commit: Callable[[int], None]) -> None:
        super().__init__()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self._percent = label("", "muted")
        self._percent.setFixedWidth(PERCENT_LABEL_WIDTH)
        self._percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title = label(text)
        title.setFixedWidth(SLIDER_LABEL_WIDTH)
        line = QHBoxLayout(self)
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(12)
        for widget in (title, self.slider, self._percent):
            line.addWidget(widget)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SLIDER_DEBOUNCE_MS)
        self._timer.timeout.connect(lambda: on_commit(self.slider.value()))
        self.slider.valueChanged.connect(lambda _value: (on_change(), self._timer.start()))
        self.slider.valueChanged.connect(lambda _value: self._show_percent())
        self.set_unavailable()

    def set_unavailable(self) -> None:
        self.setEnabled(False)
        self.setToolTip(UNSUPPORTED_TIP)

    def show_value(self, minimum: int, maximum: int, value: int) -> None:
        """Show the phone's value unless the user is mid-change on this slider."""
        self.setEnabled(True)
        self.setToolTip("")
        if self.slider.isSliderDown() or self._timer.isActive():
            return
        was_blocked = self.slider.blockSignals(True)
        self.slider.setRange(minimum, maximum)
        self.slider.setPageStep(max((maximum - minimum) // SLIDER_PAGE_STEPS, 1))
        self.slider.setValue(value)
        self.slider.blockSignals(was_blocked)
        self._show_percent()

    def _show_percent(self) -> None:
        span = self.slider.maximum() - self.slider.minimum()
        ratio = (self.slider.value() - self.slider.minimum()) / span if span else 0
        self._percent.setText(f"%{round(ratio * 100)}")


class _Tab(QWidget):
    """One tab of the page; subclasses set `READER` and implement `_show`."""

    READER: Callable = staticmethod(lambda adb, serial: None)

    def __init__(self, page: SystemSettingsPage) -> None:
        super().__init__()
        self.page = page
        self._switches: dict[str, ToggleSwitch] = {}
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(16)
        self.body.addStretch()  # spare height goes below the cards

    def refresh(self, is_quiet: bool = False) -> None:
        """Reload this tab from the phone; the result is dropped if the user acted meanwhile."""
        service = self.page.service
        if service is None:
            return
        adb = self.page.context.adb
        self.page.read(lambda: self.READER(adb, service.serial), self._show, is_quiet)

    def _show(self, state) -> None:
        raise NotImplementedError

    def card(self, title: str) -> Card:
        card = Card()
        card.body.addWidget(label(title, "sectionTitle"))
        self.body.insertWidget(self.body.count() - 1, card)
        return card

    def call(self, work: Callable, *args, success: str = "", reload: bool = True) -> None:
        """Change the phone in the background, report problems, then read it back."""
        service = self.page.service
        if service is None:
            return
        self.page.mark_changed()
        adb = self.page.context.adb

        def guarded() -> str:
            try:
                work(adb, service.serial, *args)
            except ValueError as exc:
                return str(exc)
            except AdbError as exc:
                logger.warning("system setting change failed: %s", exc)
                return f"Telefon ayarı değiştiremedi: {exc}"
            return ""

        def finished(error: str) -> None:
            if error:
                self.page.context.notify(error, "error")
            elif success:
                self.page.context.notify(success, "success")
            if reload:
                QTimer.singleShot(ACTION_SETTLE_MS, self._refresh_quietly)

        self.page.run(guarded, finished)

    def _refresh_quietly(self) -> None:
        if self.page.isVisible() and self.page.service is not None:
            self.refresh(is_quiet=True)

    def check(self, validator: Callable, *args) -> bool:
        """Validate typed input before it is sent; report problems in Turkish."""
        try:
            validator(*args)
        except ValueError as exc:
            self.page.context.notify(str(exc), "error")
            return False
        return True

    # --- shared rows --------------------------------------------------------
    def switch_card(self, title: str, toggles: tuple[sysconfig.Toggle, ...]) -> Card:
        card = self.card(title)
        for toggle in toggles:
            switch = ToggleSwitch()
            switch.setEnabled(False)
            switch.toggled.connect(lambda is_on, key=toggle.key: self.call(sysconfig.set_toggle, key, is_on))
            self._switches[toggle.key] = switch
            line = QHBoxLayout()
            title_label = label(toggle.label)
            if toggle.tooltip:
                title_label.setToolTip(toggle.tooltip)
            line.addWidget(title_label)
            line.addStretch()
            line.addWidget(switch)
            card.body.addLayout(line)
        return card

    def show_toggles(self, states: Mapping[str, bool | None]) -> None:
        for key, switch in self._switches.items():
            state = states.get(key)
            switch.setEnabled(state is not None)
            switch.setToolTip("" if state is not None else UNSUPPORTED_TIP)
            switch.set_checked_silently(bool(state))

    @staticmethod
    def info_row(card: Card, title: str) -> QLabel:
        value = label(UNKNOWN_TEXT, "muted")
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        line = QHBoxLayout()
        line.addWidget(label(title))
        line.addStretch()
        line.addWidget(value)
        card.body.addLayout(line)
        return value

    @staticmethod
    def field_row(card: Card, *widgets: QWidget, is_compact: bool = True) -> None:
        line = QHBoxLayout()
        line.setSpacing(10)
        for widget in widgets:
            line.addWidget(widget)
        if is_compact:
            line.addStretch()
        card.body.addLayout(line)


class ConnectivityTab(_Tab):
    READER = staticmethod(sysconfig.read_connectivity)

    def __init__(self, page: SystemSettingsPage) -> None:
        super().__init__(page)
        self.switch_card("Bağlantılar", sysconfig.CONNECTIVITY_TOGGLES)
        self._dns_mode = make_combo(DNS_CHOICES)
        self._dns_host = QLineEdit()
        self._dns_host.setPlaceholderText("dns.example.com")
        self._dns_mode.activated.connect(lambda _index: self._sync_dns_field())
        apply_dns = button("Uygula", variant="primary")
        apply_dns.clicked.connect(self._apply_dns)
        self._dns_host.returnPressed.connect(self._apply_dns)
        dns = self.card("Özel DNS")
        dns.body.addWidget(note("Özel DNS, alan adı sorgularını şifreli gönderir."))
        self.field_row(dns, self._dns_mode, apply_dns)
        dns.body.addWidget(self._dns_host)
        info = self.card("Durum")
        self._bluetooth = self.info_row(info, "Bluetooth adı")
        self._usb = self.info_row(info, "USB ile internet paylaşımı")
        self._hotspot = self.info_row(info, "Kablosuz erişim noktası")
        info.body.addWidget(note(BLUETOOTH_NOTE))
        self._sync_dns_field()

    def _show(self, state: sysconfig.ConnectivityState) -> None:
        self.show_toggles(state.toggles)
        select_silently(self._dns_mode, state.dns_mode)
        if not self._dns_host.hasFocus():
            self._dns_host.setText(state.dns_host)
        self._sync_dns_field()
        self._bluetooth.setText(text_or_dash(state.bluetooth_name))
        self._usb.setText(yes_no(state.usb_tethering))
        self._hotspot.setText(yes_no(state.hotspot))

    def _sync_dns_field(self) -> None:
        self._dns_host.setEnabled(self._dns_mode.currentData() == sysconfig.DNS_HOSTNAME)

    def _apply_dns(self) -> None:
        mode, host = self._dns_mode.currentData(), self._dns_host.text().strip()
        if mode == sysconfig.DNS_HOSTNAME and not self.check(sysconfig.validate_hostname, host):
            return
        self.call(sysconfig.set_private_dns, mode, host, success="Özel DNS uygulandı")


class SoundTab(_Tab):
    READER = staticmethod(sysconfig.read_sound)

    def __init__(self, page: SystemSettingsPage) -> None:
        super().__init__(page)
        self._sliders: dict[str, LevelSlider] = {}
        volumes = self.card("Ses düzeyleri")
        for stream in sysconfig.AUDIO_STREAMS:
            slider = LevelSlider(
                stream.label, page.mark_changed, lambda value, key=stream.key: self._commit_volume(key, value)
            )
            self._sliders[stream.key] = slider
            volumes.body.addWidget(slider)
        self._ringer = make_combo(RINGER_CHOICES)
        self._ringer.activated.connect(lambda _index: self.call(sysconfig.set_ringer_mode, self._ringer.currentData()))
        ringer = self.card("Zil kipi")
        self.field_row(ringer, self._ringer)
        self.switch_card("Sistem sesleri", sysconfig.SOUND_TOGGLES)

    def _commit_volume(self, key: str, value: int) -> None:
        slider = self._sliders[key].slider
        self.call(sysconfig.set_stream_volume, key, value, slider.minimum(), slider.maximum())

    def _show(self, state: sysconfig.SoundState) -> None:
        self.show_toggles(state.toggles)
        found = {volume.key: volume for volume in state.streams}
        for key, slider in self._sliders.items():
            volume = found.get(key)
            if volume is None:
                slider.set_unavailable()
            else:
                slider.show_value(volume.minimum, volume.maximum, volume.current)
        is_known = bool(state.ringer)
        self._ringer.setEnabled(is_known)
        self._ringer.setToolTip("" if is_known else UNSUPPORTED_TIP)
        select_silently(self._ringer, state.ringer)


class ScreenTab(_Tab):
    READER = staticmethod(sysconfig.read_screen)

    def __init__(self, page: SystemSettingsPage) -> None:
        super().__init__(page)
        self._rates: tuple[int, ...] = ()
        self.switch_card("Ekran", sysconfig.SCREEN_TOGGLES)
        self._rotation = make_combo(ROTATION_CHOICES)
        self._rotation.activated.connect(lambda _index: self.call(sysconfig.set_rotation, self._rotation.currentData()))
        self._peak = make_combo(())
        self._min = make_combo(())
        self._peak.activated.connect(lambda _index: self._apply_rate("peak", self._peak))
        self._min.activated.connect(lambda _index: self._apply_rate("min", self._min))
        orientation = self.card("Yön")
        orientation.body.addWidget(note("Sabit yön, yalnızca döndürme kilidi açıkken geçerlidir."))
        self.field_row(orientation, label("Zorunlu yön"), self._rotation)
        rates = self.card("Yenileme hızı")
        self.field_row(rates, label("En yüksek"), self._peak)
        self.field_row(rates, label("En düşük"), self._min)
        info = self.card("Gezinme")
        self._navigation = self.info_row(info, "Gezinme kipi")

    def _show(self, state: sysconfig.ScreenState) -> None:
        self.show_toggles(state.toggles)
        has_rotation = state.rotation is not None
        self._rotation.setEnabled(has_rotation)
        self._rotation.setToolTip("" if has_rotation else UNSUPPORTED_TIP)
        select_silently(self._rotation, state.rotation)
        self._show_rates(state)
        self._navigation.setText(text_or_dash(state.navigation))

    def _show_rates(self, state: sysconfig.ScreenState) -> None:
        if state.rates != self._rates:
            self._rates = state.rates
            self._refill(self._peak, [(f"{rate} Hz", rate) for rate in state.rates])
            self._refill(self._min, [(NO_LIMIT_TEXT, 0)] + [(f"{rate} Hz", rate) for rate in state.rates])
        for combo, value in ((self._peak, state.peak_rate), (self._min, state.min_rate or 0)):
            combo.setEnabled(bool(state.rates))
            combo.setToolTip("" if state.rates else UNSUPPORTED_TIP)
            select_silently(combo, value)

    @staticmethod
    def _refill(combo: QComboBox, choices: list[tuple[str, int]]) -> None:
        was_blocked = combo.blockSignals(True)
        combo.clear()
        for text, data in choices:
            combo.addItem(text, data)
        combo.blockSignals(was_blocked)

    def _apply_rate(self, limit: str, combo: QComboBox) -> None:
        self.call(sysconfig.set_refresh_limit, limit, combo.currentData(), self._rates)


class BatteryTab(_Tab):
    READER = staticmethod(sysconfig.read_battery)

    def __init__(self, page: SystemSettingsPage) -> None:
        super().__init__(page)
        self.switch_card("Pil ayarları", sysconfig.BATTERY_TOGGLES)
        facts = self.card("Pil sağlığı")
        self._facts = {
            key: self.info_row(facts, title)
            for key, title in (
                ("level", "Şarj düzeyi"),
                ("status", "Durum"),
                ("health", "Sağlık"),
                ("technology", "Teknoloji"),
                ("voltage", "Gerilim"),
                ("temperature", "Sıcaklık"),
                ("cycles", "Şarj döngüsü"),
            )
        }
        self._build_simulation()

    def _build_simulation(self) -> None:
        card = self.card("Pil simülasyonu")
        warning = label(SIMULATION_WARNING)
        warning.setWordWrap(True)
        warning.setStyleSheet(SIMULATION_WARNING_STYLE)
        reset = button("Sıfırla", theme.Glyph.RESTART, "primary", icon_color="white")
        reset.clicked.connect(lambda: self.call(sysconfig.simulate_battery, "reset", success="Pil simülasyonu sıfırlandı"))
        self._simulated = label("", "muted")
        self.field_row(card, reset, self._simulated)
        card.body.addWidget(warning)
        self._level = make_spin(QSpinBox())
        self._level.setRange(sysconfig.MIN_BATTERY_LEVEL, sysconfig.MAX_BATTERY_LEVEL)
        self._level.setSuffix(" %")
        self._status = make_combo(BATTERY_STATUS_CHOICES)
        apply_level = button("Seviyeyi uygula")
        apply_level.clicked.connect(lambda: self._simulate("level", self._level.value(), f"Pil seviyesi %{self._level.value()}"))
        apply_status = button("Durumu uygula")
        apply_status.clicked.connect(
            lambda: self._simulate("status", self._status.currentData(), f"Pil durumu “{self._status.currentText()}”")
        )
        unplug = button("Fişi çek", tooltip="Telefonu şarjdan ayrılmış gibi gösterir")
        unplug.clicked.connect(lambda: self._simulate("unplug", None, "Telefon şarjdan ayrılmış gibi gösterilecek"))
        self.field_row(card, self._level, apply_level)
        self.field_row(card, self._status, apply_status)
        self.field_row(card, unplug)

    def _simulate(self, action: str, value: int | None, summary: str) -> None:
        if confirm(self, "Pil simülasyonu", f"{summary}.\n\nGerçek pil değerleri gizlenecek.", "Uygula"):
            self.call(sysconfig.simulate_battery, action, value, success="Simülasyon uygulandı")

    def _show(self, state: sysconfig.BatteryState) -> None:
        self.show_toggles(state.toggles)
        facts = self._facts
        facts["level"].setText(UNKNOWN_TEXT if state.level is None else f"%{state.level}")
        facts["status"].setText(text_or_dash(state.status))
        facts["health"].setText(text_or_dash(state.health))
        facts["technology"].setText(text_or_dash(state.technology))
        facts["voltage"].setText(UNKNOWN_TEXT if state.voltage_mv is None else f"{state.voltage_mv} mV")
        facts["temperature"].setText(UNKNOWN_TEXT if state.temperature_c is None else f"{state.temperature_c:.1f} °C")
        facts["cycles"].setText(NOT_REPORTED_TEXT if state.cycles is None else str(state.cycles))
        self._simulated.setText("Simülasyon etkin" if state.is_simulated else "")
        self._simulated.setStyleSheet(SIMULATION_WARNING_STYLE if state.is_simulated else "")


class DateTab(_Tab):
    READER = staticmethod(sysconfig.read_date)

    def __init__(self, page: SystemSettingsPage) -> None:
        super().__init__(page)
        self.switch_card("Saat", sysconfig.DATE_TOGGLES)
        info = self.card("Geçerli bölge")
        self._timezone = self.info_row(info, "Saat dilimi")
        self._locale = self.info_row(info, "Dil ve bölge")

    def _show(self, state: sysconfig.DateState) -> None:
        self.show_toggles(state.toggles)
        self._timezone.setText(text_or_dash(state.timezone))
        self._locale.setText(text_or_dash(state.locale))


class PrivacyTab(_Tab):
    READER = staticmethod(sysconfig.read_privacy)

    def __init__(self, page: SystemSettingsPage) -> None:
        super().__init__(page)
        card = self.switch_card("Gizlilik ve güvenlik", sysconfig.PRIVACY_TOGGLES)
        card.body.addWidget(note(PRIVACY_NOTE))
        info = self.card("Hata ayıklama güvenliği")
        self._facts = {
            key: self.info_row(info, title)
            for key, title in (
                ("usb_debugging", "USB hata ayıklama"),
                ("wireless_debugging", "Kablosuz hata ayıklama"),
                ("developer_options", "Geliştirici seçenekleri"),
                ("adb_secure", "Bilgisayar yetkilendirmesi (RSA)"),
            )
        }

    def _show(self, state: sysconfig.PrivacyState) -> None:
        self.show_toggles(state.toggles)
        for key, fact in self._facts.items():
            fact.setText(yes_no(getattr(state, key)))
        if state.adb_secure is not None:
            self._facts["adb_secure"].setText("Gerekli" if state.adb_secure else "Gerekmiyor")


class SystemSettingsPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(
            context,
            "Sistem Ayarları",
            "Bağlantı, ses, ekran, pil, tarih ve gizlilik ayarları",
        )
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(TAB_STYLE)
        self._tabs.setDocumentMode(True)
        self._tabs.setUsesScrollButtons(False)
        for title, tab in (
            ("Bağlantı", ConnectivityTab(self)),
            ("Ses", SoundTab(self)),
            ("Ekran", ScreenTab(self)),
            ("Pil", BatteryTab(self)),
            ("Tarih ve dil", DateTab(self)),
            ("Gizlilik ve güvenlik", PrivacyTab(self)),
        ):
            self._tabs.addTab(tab, title)
        self._tabs.currentChanged.connect(lambda _index: self.refresh())
        self.content.addWidget(self._tabs)
        self.content.addStretch()

    def refresh(self) -> None:
        tab = self._tabs.currentWidget()
        if self.service is not None and isinstance(tab, _Tab):
            tab.refresh()

    def show_tab(self, index: int) -> None:
        """Select a tab by position (used for previews and tests)."""
        self._tabs.setCurrentIndex(index)
