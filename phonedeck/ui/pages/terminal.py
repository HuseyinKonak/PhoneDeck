"""Terminal page: a persistent, streaming shell on the phone with saved commands."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLineEdit,
    QPlainTextEdit,
)

from ...core.adb import AdbError
from ...core.shell import ShellSession
from .. import theme
from ..widgets import AppContext, Page, button, label

MAX_OUTPUT_BLOCKS = 20_000
MAX_HISTORY = 200
FLUSH_INTERVAL_MS = 40
SNIPPETS_KEY = "terminal/snippets"
SNIPPET_SEPARATOR = " ⟶ "
PRESETS = (
    ("Pil durumu", "dumpsys battery"),
    ("Depolama", "df -h"),
    ("Ön plandaki uygulama", "dumpsys activity activities | grep -E 'ResumedActivity' | head -3"),
    ("En çok CPU kullananlar", "top -b -n 1 | head -25"),
    ("Kullanıcı paketleri", "pm list packages -3"),
    ("IP adresleri", "ip -4 addr"),
    ("Ekran boyutu ve yoğunluk", "wm size; wm density"),
    ("Sistem özellikleri", "getprop"),
    ("Çalışan servisler", "dumpsys activity services | grep ServiceRecord | head -40"),
    ("Açık portlar", "ss -tlnp 2>/dev/null || netstat -tln"),
)


class HistoryLineEdit(QLineEdit):
    """Line edit with Up/Down command history."""

    def __init__(self) -> None:
        super().__init__()
        self._history: tuple[str, ...] = ()
        self._position = 0

    def remember(self, command: str) -> None:
        if not self._history or self._history[-1] != command:
            self._history = (*self._history, command)[-MAX_HISTORY:]
        self._position = len(self._history)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        step = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1}.get(Qt.Key(event.key()))
        if step is None or not self._history:
            super().keyPressEvent(event)
            return
        self._position = min(max(self._position + step, 0), len(self._history))
        is_past_end = self._position == len(self._history)
        self.setText("" if is_past_end else self._history[self._position])


class _ShellBridge(QObject):
    """Carries shell output from its reader thread to the UI thread."""

    output = Signal(int, str)
    exited = Signal(int)


class TerminalPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Terminal", "Kalıcı kabuk oturumu: cd ve değişkenler komutlar arasında korunur")
        self._session: ShellSession | None = None
        self._generation = 0
        self._pending: list[str] = []
        self._bridge = _ShellBridge()
        self._bridge.output.connect(self._on_output)
        self._bridge.exited.connect(self._on_exit)
        self._status = label("Oturum kapalı", "muted")
        self._snippets = QComboBox()
        self._output = QPlainTextEdit()
        self._input = HistoryLineEdit()
        self._build_header()
        self._build_body()
        self._load_snippets()

        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self._flush)
        self._flush_timer.start(FLUSH_INTERVAL_MS)

    # --- layout -------------------------------------------------------------
    def _build_header(self) -> None:
        self.header.addWidget(self._status)
        self.header.addSpacing(12)
        for text, glyph, handler, tooltip in (
            ("Yeniden Başlat", theme.Glyph.RESTART, self._restart, "Çalışan komutu keser ve yeni oturum açar"),
            ("Kaydet", theme.Glyph.DOWNLOAD, self._save_output, "Çıktıyı dosyaya kaydet"),
            ("Temizle", theme.Glyph.DELETE, self._output.clear, "Ekranı temizle"),
        ):
            control = button(text, glyph, tooltip=tooltip)
            control.clicked.connect(lambda _checked=False, h=handler: h())
            self.header.addWidget(control)

    def _build_body(self) -> None:
        snippets = QHBoxLayout()
        snippets.setSpacing(10)
        self._snippets.setMinimumWidth(320)
        self._snippets.activated.connect(self._use_snippet)
        save = button("Komutu Kaydet", theme.Glyph.ADD, tooltip="Yazılı komutu hazır komutlara ekle")
        save.clicked.connect(self._save_snippet)
        remove = button("Sil", theme.Glyph.DELETE, tooltip="Seçili kayıtlı komutu sil")
        remove.clicked.connect(self._remove_snippet)
        snippets.addWidget(label("Hazır komutlar", "muted"))
        snippets.addWidget(self._snippets, 1)
        snippets.addWidget(save)
        snippets.addWidget(remove)
        self.content.addLayout(snippets)

        self._output.setReadOnly(True)
        self._output.setMaximumBlockCount(MAX_OUTPUT_BLOCKS)
        self._output.setMinimumHeight(360)
        self._output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._output.setPlaceholderText("Komut çıktısı burada canlı akar. Uzun süren komutu kesmek için Yeniden Başlat.")
        self.content.addWidget(self._output, 1)

        self._input.setObjectName("terminalInput")
        self._input.setPlaceholderText("Komut yaz ve Enter'a bas  ·  ↑ ↓ geçmiş")
        self._input.returnPressed.connect(self._execute)
        run = button("Çalıştır", theme.Glyph.PLAY, "primary", icon_color="white")
        run.clicked.connect(self._execute)
        line = QHBoxLayout()
        line.setSpacing(10)
        line.addWidget(self._input, 1)
        line.addWidget(run)
        self.content.addLayout(line)

    # --- session ------------------------------------------------------------
    def set_service(self, service) -> None:
        if service is not self.service:
            self._stop_session()
        super().set_service(service)

    def on_shown(self) -> None:
        if self._session is None:
            self._start_session()
        self._input.setFocus()

    def shutdown(self) -> None:
        self._stop_session()

    def _start_session(self) -> None:
        if self.service is None:
            return
        self._generation += 1
        generation = self._generation
        session = ShellSession(
            self.context.adb,
            self.service.serial,
            on_output=lambda text: self._bridge.output.emit(generation, text),
            on_exit=lambda: self._bridge.exited.emit(generation),
        )
        try:
            session.start()
        except AdbError as exc:
            self.context.notify(f"Kabuk açılamadı: {exc}", "error")
            return
        self._session = session
        self._status.setText("Oturum açık")

    def _stop_session(self) -> None:
        self._generation += 1  # Output from the old session is ignored from here on.
        if self._session is not None:
            self._session.stop()
            self._session = None
        self._status.setText("Oturum kapalı")

    def _restart(self) -> None:
        self._stop_session()
        self._pending.append("\n— oturum yeniden başlatıldı —\n")
        self._start_session()

    def _on_output(self, generation: int, text: str) -> None:
        if generation == self._generation:
            self._pending.append(text)

    def _on_exit(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._session = None
        self._status.setText("Oturum kapandı")
        self._pending.append("\n— oturum kapandı; yeni komutla yeniden açılır —\n")

    def _flush(self) -> None:
        """Append buffered output in one go so heavy output cannot freeze the UI."""
        if not self._pending:
            return
        text, self._pending = "".join(self._pending), []
        scrollbar = self._output.verticalScrollBar()
        is_at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        cursor = self._output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        if is_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    # --- commands -----------------------------------------------------------
    def _execute(self) -> None:
        command = self._input.text().strip()
        if not command or self.service is None:
            return
        if self._session is None or not self._session.is_running:
            self._start_session()
        if self._session is None:
            return
        self._input.remember(command)
        self._input.clear()
        self._pending.append(f"\n$ {command}\n")
        if not self._session.send(command):
            self.context.notify("Komut gönderilemedi; oturum kapalı.", "error")

    def _save_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Çıktıyı kaydet", "terminal.txt", "Metin (*.txt)")
        if not path:
            return
        try:
            Path(path).write_text(self._output.toPlainText(), encoding="utf-8")
        except OSError as exc:
            self.context.notify(f"Kaydedilemedi: {exc}", "error")
            return
        self.context.notify(f"Kaydedildi: {path}", "success")

    # --- snippets -----------------------------------------------------------
    def _saved_snippets(self) -> list[str]:
        value = QSettings().value(SNIPPETS_KEY, [])
        return [value] if isinstance(value, str) else list(value or [])

    def _load_snippets(self) -> None:
        self._snippets.clear()
        self._snippets.addItem("Hazır komut seç…", "")
        for name, command in PRESETS:
            self._snippets.addItem(f"{name}{SNIPPET_SEPARATOR}{command}", command)
        saved = self._saved_snippets()
        if saved:
            self._snippets.insertSeparator(self._snippets.count())
        for entry in saved:
            name, _, command = entry.partition(SNIPPET_SEPARATOR)
            self._snippets.addItem(f"★ {entry}", command or name)

    def _use_snippet(self, index: int) -> None:
        command = self._snippets.itemData(index)
        if command:
            self._input.setText(command)
            self._input.setFocus()
        self._snippets.setCurrentIndex(0)

    def _save_snippet(self) -> None:
        command = self._input.text().strip()
        if not command:
            self.context.notify("Önce kaydedilecek komutu yaz.", "error")
            return
        name, accepted = QInputDialog.getText(self, "Komutu kaydet", "Bu komuta bir ad ver:")
        if not accepted or not name.strip():
            return
        entries = [*self._saved_snippets(), f"{name.strip()}{SNIPPET_SEPARATOR}{command}"]
        QSettings().setValue(SNIPPETS_KEY, entries)
        self._load_snippets()
        self.context.notify("Komut kaydedildi", "success")

    def _remove_snippet(self) -> None:
        saved = self._saved_snippets()
        if not saved:
            self.context.notify("Kayıtlı komut yok.", "error")
            return
        entry, accepted = QInputDialog.getItem(self, "Kayıtlı komutu sil", "Silinecek komut:", saved, 0, False)
        if not accepted:
            return
        QSettings().setValue(SNIPPETS_KEY, [item for item in saved if item != entry])
        self._load_snippets()
