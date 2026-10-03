"""Files page: a full file manager for the phone's storage."""

from __future__ import annotations

import posixpath
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ...core import fileops
from ...core.device import normalize_path
from ...core.models import FileEntry
from ...core.parsers import format_size
from .. import theme
from ..widgets import AppContext, Page, button, label

HOME_PATH = "/sdcard"
SHORTCUTS = (
    ("Dahili", "/sdcard"),
    ("İndirilenler", "/sdcard/Download"),
    ("Kamera", "/sdcard/DCIM"),
    ("Resimler", "/sdcard/Pictures"),
    ("Videolar", "/sdcard/Movies"),
    ("Müzik", "/sdcard/Music"),
    ("Belgeler", "/sdcard/Documents"),
    ("Kök", "/"),
)
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"})
PREVIEW_SIZE = (900, 700)
MAX_LISTED_IN_DIALOG = 8
NAME_COLUMN, SIZE_COLUMN, DATE_COLUMN = range(3)


@dataclass(frozen=True)
class Clipboard:
    """Paths picked with Copy or Cut, waiting for Paste."""

    paths: tuple[str, ...] = ()
    is_cut: bool = False


class EntryItem(QTreeWidgetItem):
    """Sorts folders before files, sizes numerically and names case-insensitively."""

    def __init__(self, entry: FileEntry) -> None:
        size = "" if entry.is_dir else format_size(entry.size)
        super().__init__([entry.name, size, entry.modified])
        self.entry = entry
        self.setTextAlignment(SIZE_COLUMN, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other: "EntryItem") -> bool:
        column = self.treeWidget().sortColumn()
        ascending = self.treeWidget().header().sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
        if self.entry.is_dir != other.entry.is_dir:
            # Folders stay on top whichever way the column is sorted.
            return self.entry.is_dir == ascending
        if column == SIZE_COLUMN:
            return self.entry.size < other.entry.size
        if column == DATE_COLUMN:
            return self.entry.modified < other.entry.modified
        return self.entry.name.casefold() < other.entry.name.casefold()


class FileTree(QTreeWidget):
    """File list that accepts files dragged in from the computer."""

    files_dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self.setHeaderLabels(["Ad", "Boyut", "Değiştirilme"])
        self.setRootIsDecorated(False)
        self.setMinimumHeight(360)
        self.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.setSortingEnabled(True)
        self.sortByColumn(NAME_COLUMN, Qt.SortOrder.AscendingOrder)
        self.header().setSectionResizeMode(NAME_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.header().setStretchLastSection(False)
        self.setColumnWidth(SIZE_COLUMN, 110)
        self.setColumnWidth(DATE_COLUMN, 150)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


class PreviewDialog(QDialog):
    """Shows an image or the beginning of a text file."""

    def __init__(self, parent, title: str, pixmap: QPixmap | None = None, text: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*PREVIEW_SIZE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        if pixmap is not None:
            image = QLabel()
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            image.setPixmap(pixmap.scaled(
                PREVIEW_SIZE[0] - 40, PREVIEW_SIZE[1] - 40,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
            ))
            layout.addWidget(image)
        else:
            viewer = QPlainTextEdit(text)
            viewer.setReadOnly(True)
            layout.addWidget(viewer)


class FilesPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Dosyalar", "Gezin, önizle, kopyala, taşı, yeniden adlandır; bilgisayarla dosya alışverişi")
        self._path = HOME_PATH
        self._entries: tuple[FileEntry, ...] = ()
        self._clipboard = Clipboard()
        self._folder_icon = theme.make_icon(theme.Glyph.FOLDER, theme.WARNING)
        self._file_icon = theme.make_icon(theme.Glyph.FILE, theme.MUTED)
        self._summary = label("", "muted")
        self._path_field = QLineEdit(self._path)
        self._filter = QLineEdit()
        self._hidden = QCheckBox("Gizli dosyalar")
        self._paste = button("Yapıştır", theme.Glyph.PASTE)
        self._tree = FileTree()
        self._build_navigation()
        self._build_tree()
        self._build_actions()

    # --- layout -------------------------------------------------------------
    def _build_navigation(self) -> None:
        shortcuts = QHBoxLayout()
        shortcuts.setSpacing(6)
        for text, path in SHORTCUTS:
            chip = button(text, variant="ghost")
            chip.clicked.connect(lambda _checked=False, p=path: self._open(p))
            shortcuts.addWidget(chip)
        shortcuts.addStretch()
        self.content.addLayout(shortcuts)

        navigation = QHBoxLayout()
        navigation.setSpacing(8)
        for glyph, tooltip, handler in (
            (theme.Glyph.UP, "Üst klasör", self._go_up),
            (theme.Glyph.REFRESH, "Yenile", self.refresh),
        ):
            control = button(glyph=glyph, tooltip=tooltip)
            control.clicked.connect(lambda _checked=False, h=handler: h())
            navigation.addWidget(control)
        self._path_field.returnPressed.connect(lambda: self._open(self._path_field.text()))
        self._filter.setPlaceholderText("Bu klasörde filtrele…")
        self._filter.setClearButtonEnabled(True)
        self._filter.setFixedWidth(170)
        self._filter.textChanged.connect(lambda _text: self._render())
        self._hidden.toggled.connect(lambda _checked: self._render())
        navigation.addWidget(self._path_field, 1)
        navigation.addWidget(self._filter)
        navigation.addWidget(self._hidden)
        self.content.addLayout(navigation)

    def _build_tree(self) -> None:
        self._tree.itemDoubleClicked.connect(lambda item, _column: self._activate(item))
        self._tree.customContextMenuRequested.connect(self._show_menu)
        self._tree.files_dropped.connect(self._upload_paths)
        self._tree.itemSelectionChanged.connect(self._update_summary)
        self.content.addWidget(self._tree, 1)
        self.content.addWidget(self._summary)

    def _build_actions(self) -> None:
        # Two short rows keep the page usable in a narrow window.
        transfer, edit = QHBoxLayout(), QHBoxLayout()
        for row, entries in (
            (transfer, (
                ("İndir", theme.Glyph.DOWNLOAD, "primary", self._download),
                ("Gönder", theme.Glyph.UPLOAD, "", self._upload),
                ("Yeni Klasör", theme.Glyph.NEW_FOLDER, "", self._make_dir),
                ("Yeniden Adlandır", theme.Glyph.RENAME, "", self._rename),
            )),
            (edit, (
                ("Kopyala", theme.Glyph.COPY, "", lambda: self._pick(is_cut=False)),
                ("Kes", theme.Glyph.CUT, "", lambda: self._pick(is_cut=True)),
            )),
        ):
            row.setSpacing(8)
            for text, glyph, variant, handler in entries:
                color = "white" if variant == "primary" else theme.TEXT
                action = button(text, glyph, variant, icon_color=color)
                action.clicked.connect(lambda _checked=False, h=handler: h())
                row.addWidget(action)
        self._paste.clicked.connect(self._paste_here)
        self._paste.setEnabled(False)
        delete = button("Sil", theme.Glyph.DELETE, "danger", icon_color=theme.DANGER)
        delete.clicked.connect(self._delete)
        edit.addWidget(self._paste)
        edit.addWidget(delete)
        for row in (transfer, edit):
            row.addStretch()
            self.content.addLayout(row)

    # --- navigation ---------------------------------------------------------
    def on_shown(self) -> None:
        # Keep the current listing when the page is reopened.
        if not self._entries:
            self.refresh()

    def refresh(self) -> None:
        self._open(self._path)

    def _open(self, path: str) -> None:
        if self.service is None:
            return
        target = normalize_path(path)
        self.run(lambda: self.service.list_dir(target), lambda entries: self._show(target, entries))

    def _show(self, path: str, entries: tuple[FileEntry, ...]) -> None:
        if path != self._path:
            self._filter.clear()
        self._path = path
        self._entries = entries
        self._path_field.setText(path)
        self._render()

    def _render(self) -> None:
        query = self._filter.text().strip().casefold()
        show_hidden = self._hidden.isChecked()
        self._tree.setSortingEnabled(False)
        self._tree.clear()
        for entry in self._entries:
            if entry.name.startswith(".") and not show_hidden:
                continue
            if query and query not in entry.name.casefold():
                continue
            item = EntryItem(entry)
            item.setIcon(NAME_COLUMN, self._folder_icon if entry.is_dir else self._file_icon)
            self._tree.addTopLevelItem(item)
        self._tree.setSortingEnabled(True)
        self._update_summary()

    def _update_summary(self) -> None:
        selected = self._selected_entries()
        if selected:
            total = sum(entry.size for entry in selected if not entry.is_dir)
            self._summary.setText(f"{len(selected)} öğe seçili  ·  {format_size(total)}")
            return
        shown = self._tree.topLevelItemCount()
        self._summary.setText(f"{shown} öğe  ·  toplam {len(self._entries)}")

    def _go_up(self) -> None:
        self._open(posixpath.dirname(self._path) or "/")

    def _activate(self, item: EntryItem) -> None:
        if item.entry.is_dir:
            self._open(posixpath.join(self._path, item.entry.name))
        else:
            self._preview(item.entry)

    def _selected_entries(self) -> list[FileEntry]:
        return [item.entry for item in self._tree.selectedItems()]

    def _selected_paths(self) -> list[str]:
        paths = [posixpath.join(self._path, entry.name) for entry in self._selected_entries()]
        if not paths:
            self.context.notify("Önce dosya veya klasör seç.", "error")
        return paths

    # --- context menu -------------------------------------------------------
    def _show_menu(self, position) -> None:
        selected = self._selected_entries()
        menu = QMenu(self)
        if len(selected) == 1:
            open_text = "Aç" if selected[0].is_dir else "Önizle"
            menu.addAction(open_text, lambda: self._activate(self._tree.selectedItems()[0]))
        if selected:
            menu.addAction("Bilgisayara indir", self._download)
            menu.addSeparator()
            menu.addAction("Kopyala", lambda: self._pick(is_cut=False))
            menu.addAction("Kes", lambda: self._pick(is_cut=True))
        if self._clipboard.paths:
            menu.addAction(f"Buraya yapıştır ({len(self._clipboard.paths)})", self._paste_here)
        if len(selected) == 1:
            menu.addAction("Yeniden adlandır", self._rename)
            menu.addAction("Yolu kopyala", self._copy_path)
            menu.addAction("Boyutu hesapla", self._show_usage)
        if selected:
            menu.addSeparator()
            menu.addAction("Sil", self._delete)
        menu.addSeparator()
        menu.addAction("Yeni klasör", self._make_dir)
        menu.addAction("Telefona dosya gönder", self._upload)
        menu.exec(self._tree.viewport().mapToGlobal(position))

    # --- transfer -----------------------------------------------------------
    def _download(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        folder = QFileDialog.getExistingDirectory(self, "İndirilecek klasörü seç")
        if not folder:
            return
        service = self.service
        self.context.notify(f"{len(paths)} öğe indiriliyor…", "info")

        def work() -> None:
            for path in paths:
                service.pull(path, folder)

        self.run(work, success=f"İndirildi: {folder}")

    def _upload(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Gönderilecek dosyaları seç")
        if files:
            self._upload_paths(files)

    def _upload_paths(self, files: list[str]) -> None:
        """Send files or folders from the computer into the current phone folder."""
        service, destination = self.service, self._path
        if service is None:
            return
        self.context.notify(f"{len(files)} öğe gönderiliyor…", "info")

        def work() -> None:
            for file in files:
                service.push(file, destination)

        self.run(work, lambda _result: self.refresh(), success=f"Gönderildi: {destination}")

    def _preview(self, entry: FileEntry) -> None:
        remote = posixpath.join(self._path, entry.name)
        adb, service = self.context.adb, self.service
        suffix = posixpath.splitext(entry.name)[1].lower()
        if suffix in IMAGE_SUFFIXES:

            def fetch() -> QImage:
                # A fixed local name: the phone's file name must never choose where the
                # file lands on this computer. The copy is removed once it is loaded.
                with tempfile.TemporaryDirectory(prefix="phonedeck-") as folder:
                    local = Path(folder) / f"preview{suffix}"
                    service.pull(remote, str(local))
                    return QImage(str(local))

            self.run(
                fetch,
                lambda image: PreviewDialog(self, entry.name, pixmap=QPixmap.fromImage(image)).exec(),
            )
        else:
            self.run(
                lambda: fileops.read_text(adb, service.serial, remote),
                lambda text: PreviewDialog(self, entry.name, text=text).exec(),
            )

    # --- edit ---------------------------------------------------------------
    def _make_dir(self) -> None:
        name, accepted = QInputDialog.getText(self, "Yeni klasör", "Klasör adı:")
        if not accepted or not name.strip():
            return
        try:
            target = posixpath.join(self._path, fileops.validate_name(name))
        except ValueError as exc:
            self.context.notify(str(exc), "error")
            return
        self.run(lambda: self.service.make_dir(target), lambda _result: self.refresh())

    def _rename(self) -> None:
        paths = self._selected_paths()
        if len(paths) != 1:
            if paths:
                self.context.notify("Yeniden adlandırmak için tek öğe seç.", "error")
            return
        current = posixpath.basename(paths[0])
        name, accepted = QInputDialog.getText(self, "Yeniden adlandır", "Yeni ad:", text=current)
        if not accepted or not name.strip() or name.strip() == current:
            return
        adb, serial = self.context.adb, self.service.serial
        self.run(
            lambda: fileops.rename(adb, serial, paths[0], name),
            lambda _result: self.refresh(),
            success="Yeniden adlandırıldı",
        )

    def _pick(self, is_cut: bool) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        self._clipboard = Clipboard(tuple(paths), is_cut)
        self._paste.setEnabled(True)
        self._paste.setText(f" Yapıştır ({len(paths)})")
        verb = "kesildi" if is_cut else "kopyalandı"
        self.context.notify(f"{len(paths)} öğe {verb}; hedef klasöre gidip Yapıştır'a bas", "info")

    def _paste_here(self) -> None:
        clipboard, destination = self._clipboard, self._path
        if not clipboard.paths:
            return
        adb, serial = self.context.adb, self.service.serial
        operation = fileops.move_into if clipboard.is_cut else fileops.copy_into

        def done(_result) -> None:
            if clipboard.is_cut:
                self._clipboard = Clipboard()
                self._paste.setEnabled(False)
                self._paste.setText(" Yapıştır")
            self.refresh()

        self.context.notify(f"{len(clipboard.paths)} öğe yapıştırılıyor…", "info")
        self.run(
            lambda: operation(adb, serial, list(clipboard.paths), destination),
            done,
            success="Taşındı" if clipboard.is_cut else "Kopyalandı",
        )

    def _copy_path(self) -> None:
        paths = self._selected_paths()
        if paths:
            QGuiApplication.clipboard().setText(paths[0])
            self.context.notify("Yol panoya kopyalandı", "success")

    def _show_usage(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        adb, serial = self.context.adb, self.service.serial
        self.run(
            lambda: fileops.disk_usage(adb, serial, paths[0]),
            lambda size: self.context.notify(f"{posixpath.basename(paths[0])}: {size}", "info"),
        )

    def _delete(self) -> None:
        paths = self._selected_paths()
        if not paths:
            return
        listing = "\n".join(paths[:MAX_LISTED_IN_DIALOG])
        if len(paths) > MAX_LISTED_IN_DIALOG:
            listing += "\n…"
        answer = QMessageBox.warning(
            self,
            "Kalıcı olarak sil",
            f"{len(paths)} öğe telefondan kalıcı olarak silinecek:\n\n{listing}\n\nBu işlem geri alınamaz.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        service = self.service

        def work() -> None:
            for path in paths:
                service.delete(path)

        self.run(work, lambda _result: self.refresh(), success="Silindi")
