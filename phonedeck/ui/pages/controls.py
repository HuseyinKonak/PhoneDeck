"""Controls page: switches, brightness, volume, media keys, text and links."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider

from ...core.models import ToggleState
from ...core.scrcpy import protocol
from ...core.scrcpy.protocol import Key
from .. import theme
from ..widgets import AppContext, Card, Page, ToggleSwitch, button, label, round_button

SLIDER_DEBOUNCE_MS = 250
SLIDER_PAGE_STEPS = 10
PERCENT_LABEL_WIDTH = 40
REFRESH_INTERVAL_MS = 4_000
ACTION_SETTLE_MS = 900
MEDIA_BUTTON_SIZE = 46
MEDIA_ICON_SIZE = 16
SWITCH_COLUMNS = 2

# (glyph, label, ToggleState field, DeviceService setter)
SWITCHES = (
    (theme.Glyph.WIFI, "Wi-Fi", "wifi", "set_wifi"),
    (theme.Glyph.BLUETOOTH, "Bluetooth", "bluetooth", "set_bluetooth"),
    (theme.Glyph.GLOBE, "Mobil veri", "mobile_data", "set_mobile_data"),
    (theme.Glyph.ROTATE, "Otomatik döndür", "auto_rotate", "set_auto_rotate"),
    (theme.Glyph.BELL, "Rahatsız etme", "do_not_disturb", "set_do_not_disturb"),
)


class ControlsPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Kontroller", "Bağlantılar, ses, parlaklık ve metin gönderme")
        self._state = ToggleState()
        self._switches: dict[str, ToggleSwitch] = {}
        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._brightness = QSlider(Qt.Orientation.Horizontal)
        self._slider_parts: dict[QSlider, tuple[QTimer, QLabel]] = {}
        self._play = round_button(theme.Glyph.PLAY, "Oynat", MEDIA_BUTTON_SIZE)
        self._mute = round_button(theme.Glyph.MUTE, "Sesi kapat", MEDIA_BUTTON_SIZE)
        self._text = QLineEdit()
        self._url = QLineEdit()

        self.content.addWidget(self._build_switches())
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(self._build_levels(), 1)
        row.addWidget(self._build_media(), 1)
        self.content.addLayout(row)
        self.content.addWidget(self._build_text())
        self.content.addStretch()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_if_visible)
        self._timer.start(REFRESH_INTERVAL_MS)

    def _build_switches(self) -> Card:
        card = Card()
        card.body.addWidget(label("Hızlı Ayarlar", "sectionTitle"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(40)
        grid.setVerticalSpacing(14)
        for index, (glyph, text, field, setter) in enumerate(SWITCHES):
            switch = ToggleSwitch()
            switch.toggled.connect(
                lambda is_on, f=field, s=setter: self._apply_switch(f, s, is_on)
            )
            self._switches[field] = switch
            line = QHBoxLayout()
            line.setSpacing(10)
            line.addWidget(self._icon(glyph))
            line.addWidget(label(text))
            line.addStretch()
            line.addWidget(switch)
            grid.addLayout(line, index // SWITCH_COLUMNS, index % SWITCH_COLUMNS)
        card.body.addLayout(grid)
        return card

    def _build_levels(self) -> Card:
        card = Card()
        card.body.addWidget(label("Ses ve Parlaklık", "sectionTitle"))
        for glyph, slider, handler in (
            (theme.Glyph.VOLUME_UP, self._volume, self._apply_volume),
            (theme.Glyph.BRIGHTNESS, self._brightness, self._apply_brightness),
        ):
            percent = label("", "muted")
            percent.setFixedWidth(PERCENT_LABEL_WIDTH)
            percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            line = QHBoxLayout()
            line.setSpacing(12)
            line.addWidget(self._icon(glyph))
            line.addWidget(slider)
            line.addWidget(percent)
            # Debounced so drags, track clicks, wheel and keyboard all apply once.
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(SLIDER_DEBOUNCE_MS)
            timer.timeout.connect(handler)
            slider.valueChanged.connect(lambda _value, t=timer: (self.mark_changed(), t.start()))
            slider.valueChanged.connect(lambda _value, s=slider, p=percent: self._show_percent(s, p))
            self._slider_parts[slider] = (timer, percent)
            card.body.addLayout(line)
        card.body.addStretch()
        return card

    def _build_media(self) -> Card:
        card = Card()
        card.body.addWidget(label("Medya", "sectionTitle"))
        self._mute.setCheckable(True)
        previous = round_button(theme.Glyph.PREVIOUS, "Önceki", MEDIA_BUTTON_SIZE)
        following = round_button(theme.Glyph.NEXT, "Sonraki", MEDIA_BUTTON_SIZE)
        line = QHBoxLayout()
        line.setSpacing(12)
        for control, key in (
            (previous, Key.MEDIA_PREVIOUS),
            (self._play, Key.MEDIA_PLAY_PAUSE),
            (following, Key.MEDIA_NEXT),
            (self._mute, Key.VOLUME_MUTE),
        ):
            control.clicked.connect(lambda _checked=False, k=key: self._press(k))
            line.addWidget(control)
        line.addStretch()
        card.body.addLayout(line)
        card.body.addStretch()
        return card

    def _build_text(self) -> Card:
        card = Card()
        card.body.addWidget(label("Metin ve Bağlantı", "sectionTitle"))
        for field, placeholder, text, glyph, handler in (
            (self._text, "Telefondaki odaklı alana yazılacak metin", "Gönder", theme.Glyph.SEND, self._send_text),
            (self._url, "https://… telefonda açılacak adres", "Aç", theme.Glyph.GLOBE, self._open_url),
        ):
            line = QHBoxLayout()
            line.setSpacing(10)
            field.setPlaceholderText(placeholder)
            field.returnPressed.connect(handler)
            action = button(text, glyph, "primary", icon_color="white")
            action.clicked.connect(handler)
            line.addWidget(field)
            line.addWidget(action)
            card.body.addLayout(line)
        return card

    @staticmethod
    def _icon(glyph: str) -> QLabel:
        icon = QLabel()
        icon.setPixmap(theme.make_icon(glyph, theme.ACCENT_ALT, 18).pixmap(18, 18))
        return icon

    @staticmethod
    def _set_glyph(control: QPushButton, glyph: str, tooltip: str) -> None:
        control.setIcon(theme.make_icon(glyph, theme.TEXT, MEDIA_ICON_SIZE))
        control.setIconSize(QSize(MEDIA_ICON_SIZE, MEDIA_ICON_SIZE))
        control.setToolTip(tooltip)

    # --- data ---------------------------------------------------------------
    def _refresh_if_visible(self) -> None:
        if self.isVisible() and self.service is not None:
            self.refresh(is_quiet=True)

    def refresh(self, is_quiet: bool = False) -> None:
        if self.service is not None:
            self.read(self.service.toggles, self._show_state, is_quiet)

    def _refresh_soon(self) -> None:
        """Confirm against the phone once it has had time to apply an action."""
        QTimer.singleShot(ACTION_SETTLE_MS, lambda: self.refresh(is_quiet=True))

    def _show_state(self, state: ToggleState) -> None:
        self._state = state
        for field, switch in self._switches.items():
            switch.set_checked_silently(getattr(state, field))
        if state.is_playing:
            self._set_glyph(self._play, theme.Glyph.PAUSE, "Duraklat")
        else:
            self._set_glyph(self._play, theme.Glyph.PLAY, "Oynat")
        self._mute.setChecked(state.is_muted)
        self._set_glyph(
            self._mute,
            theme.Glyph.MUTE if state.is_muted else theme.Glyph.VOLUME_UP,
            "Sessizde · sesi aç" if state.is_muted else "Sesi kapat",
        )
        self._sync_slider(self._volume, 0, state.volume.maximum, state.volume.current)
        brightness = state.brightness
        self._sync_slider(self._brightness, brightness.minimum, brightness.maximum, brightness.current)

    def _sync_slider(self, slider: QSlider, minimum: int, maximum: int, value: int) -> None:
        """Show the phone's value unless the user is mid-change on this slider."""
        timer, percent = self._slider_parts[slider]
        if slider.isSliderDown() or timer.isActive():
            return
        was_blocked = slider.blockSignals(True)
        slider.setRange(minimum, maximum)
        slider.setPageStep(max((maximum - minimum) // SLIDER_PAGE_STEPS, 1))
        slider.setValue(value)
        slider.blockSignals(was_blocked)
        self._show_percent(slider, percent)

    @staticmethod
    def _show_percent(slider: QSlider, percent: QLabel) -> None:
        span = slider.maximum() - slider.minimum()
        ratio = (slider.value() - slider.minimum()) / span if span else 0
        percent.setText(f"%{round(ratio * 100)}")

    # --- actions ------------------------------------------------------------
    # The UI changes first; the phone is updated in the background and then re-read.
    def _apply_switch(self, field: str, setter: str, is_on: bool) -> None:
        self.mark_changed()
        self._state = replace(self._state, **{field: is_on})
        service = self.service
        self.run(lambda: getattr(service, setter)(is_on), lambda _result: self._refresh_soon())

    def _apply_volume(self) -> None:
        self.mark_changed()
        level = self._volume.value()
        self.run(lambda: self.service.set_volume(level), lambda _result: self._refresh_soon())

    def _apply_brightness(self) -> None:
        self.mark_changed()
        level = self._brightness.value()
        self.run(lambda: self.service.set_brightness(level), lambda _result: self._refresh_soon())

    def _press(self, key: Key) -> None:
        self.mark_changed()
        if key == Key.MEDIA_PLAY_PAUSE:
            self._show_state(replace(self._state, is_playing=not self._state.is_playing))
        elif key == Key.VOLUME_MUTE:
            self._show_state(replace(self._state, is_muted=not self._state.is_muted))
        if not self.context.send_control(protocol.key_press(key)):
            service = self.service
            self.run(lambda: service.keyevent(key))
        self._refresh_soon()

    def _send_text(self) -> None:
        value = self._text.text()
        if not value:
            return
        if self.context.send_control(protocol.text(value)):
            self._text.clear()
        else:
            self.context.notify("Ekran akışı bağlı değil.", "error")

    def _open_url(self) -> None:
        url = self._url.text().strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        self.run(lambda: self.service.open_url(url), success="Bağlantı telefonda açıldı")
