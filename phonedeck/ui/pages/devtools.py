"""Developer tools page: display, debugging, power, ports, properties and intents."""

from __future__ import annotations

import re
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import devtools
from ...core.devtools import ForwardRule, IntentSpec, WmValue
from .. import theme
from ..widgets import AppContext, Card, Page, ToggleSwitch, button, label

ACTION_SETTLE_MS = 700
TREE_MIN_HEIGHT = 300
OUTPUT_MIN_HEIGHT = 110
EXTRAS_HEIGHT = 70
HISTORY_LIMIT = 10
HISTORY_HEIGHT = 120
SPIN_WIDTH = 120
VALUE_COLUMN_WIDTH = 260
FONT_STEP = 0.05
SOURCE_PROPERTIES = "props"
SECONDS_PER_MINUTE = 60
MS_PER_SECOND = 1_000
VALUE_PREVIEW_LIMIT = 300

TIMEOUT_CHOICES = (
    (15_000, "15 saniye"),
    (30_000, "30 saniye"),
    (60_000, "1 dakika"),
    (120_000, "2 dakika"),
    (300_000, "5 dakika"),
    (600_000, "10 dakika"),
    (1_800_000, "30 dakika"),
)
ANIMATION_LABELS = (
    ("Pencere animasyonu", devtools.ANIMATION_SCALES[0]),
    ("Geçiş animasyonu", devtools.ANIMATION_SCALES[1]),
    ("Animatör süresi", devtools.ANIMATION_SCALES[2]),
)
SCALE_PRESET_LABELS = {0.0: "Kapalı", 0.5: "0,5x", 1.0: "1x"}
POWER_GLYPHS = {
    "reboot": theme.Glyph.RESTART,
    "recovery": theme.Glyph.REFRESH,
    "bootloader": theme.Glyph.TERMINAL,
    "poweroff": theme.Glyph.POWER,
}
RULE_KINDS = (
    ("PC → Telefon (forward)", "forward"),
    ("Telefon → PC (reverse)", "reverse"),
)
RULE_KIND_TEXT = {"forward": "forward · PC → telefon", "reverse": "reverse · telefon → PC"}
SETTING_SOURCES = (
    ("Özellikler (getprop)", SOURCE_PROPERTIES),
    ("Ayarlar · global", "global"),
    ("Ayarlar · system", "system"),
    ("Ayarlar · secure", "secure"),
)
INTENT_KINDS = (("Etkinlik başlat (am start)", "activity"), ("Yayın gönder (am broadcast)", "broadcast"))

# Local styles: the global stylesheet has no tab bar or spin box rules.
TAB_STYLE = f"""
QTabWidget::pane {{ border: none; top: 8px; }}
QTabBar::tab {{
    background: transparent; color: {theme.MUTED}; padding: 9px 12px;
    margin-right: 4px; border-radius: 10px; font-weight: 600;
}}
QTabBar::tab:hover {{ background: {theme.CARD}; color: {theme.TEXT}; }}
QTabBar::tab:selected {{ background: rgba(124, 92, 255, 0.18); color: {theme.TEXT}; }}
"""
SPIN_STYLE = f"""
QSpinBox, QDoubleSpinBox {{
    background: {theme.BACKGROUND}; border: 1px solid {theme.BORDER}; border-radius: 10px;
    padding: 8px 12px; selection-background-color: {theme.ACCENT};
}}
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {theme.ACCENT}; }}
"""


def confirm(parent: QWidget, title: str, text: str, accept: str = "Onayla") -> bool:
    """Ask before a risky change; the text must name exactly what will happen."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(text)
    accepted = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Vazgeç", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    return box.clickedButton() is accepted


def describe_wm(value: WmValue, unit: str = "") -> str:
    text = f"{value.current or '—'}{unit}"
    if value.override:
        text += f"  (fiziksel {value.physical}{unit})"
    return text


def preview(value: str) -> str:
    flat = value.replace("\n", " ⏎ ")
    return flat if len(flat) <= VALUE_PREVIEW_LIMIT else flat[:VALUE_PREVIEW_LIMIT] + "…"


class _Tab(QWidget):
    """One tab of the page; runs device work through the owning page."""

    def __init__(self, page: DevToolsPage) -> None:
        super().__init__()
        self.page = page
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(16)
        self.body.addStretch()  # spare height goes below the cards

    def refresh(self) -> None:
        """Reload device state; tabs without readable state keep this no-op."""

    def card(self, title: str) -> Card:
        card = Card()
        card.body.addWidget(label(title, "sectionTitle"))
        self.body.insertWidget(self.body.count() - 1, card)
        return card

    def load(self, work: Callable, on_done: Callable, *args, is_quiet: bool = False) -> None:
        """Read from the phone; the result is dropped if the user acted meanwhile."""
        service = self.page.service
        if service is None:
            return
        adb = self.page.context.adb
        self.page.read(lambda: work(adb, service.serial, *args), on_done, is_quiet)

    def call(
        self, work: Callable, *args, success: str = "", then: Callable | None = None, reload: bool = True
    ) -> None:
        """Change the phone in the background, then confirm by reading it back."""
        service = self.page.service
        if service is None:
            return
        self.page.mark_changed()
        adb = self.page.context.adb
        if then is None and reload:
            then = lambda _result: self.refresh_soon()  # noqa: E731
        self.page.run(lambda: work(adb, service.serial, *args), then, success)

    def refresh_soon(self) -> None:
        QTimer.singleShot(ACTION_SETTLE_MS, lambda: self.refresh_quietly())

    def refresh_quietly(self) -> None:
        if self.page.isVisible() and self.page.service is not None:
            self.refresh()

    def toggle_row(self, card: Card, text: str, on_toggle: Callable[[bool], None]) -> ToggleSwitch:
        switch = ToggleSwitch()
        switch.toggled.connect(on_toggle)
        line = QHBoxLayout()
        line.addWidget(label(text))
        line.addStretch()
        line.addWidget(switch)
        card.body.addLayout(line)
        return switch

    @staticmethod
    def field_row(card: Card, *widgets: QWidget, is_compact: bool = False) -> None:
        line = QHBoxLayout()
        line.setSpacing(10)
        for widget in widgets:
            line.addWidget(widget)
        if is_compact:
            line.addStretch()
        card.body.addLayout(line)

    def check(self, validator: Callable, *args) -> bool:
        """Validate typed input before it is sent; report problems in Turkish."""
        try:
            validator(*args)
        except ValueError as exc:
            self.page.context.notify(str(exc), "error")
            return False
        return True


def make_spin(spin: QSpinBox | QDoubleSpinBox) -> QSpinBox | QDoubleSpinBox:
    spin.setStyleSheet(SPIN_STYLE)
    spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    spin.setFixedWidth(SPIN_WIDTH)
    return spin


class ScreenTab(_Tab):
    def __init__(self, page: DevToolsPage) -> None:
        super().__init__(page)
        self._size_info = label("—", "muted")
        self._density_info = label("—", "muted")
        self._size = QLineEdit()
        self._size.setPlaceholderText("1080x2400")
        self._density = make_spin(QSpinBox())
        self._density.setRange(devtools.MIN_DENSITY, devtools.MAX_DENSITY)
        self._density.setSuffix(" dpi")
        self._font = make_spin(QDoubleSpinBox())
        self._font.setRange(devtools.MIN_FONT_SCALE, devtools.MAX_FONT_SCALE)
        self._font.setSingleStep(FONT_STEP)
        self._font.setDecimals(2)
        self._timeout = QComboBox()
        for milliseconds, text in TIMEOUT_CHOICES:
            self._timeout.addItem(text, milliseconds)
        self._timeout.activated.connect(self._apply_timeout)
        self._build_resolution()
        self._build_appearance()

    def _build_resolution(self) -> None:
        card = self.card("Çözünürlük ve yoğunluk")
        card.body.addWidget(self._size_info)
        apply_size = button("Uygula", variant="primary")
        reset_size = button("Sıfırla")
        apply_size.clicked.connect(self._apply_size)
        reset_size.clicked.connect(lambda: self.call(devtools.reset_size, success="Çözünürlük sıfırlandı"))
        self._size.returnPressed.connect(self._apply_size)
        self.field_row(card, self._size, apply_size, reset_size)
        card.body.addWidget(self._density_info)
        apply_density = button("Uygula", variant="primary")
        reset_density = button("Sıfırla")
        apply_density.clicked.connect(self._apply_density)
        reset_density.clicked.connect(lambda: self.call(devtools.reset_density, success="Yoğunluk sıfırlandı"))
        self.field_row(card, self._density, apply_density, reset_density, is_compact=True)

    def _build_appearance(self) -> None:
        card = self.card("Görünüm ve ekran")
        self._dark = self.toggle_row(card, "Koyu mod", self._apply_dark)
        self._awake = self.toggle_row(card, "Şarjdayken ekran açık kalsın", self._apply_awake)
        apply_font = button("Uygula", variant="primary")
        apply_font.clicked.connect(self._apply_font)
        self.field_row(card, label("Yazı boyutu çarpanı"), self._font, apply_font, is_compact=True)
        self.field_row(card, label("Ekran kapanma süresi"), self._timeout, is_compact=True)

    def refresh(self) -> None:
        self.load(devtools.read_display, self._show, is_quiet=True)

    def _show(self, state: devtools.DisplayState) -> None:
        self._size_info.setText(f"Geçerli çözünürlük: {describe_wm(state.size)}")
        self._density_info.setText(f"Geçerli yoğunluk: {describe_wm(state.density, ' dpi')}")
        self._dark.set_checked_silently(state.night_mode == "yes")
        self._awake.set_checked_silently(state.stay_awake)
        if state.density.current.isdigit():
            self._density.setValue(int(state.density.current))
        if state.font_scale:
            self._font.setValue(state.font_scale)
        if state.screen_timeout_ms:
            self._select_timeout(state.screen_timeout_ms)

    def _select_timeout(self, milliseconds: int) -> None:
        index = self._timeout.findData(milliseconds)
        if index < 0:
            seconds = milliseconds // MS_PER_SECOND
            self._timeout.addItem(f"{seconds} saniye", milliseconds)
            index = self._timeout.count() - 1
        was_blocked = self._timeout.blockSignals(True)
        self._timeout.setCurrentIndex(index)
        self._timeout.blockSignals(was_blocked)

    def _apply_size(self) -> None:
        size = self._size.text().strip()
        if self.check(devtools.validate_size, size):
            self.call(devtools.set_size, size, success="Çözünürlük uygulandı")

    def _apply_density(self) -> None:
        self.call(devtools.set_density, self._density.value(), success="Yoğunluk uygulandı")

    def _apply_dark(self, is_on: bool) -> None:
        self.call(devtools.set_dark_mode, is_on)

    def _apply_awake(self, is_on: bool) -> None:
        self.call(devtools.set_stay_awake, is_on)

    def _apply_font(self) -> None:
        self.call(devtools.set_font_scale, self._font.value(), success="Yazı boyutu uygulandı")

    def _apply_timeout(self, index: int) -> None:
        self.call(devtools.set_screen_timeout, self._timeout.itemData(index))


class DebugTab(_Tab):
    def __init__(self, page: DevToolsPage) -> None:
        super().__init__(page)
        self._scale_labels: dict[str, QLabel] = {}
        card = self.card("Hata ayıklama göstergeleri")
        self._touches = self.toggle_row(card, "Dokunmaları göster", lambda on: self._flag("show_touches", on))
        self._pointer = self.toggle_row(card, "İşaretçi konumu", lambda on: self._flag("pointer_location", on))
        self._layout = self.toggle_row(card, "Yerleşim sınırları", self._apply_layout)
        self._build_animations()

    def _build_animations(self) -> None:
        card = self.card("Animasyon ölçekleri")
        for text, name in ANIMATION_LABELS:
            self._scale_labels[name] = label("—", "muted")
            self._animation_row(card, text, (name,), self._scale_labels[name])
        self._animation_row(card, "Tümü", devtools.ANIMATION_SCALES, None)

    def _animation_row(self, card: Card, text: str, names: tuple[str, ...], value: QLabel | None) -> None:
        line = QHBoxLayout()
        line.setSpacing(8)
        title = label(text)
        title.setMinimumWidth(150)
        line.addWidget(title)
        if value is not None:
            value.setMinimumWidth(50)
            line.addWidget(value)
        line.addStretch()
        for preset in devtools.ANIMATION_PRESETS:
            preset_button = button(SCALE_PRESET_LABELS[preset])
            preset_button.clicked.connect(lambda _c=False, p=preset, n=names: self._scale(p, n))
            line.addWidget(preset_button)
        card.body.addLayout(line)

    def refresh(self) -> None:
        self.load(devtools.read_debug, self._show, is_quiet=True)

    def _show(self, state: devtools.DebugState) -> None:
        self._touches.set_checked_silently(state.show_touches)
        self._pointer.set_checked_silently(state.pointer_location)
        self._layout.set_checked_silently(state.layout_bounds)
        for name, scale in zip(devtools.ANIMATION_SCALES, state.animation_scales):
            self._scale_labels[name].setText("—" if scale is None else f"{scale:g}x")

    def _flag(self, key: str, is_on: bool) -> None:
        self.call(devtools.set_debug_flag, key, is_on)

    def _apply_layout(self, is_on: bool) -> None:
        self.call(devtools.set_layout_bounds, is_on)

    def _scale(self, value: float, names: tuple[str, ...]) -> None:
        self.call(devtools.set_animation_scales, value, names, success="Animasyon ölçeği uygulandı")


class PowerTab(_Tab):
    def __init__(self, page: DevToolsPage) -> None:
        super().__init__(page)
        card = self.card("Güç işlemleri")
        card.body.addWidget(label("Bu işlemler telefonla bağlantıyı keser.", "muted"))
        for action in devtools.POWER_ACTIONS:
            variant = "danger" if action.key == "poweroff" else ""
            control = button(action.label, POWER_GLYPHS[action.key], variant)
            control.clicked.connect(lambda _c=False, a=action: self._run(a))
            card.body.addWidget(control, alignment=Qt.AlignmentFlag.AlignLeft)

    def _run(self, action: devtools.PowerAction) -> None:
        text = f"“{action.label}” işlemi yapılsın mı?\n\nTelefonla bağlantı kesilecek."
        if confirm(self, action.label, text):
            self.call(devtools.power, action.key, success=f"{action.label} gönderildi", reload=False)


class PortsTab(_Tab):
    def __init__(self, page: DevToolsPage) -> None:
        super().__init__(page)
        self._rules: tuple[ForwardRule, ...] = ()
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Tür", "Kaynak", "Hedef"])
        self._tree.setRootIsDecorated(False)
        self._tree.setMinimumHeight(TREE_MIN_HEIGHT // 2)
        remove = button("Seçileni kaldır", theme.Glyph.DELETE, "danger", icon_color=theme.DANGER)
        remove.clicked.connect(self._remove)
        reload_button = button("Yenile", theme.Glyph.REFRESH)
        reload_button.clicked.connect(self.refresh)
        card = self.card("Yönlendirme kuralları")
        card.body.addWidget(self._tree)
        self.field_row(card, reload_button, remove)
        self._build_add()

    def _build_add(self) -> None:
        card = self.card("Kural ekle")
        self._kind = QComboBox()
        for text, kind in RULE_KINDS:
            self._kind.addItem(text, kind)
        self._host = QLineEdit()
        self._host.setPlaceholderText("PC portu")
        self._device = QLineEdit()
        self._device.setPlaceholderText("Telefon portu")
        add = button("Ekle", theme.Glyph.ADD, "primary", icon_color="white")
        add.clicked.connect(self._add)
        self.field_row(card, self._kind, self._host, self._device, add)

    def refresh(self) -> None:
        self.load(devtools.list_rules, self._show)

    def _show(self, rules: tuple[ForwardRule, ...]) -> None:
        self._rules = rules
        self._tree.clear()
        for rule in rules:
            self._tree.addTopLevelItem(QTreeWidgetItem([RULE_KIND_TEXT[rule.kind], rule.source, rule.target]))

    def _add(self) -> None:
        host, device = self._host.text().strip(), self._device.text().strip()
        if not (self.check(devtools.validate_port, host) and self.check(devtools.validate_port, device)):
            return
        self.call(devtools.add_rule, self._kind.currentData(), host, device, success="Kural eklendi")

    def _remove(self) -> None:
        index = self._tree.indexOfTopLevelItem(self._tree.currentItem()) if self._tree.currentItem() else -1
        if index < 0:
            self.page.context.notify("Önce bir kural seçin.", "error")
            return
        self.call(devtools.remove_rule, self._rules[index], success="Kural kaldırıldı")


class SettingsTab(_Tab):
    def __init__(self, page: DevToolsPage) -> None:
        super().__init__(page)
        self._source = QComboBox()
        for text, key in SETTING_SOURCES:
            self._source.addItem(text, key)
        self._source.currentIndexChanged.connect(self._source_changed)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Ara: anahtar veya değer")
        self._search.textChanged.connect(self._filter)
        self._count = label("", "muted")
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Anahtar", "Değer"])
        self._tree.setRootIsDecorated(False)
        self._tree.setMinimumHeight(TREE_MIN_HEIGHT)
        self._tree.setColumnWidth(0, VALUE_COLUMN_WIDTH)
        self._tree.currentItemChanged.connect(lambda *_: self._sync_buttons())
        self._edit = button("Düzenle", theme.Glyph.SLIDERS)
        self._delete = button("Sil", theme.Glyph.DELETE, "danger", icon_color=theme.DANGER)
        self._edit.clicked.connect(self._edit_value)
        self._delete.clicked.connect(self._delete_key)
        reload_button = button("Yenile", theme.Glyph.REFRESH)
        reload_button.clicked.connect(self.refresh)
        card = self.card("Özellikler ve ayarlar")
        self.field_row(card, self._source, self._search)
        card.body.addWidget(self._tree)
        self.field_row(card, self._count, self._edit, self._delete, reload_button)
        self._sync_buttons()

    def _namespace(self) -> str:
        return self._source.currentData()

    def _source_changed(self) -> None:
        self.page.mark_changed()
        self.refresh()

    def refresh(self) -> None:
        namespace = self._namespace()
        if namespace == SOURCE_PROPERTIES:
            self.load(devtools.read_properties, self._show)
        else:
            self.load(devtools.read_settings, self._show, namespace)

    def _show(self, entries: tuple[devtools.Entry, ...]) -> None:
        self._tree.setUpdatesEnabled(False)
        self._tree.clear()
        for entry in entries:
            item = QTreeWidgetItem([entry.key, preview(entry.value)])
            item.setData(1, Qt.ItemDataRole.UserRole, entry.value)
            self._tree.addTopLevelItem(item)
        self._tree.setUpdatesEnabled(True)
        self._filter()

    def _filter(self) -> None:
        needle = self._search.text().strip().lower()
        shown = 0
        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            full = item.data(1, Qt.ItemDataRole.UserRole) or ""
            is_match = not needle or needle in item.text(0).lower() or needle in full.lower()
            item.setHidden(not is_match)
            shown += is_match
        self._count.setText(f"{shown} kayıt")

    def _sync_buttons(self) -> None:
        is_editable = self._namespace() != SOURCE_PROPERTIES and self._tree.currentItem() is not None
        self._edit.setEnabled(is_editable)
        self._delete.setEnabled(is_editable)

    def _selected(self) -> tuple[str, str, str] | None:
        item = self._tree.currentItem()
        if item is None or self._namespace() == SOURCE_PROPERTIES:
            return None
        return self._namespace(), item.text(0), item.data(1, Qt.ItemDataRole.UserRole) or ""

    def _edit_value(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        namespace, key, old = selected
        new, is_ok = QInputDialog.getText(self, "Ayarı düzenle", f"{namespace} · {key}", text=old)
        if not is_ok or new == old or not self.check(devtools.validate_key, key):
            return
        text = f"Alan: {namespace}\nAnahtar: {key}\nEski değer: {preview(old)}\nYeni değer: {preview(new)}"
        if confirm(self, "Ayar değiştirilsin mi?", text):
            self.call(devtools.put_setting, namespace, key, new, success="Ayar güncellendi")

    def _delete_key(self) -> None:
        selected = self._selected()
        if selected is None:
            return
        namespace, key, old = selected
        text = f"Alan: {namespace}\nAnahtar: {key}\nDeğer: {preview(old)}\n\nBu anahtar silinecek."
        if confirm(self, "Ayar silinsin mi?", text, "Sil"):
            self.call(devtools.delete_setting, namespace, key, success="Ayar silindi")


class IntentTab(_Tab):
    def __init__(self, page: DevToolsPage) -> None:
        super().__init__(page)
        self._history: list[IntentSpec] = []
        self._kind = QComboBox()
        for text, kind in INTENT_KINDS:
            self._kind.addItem(text, kind)
        self._action = self._line("Eylem  (örn. android.intent.action.VIEW)")
        self._data = self._line("Veri URI  (örn. https://example.com)")
        self._component = self._line("Bileşen  (örn. com.example.app/.MainActivity)")
        self._categories = self._line("Kategoriler  (boşlukla ayırın)")
        self._extras = QPlainTextEdit()
        self._extras.setPlaceholderText("Dize ekstraları, satır başına bir tane:  anahtar=değer")
        self._extras.setFixedHeight(EXTRAS_HEIGHT)
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setMinimumHeight(OUTPUT_MIN_HEIGHT)
        self._list = QListWidget()
        self._list.setFixedHeight(HISTORY_HEIGHT)
        self._list.itemDoubleClicked.connect(lambda _item: self._restore())
        self._build()

    @staticmethod
    def _line(placeholder: str) -> QLineEdit:
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        return field

    def _build(self) -> None:
        card = self.card("Intent gönder")
        for widget in (self._kind, self._action, self._data, self._component, self._categories, self._extras):
            card.body.addWidget(widget)
        send = button("Gönder", theme.Glyph.SEND, "primary", icon_color="white")
        send.clicked.connect(self._send)
        card.body.addWidget(send, alignment=Qt.AlignmentFlag.AlignLeft)
        card.body.addWidget(label("Çıktı", "muted"))
        card.body.addWidget(self._output)
        history = self.card("Son gönderilenler")
        history.body.addWidget(label("Alanlara geri yüklemek için çift tıklayın.", "muted"))
        history.body.addWidget(self._list)

    def _spec(self) -> IntentSpec:
        spec = IntentSpec(
            kind=self._kind.currentData(),
            action=self._action.text().strip(),
            data=self._data.text().strip(),
            component=self._component.text().strip(),
            categories=tuple(part for part in re.split(r"[,\s]+", self._categories.text()) if part),
            extras=devtools.parse_extras_text(self._extras.toPlainText()),
        )
        devtools.build_intent_command(spec)  # validates every field
        return spec

    def _send(self) -> None:
        try:
            spec = self._spec()
        except ValueError as exc:
            self.page.context.notify(str(exc), "error")
            return
        self.call(devtools.run_intent, spec, then=lambda output: self._done(spec, output))

    def _done(self, spec: IntentSpec, output: str) -> None:
        self._output.setPlainText(output or "(çıktı yok)")
        self._history = [spec, *[item for item in self._history if item != spec]][:HISTORY_LIMIT]
        self._list.clear()
        self._list.addItems([item.summary() for item in self._history])

    def _restore(self) -> None:
        row = self._list.currentRow()
        if not 0 <= row < len(self._history):
            return
        spec = self._history[row]
        self._kind.setCurrentIndex(max(self._kind.findData(spec.kind), 0))
        self._action.setText(spec.action)
        self._data.setText(spec.data)
        self._component.setText(spec.component)
        self._categories.setText(" ".join(spec.categories))
        self._extras.setPlainText("\n".join(f"{key}={value}" for key, value in spec.extras))


class DevToolsPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(
            context,
            "Geliştirici Araçları",
            "Ekran, hata ayıklama, güç, port yönlendirme, ayarlar ve intent",
        )
        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(TAB_STYLE)
        self._tabs.setDocumentMode(True)
        self._tabs.setUsesScrollButtons(False)
        for title, tab in (
            ("Ekran", ScreenTab(self)),
            ("Hata ayıklama", DebugTab(self)),
            ("Güç", PowerTab(self)),
            ("Port yönlendirme", PortsTab(self)),
            ("Özellikler ve ayarlar", SettingsTab(self)),
            ("Intent", IntentTab(self)),
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
