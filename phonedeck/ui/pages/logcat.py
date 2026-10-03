"""Logcat page: live, filterable system log of the phone."""

from __future__ import annotations

import logging
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from ...core.adb import AdbError
from ...core.logcat import (
    BUFFERS,
    DEFAULT_BUFFER,
    LEVELS,
    TAIL_LINES,
    LogFilter,
    LogLine,
    LogcatReader,
    clear_device_buffer,
    format_line,
    resolve_package_pids,
)
from .. import theme
from ..widgets import AppContext, Card, Page, button, label

logger = logging.getLogger(__name__)

MAX_DISPLAY_LINES = 10000
MAX_BUFFERED_LINES = 50000
MAX_LINES_PER_FLUSH = 1500
FLUSH_INTERVAL_MS = 60
FILTER_DEBOUNCE_MS = 250
OUTPUT_MIN_HEIGHT = 420
LOG_FONT_FAMILIES = ("Cascadia Mono", "Consolas", "monospace")
LOG_FONT_SIZE = 12
LEVEL_COLORS = {
    "V": theme.MUTED,
    "D": theme.ACCENT_ALT,
    "I": theme.TEXT,
    "W": theme.WARNING,
    "E": theme.DANGER,
    "F": theme.DANGER,
}
BUFFER_LABELS = {
    "main": "Ana (main)",
    "system": "Sistem (system)",
    "crash": "Çökme (crash)",
    "events": "Olaylar (events)",
    "all": "Tümü (all)",
}
LEVEL_LABELS = {
    "V": "Ayrıntılı (V)",
    "D": "Hata ayıklama (D)",
    "I": "Bilgi (I)",
    "W": "Uyarı (W)",
    "E": "Hata (E)",
    "F": "Ölümcül (F)",
}
INVALID_FIELD_STYLE = f"border-color: {theme.DANGER};"


class _ReaderBridge(QObject):
    """Carries reader-thread callbacks onto the UI thread."""

    batch = Signal(int, object)
    stopped = Signal(int, str)


class LogView(QPlainTextEdit):
    """Read-only monospace view that paints each line in its level color."""

    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setMaximumBlockCount(MAX_DISPLAY_LINES)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont(list(LOG_FONT_FAMILIES))
        font.setPixelSize(LOG_FONT_SIZE)
        self.setFont(font)
        self._formats = {level: self._make_format(color) for level, color in LEVEL_COLORS.items()}
        self._is_empty = True

    @staticmethod
    def _make_format(color: str) -> QTextCharFormat:
        text_format = QTextCharFormat()
        text_format.setForeground(QColor(color))
        return text_format

    def append_lines(self, lines: list[LogLine], should_follow: bool) -> None:
        """Add many records in one edit block so the view repaints once."""
        if not lines:
            return
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.beginEditBlock()
        for line in lines:
            if not self._is_empty:
                cursor.insertBlock()
            cursor.insertText(format_line(line), self._formats.get(line.level, self._formats["I"]))
            self._is_empty = False
        cursor.endEditBlock()
        if should_follow:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def reset(self) -> None:
        self.clear()
        self._is_empty = True


class LogcatPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Logcat", "Telefonun sistem günlüğünü canlı izle ve filtrele")
        self._buffer: deque[LogLine] = deque(maxlen=MAX_BUFFERED_LINES)
        self._pending: deque[LogLine] = deque(maxlen=MAX_BUFFERED_LINES)
        self._shown = 0
        self._reader: LogcatReader | None = None
        self._generation = 0
        self._wants_running = True
        self._pids: frozenset[int] | None = None
        self._matches = LogFilter().matcher()
        self._replay_cutoff: tuple[str, frozenset[LogLine]] | None = None
        self._replay_left = 0

        self._bridge = _ReaderBridge()
        self._bridge.batch.connect(self._on_batch)
        self._bridge.stopped.connect(self._on_reader_stopped)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(FILTER_DEBOUNCE_MS)
        self._filter_timer.timeout.connect(self._apply_filter)

        self._build_toolbar()
        self._output = LogView()
        self._output.setMinimumHeight(OUTPUT_MIN_HEIGHT)
        self._output.setPlaceholderText("Günlük satırları burada görünecek. Başlat'a basarak akışı aç.")
        self.content.addWidget(self._output, 1)
        self._update_counter()

    # --- layout -------------------------------------------------------------
    def _build_toolbar(self) -> None:
        card = Card(spacing=10)
        self._toggle = button("Duraklat", theme.Glyph.PAUSE, "primary", icon_color="white")
        self._toggle.clicked.connect(self._toggle_running)
        clear_view = button("Temizle", theme.Glyph.DELETE, tooltip="Ekrandaki ve bellekteki satırları siler")
        clear_view.clicked.connect(self._clear_view)
        clear_device = button(
            "Cihazı Sıfırla", theme.Glyph.RESTART, "danger", "Telefondaki log tamponunu boşaltır (logcat -c)",
            icon_color=theme.DANGER,
        )
        clear_device.clicked.connect(self._clear_device)
        save = button("Kaydet", theme.Glyph.DOWNLOAD, tooltip="Görünen satırları .txt dosyasına kaydet")
        save.clicked.connect(self._save)
        self._auto_scroll = QCheckBox("Otomatik kaydır")
        self._auto_scroll.setChecked(True)
        self._counter = label("", "muted")
        top = QHBoxLayout()
        top.setSpacing(10)
        for widget in (self._toggle, clear_view, clear_device, save):
            top.addWidget(widget)
        top.addStretch()
        top.addWidget(self._auto_scroll)
        top.addWidget(self._counter)
        card.body.addLayout(top)
        card.body.addLayout(self._build_filter_row())
        self.content.addWidget(card)

    def _build_filter_row(self) -> QVBoxLayout:
        self._buffer_box = QComboBox()
        for name in BUFFERS:
            self._buffer_box.addItem(BUFFER_LABELS[name], name)
        self._buffer_box.setCurrentIndex(BUFFERS.index(DEFAULT_BUFFER))
        self._buffer_box.setToolTip("Okunacak log tamponu")
        self._buffer_box.currentIndexChanged.connect(self._on_buffer_changed)
        self._level_box = QComboBox()
        for level in LEVELS:
            self._level_box.addItem(LEVEL_LABELS[level], level)
        self._level_box.setToolTip("En düşük seviye")
        self._level_box.currentIndexChanged.connect(self._apply_filter)
        self._tag_edit = self._make_edit("Etiket (tag)", "Etikette geçen metin")
        self._text_edit = self._make_edit("Mesajda ara", "Mesajda geçen metin ya da düzenli ifade")
        self._regex_box = QCheckBox("Regex")
        self._regex_box.toggled.connect(self._apply_filter)
        self._package_edit = self._make_edit("Paket (com.örnek.uygulama)", "Yalnızca bu uygulamanın süreçleri; Enter ile uygula")
        self._package_edit.textChanged.disconnect()
        self._package_edit.editingFinished.connect(self._resolve_package)
        rows = QVBoxLayout()
        rows.setSpacing(10)
        first, second = QHBoxLayout(), QHBoxLayout()
        for row in (first, second):
            row.setSpacing(10)
            rows.addLayout(row)
        for widget in (self._buffer_box, self._level_box):
            first.addWidget(widget)
        first.addWidget(self._tag_edit, 1)
        first.addWidget(self._package_edit, 1)
        second.addWidget(self._text_edit, 1)
        second.addWidget(self._regex_box)
        return rows

    def _make_edit(self, placeholder: str, tooltip: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setToolTip(tooltip)
        edit.setClearButtonEnabled(True)
        edit.textChanged.connect(lambda _text: self._filter_timer.start())
        return edit

    # --- lifecycle ----------------------------------------------------------
    def on_shown(self) -> None:
        self._start_if_wanted()

    def refresh(self) -> None:
        self._start_if_wanted()

    def set_service(self, service) -> None:
        self._stop_reader()
        self._clear_view()
        self._pids = None
        self._package_edit.clear()
        self._update_toggle(False)
        super().set_service(service)

    def shutdown(self) -> None:
        """Stop adb and the timers. Call when the main window closes."""
        self._wants_running = False
        self._stop_reader()
        self._flush_timer.stop()
        self._filter_timer.stop()

    def _start_if_wanted(self) -> None:
        if self._wants_running and self._reader is None and self.service is not None:
            self._start_reader()

    def _start_reader(self) -> None:
        self._generation += 1
        generation = self._generation
        reader = LogcatReader(
            self.context.adb,
            self.service.serial,
            lambda lines: self._bridge.batch.emit(generation, lines),
            lambda reason: self._bridge.stopped.emit(generation, reason),
            buffer=self._buffer_box.currentData(),
        )
        try:
            reader.start()
        except AdbError as exc:
            self._wants_running = False
            self._update_toggle(False)
            self.context.notify(f"Logcat başlatılamadı: {exc}", "error")
            return
        self._reader = reader
        self._wants_running = True
        self._flush_timer.start()
        self._update_toggle(True)

    def _stop_reader(self) -> None:
        reader, self._reader = self._reader, None
        self._generation += 1  # late batches from this reader are ignored
        if reader is not None:
            self._remember_replay_cutoff()
            reader.stop()

    def _remember_replay_cutoff(self) -> None:
        """Note the newest line so a resume (which re-reads the tail) skips repeats."""
        if not self._buffer:
            self._replay_cutoff = None
            return
        newest = self._buffer[-1].timestamp
        same_time = frozenset(line for line in reversed(self._buffer) if line.timestamp == newest)
        self._replay_cutoff = (newest, same_time)
        self._replay_left = TAIL_LINES

    # --- reader events (UI thread) -------------------------------------------
    def _on_batch(self, generation: int, lines: list[LogLine]) -> None:
        if generation != self._generation:
            return
        fresh = self._drop_replayed(lines)
        self._buffer.extend(fresh)
        self._pending.extend(line for line in fresh if self._matches(line))

    def _drop_replayed(self, lines: list[LogLine]) -> list[LogLine]:
        if self._replay_cutoff is None:
            return lines
        newest, seen = self._replay_cutoff
        kept: list[LogLine] = []
        for line in lines:
            is_old = line.timestamp < newest or (line.timestamp == newest and line in seen)
            if self._replay_cutoff is not None and is_old and self._replay_left > 0:
                self._replay_left -= 1
                continue
            self._replay_cutoff = None
            kept.append(line)
        return kept

    def _on_reader_stopped(self, generation: int, reason: str) -> None:
        if generation != self._generation:
            return
        self._reader = None
        self._wants_running = False
        self._update_toggle(False)
        self.context.notify(f"Logcat akışı durdu: {reason}", "error")

    def _flush(self) -> None:
        count = min(len(self._pending), MAX_LINES_PER_FLUSH)
        if count == 0:
            return
        lines = [self._pending.popleft() for _ in range(count)]
        self._shown = min(self._shown + count, MAX_DISPLAY_LINES)
        self._output.append_lines(lines, self._auto_scroll.isChecked())
        self._update_counter()

    # --- filtering ----------------------------------------------------------
    def _current_filter(self) -> LogFilter:
        return LogFilter(
            min_level=self._level_box.currentData(),
            tag=self._tag_edit.text(),
            text=self._text_edit.text(),
            is_regex=self._regex_box.isChecked(),
            pids=self._pids,
        )

    def _apply_filter(self, *_args) -> None:
        """Rebuild the compiled filter and re-render what is already buffered."""
        self._filter_timer.stop()
        try:
            matcher = self._current_filter().matcher()
        except ValueError as exc:
            self._text_edit.setStyleSheet(INVALID_FIELD_STYLE)
            self.context.notify(str(exc), "error")
            return
        self._text_edit.setStyleSheet("")
        self._matches = matcher
        self._pending.clear()
        self._output.reset()
        matched = [line for line in self._buffer if matcher(line)][-MAX_DISPLAY_LINES:]
        self._shown = len(matched)
        self._output.append_lines(matched, self._auto_scroll.isChecked())
        self._update_counter()

    def _resolve_package(self) -> None:
        package = self._package_edit.text().strip()
        if not package:
            self._set_pids(None)
        elif self.service is not None:
            self.mark_changed()
            adb, serial = self.context.adb, self.service.serial
            self.read(lambda: resolve_package_pids(adb, serial, package), self._on_pids_resolved)

    def _on_pids_resolved(self, pids: frozenset[int]) -> None:
        if not pids:
            self.context.notify("Bu uygulama şu an çalışmıyor; süreç bulunamadı.", "error")
        self._set_pids(pids)

    def _set_pids(self, pids: frozenset[int] | None) -> None:
        self._pids = pids
        self._apply_filter()

    def _on_buffer_changed(self) -> None:
        self._clear_view()
        if self._reader is not None:
            self._stop_reader()
            self._replay_cutoff = None
            self._start_reader()

    # --- toolbar actions ----------------------------------------------------
    def _toggle_running(self) -> None:
        if self.service is None:
            return
        if self._reader is None:
            self._wants_running = True
            self._start_reader()
            return
        self._wants_running = False
        self._stop_reader()
        self._update_toggle(False)

    def _update_toggle(self, is_running: bool) -> None:
        text, glyph = ("Duraklat", theme.Glyph.PAUSE) if is_running else ("Başlat", theme.Glyph.PLAY)
        self._toggle.setText(f" {text}")
        self._toggle.setIcon(theme.make_icon(glyph, "white", 16))
        self._toggle.setIconSize(QSize(16, 16))

    def _clear_view(self) -> None:
        self._pending.clear()
        self._output.reset()
        self._buffer.clear()
        self._replay_cutoff = None
        self._shown = 0
        self._update_counter()

    def _clear_device(self) -> None:
        if self.service is None:
            return
        adb, serial = self.context.adb, self.service.serial
        self.run(lambda: clear_device_buffer(adb, serial), success="Cihaz log tamponu temizlendi")

    def _save(self) -> None:
        lines = [line for line in self._buffer if self._matches(line)][-MAX_DISPLAY_LINES:]
        if not lines:
            self.context.notify("Kaydedilecek satır yok.", "error")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Logcat'i kaydet", "logcat.txt", "Metin dosyası (*.txt)")
        if not path:
            return
        text = "\n".join(format_line(line) for line in lines) + "\n"
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("could not save logcat: %s", exc)
            self.context.notify(f"Dosya kaydedilemedi: {exc.strerror or exc}", "error")
            return
        self.context.notify(f"{len(lines)} satır kaydedildi", "success")

    def _update_counter(self) -> None:
        self._counter.setText(f"{self._shown} / {len(self._buffer)} satır")
