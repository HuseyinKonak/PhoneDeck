"""Live phone screen: rendering, touch/keyboard forwarding, recording and device keys."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import av
import numpy as np
from PySide6.QtCore import (
    QObject,
    QPoint,
    QRectF,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.adb import Adb
from ..core.device import DeviceService
from ..core.scrcpy import protocol
from ..core.scrcpy.client import ScrcpyClient, ScrcpyOptions
from ..core.scrcpy.protocol import Action, ControlType, Key
from . import theme
from .async_runner import run_async
from .widgets import label, round_button

PANEL_MIN_WIDTH = 340
PANEL_DEFAULT_WIDTH = 400
BEZEL = 10
CORNER_RADIUS = 26
WHEEL_STEP = 120
FPS_INTERVAL_MS = 1000
RECONNECT_DELAY_MS = 3000
SHUTDOWN_WAIT_SECONDS = 3
PRESCALE_TOLERANCE_PX = 2
MEDIA_FOLDER = "PhoneDeck"
DROP_TARGET_DIR = "/sdcard/Download"
APK_SUFFIX = ".apk"
SECONDS_PER_MINUTE = 60

# Choices offered in the stream settings dialog: (label, value)
SIZE_CHOICES = (("Orijinal", 0), ("1920", 1920), ("1600", 1600), ("1280", 1280), ("1024", 1024), ("800", 800))
BIT_RATE_CHOICES = (("2 Mbps", 2_000_000), ("4 Mbps", 4_000_000), ("8 Mbps", 8_000_000), ("16 Mbps", 16_000_000), ("32 Mbps", 32_000_000))
FPS_CHOICES = (("15", 15), ("30", 30), ("60", 60), ("90", 90), ("120", 120))

_QT_KEYS = {
    Qt.Key.Key_Return: Key.ENTER,
    Qt.Key.Key_Enter: Key.ENTER,
    Qt.Key.Key_Backspace: Key.DEL,
    Qt.Key.Key_Delete: Key.FORWARD_DEL,
    Qt.Key.Key_Tab: Key.TAB,
    Qt.Key.Key_Escape: Key.BACK,
    Qt.Key.Key_Up: Key.DPAD_UP,
    Qt.Key.Key_Down: Key.DPAD_DOWN,
    Qt.Key.Key_Left: Key.DPAD_LEFT,
    Qt.Key.Key_Right: Key.DPAD_RIGHT,
    Qt.Key.Key_Home: Key.MOVE_HOME,
    Qt.Key.Key_End: Key.MOVE_END,
    Qt.Key.Key_PageUp: Key.PAGE_UP,
    Qt.Key.Key_PageDown: Key.PAGE_DOWN,
}


def load_options() -> ScrcpyOptions:
    """Stream settings saved from the last run."""
    settings = QSettings()
    defaults = ScrcpyOptions()
    return ScrcpyOptions(
        max_size=int(settings.value("stream/max_size", defaults.max_size)),
        bit_rate=int(settings.value("stream/bit_rate", defaults.bit_rate)),
        max_fps=int(settings.value("stream/max_fps", defaults.max_fps)),
        stay_awake=settings.value("stream/stay_awake", defaults.stay_awake, type=bool),
        show_touches=settings.value("stream/show_touches", defaults.show_touches, type=bool),
        power_off_on_close=settings.value(
            "stream/power_off_on_close", defaults.power_off_on_close, type=bool
        ),
    )


def save_options(options: ScrcpyOptions) -> None:
    settings = QSettings()
    for name in ("max_size", "bit_rate", "max_fps", "stay_awake", "show_touches", "power_off_on_close"):
        settings.setValue(f"stream/{name}", getattr(options, name))


def media_path(location: QStandardPaths.StandardLocation, prefix: str, suffix: str) -> Path:
    base = QStandardPaths.writableLocation(location)
    return Path(base) / MEDIA_FOLDER / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"


class StreamSettingsDialog(QDialog):
    """Quality and behaviour of the mirroring session."""

    def __init__(self, parent: QWidget, options: ScrcpyOptions) -> None:
        super().__init__(parent)
        self.setWindowTitle("Yayın ayarları")
        self._options = options
        self._size = self._combo(SIZE_CHOICES, options.max_size)
        self._bit_rate = self._combo(BIT_RATE_CHOICES, options.bit_rate)
        self._fps = self._combo(FPS_CHOICES, options.max_fps)
        self._stay_awake = QCheckBox("Bağlıyken ekranı açık tut")
        self._stay_awake.setChecked(options.stay_awake)
        self._show_touches = QCheckBox("Dokunmaları telefonda göster")
        self._show_touches.setChecked(options.show_touches)
        self._power_off = QCheckBox("Kapatırken telefon ekranını kapat")
        self._power_off.setChecked(options.power_off_on_close)

        form = QFormLayout()
        form.setSpacing(12)
        form.addRow("En büyük kenar (piksel)", self._size)
        form.addRow("Bit hızı", self._bit_rate)
        form.addRow("Kare hızı (FPS)", self._fps)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        layout.addLayout(form)
        for checkbox in (self._stay_awake, self._show_touches, self._power_off):
            layout.addWidget(checkbox)
        layout.addWidget(label("Değişiklikler yayını yeniden başlatır.", "muted"))
        layout.addWidget(buttons)

    @staticmethod
    def _combo(choices: tuple[tuple[str, int], ...], current: int) -> QComboBox:
        combo = QComboBox()
        for text, value in choices:
            combo.addItem(text, value)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    def options(self) -> ScrcpyOptions:
        return replace(
            self._options,
            max_size=self._size.currentData(),
            bit_rate=self._bit_rate.currentData(),
            max_fps=self._fps.currentData(),
            stay_awake=self._stay_awake.isChecked(),
            show_touches=self._show_touches.isChecked(),
            power_off_on_close=self._power_off.isChecked(),
        )


class FrameBridge(QObject):
    """Moves decoded frames from the decoder thread to the UI thread.

    Only the newest frame is kept, so a slow UI never builds up a backlog.
    """

    frame_ready = Signal()
    state_changed = Signal(int, str, str)
    clipboard_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._latest: tuple[QImage, QSize] | None = None
        self._is_pending = False
        self._target = QSize()

    def set_target(self, size: QSize) -> None:
        """Pixel box the view can show; frames are scaled to fit it off the UI thread."""
        with self._lock:
            self._target = QSize(size)

    def push(self, frame: av.VideoFrame) -> None:
        """Called on the decoder thread: scale there so painting is a plain copy."""
        source = QSize(frame.width, frame.height)
        with self._lock:
            target = QSize(self._target)
        scaled = source.scaled(target, Qt.AspectRatioMode.KeepAspectRatio) if target.isValid() else source
        if scaled.width() >= source.width() or scaled.isEmpty():
            scaled = source  # Never upscale here; the painter handles that rare case.
        # Scaled frames can come back as a padded (non-contiguous) view.
        array = np.ascontiguousarray(
            frame.to_ndarray(width=scaled.width(), height=scaled.height(), format="rgb24")
        )
        height, width, _ = array.shape
        image = QImage(array.data, width, height, 3 * width, QImage.Format.Format_RGB888).copy()
        with self._lock:
            self._latest = (image, source)
            should_notify = not self._is_pending
            self._is_pending = True
        if should_notify:
            self.frame_ready.emit()

    def take(self) -> tuple[QImage, QSize] | None:
        with self._lock:
            self._is_pending = False
            return self._latest


class MirrorView(QWidget):
    """Paints the phone screen and turns mouse/keyboard input into control messages."""

    target_changed = Signal(QSize)
    files_dropped = Signal(list)

    def __init__(self) -> None:
        super().__init__()
        self._image: QImage | None = None
        self._source = QSize()
        self._message = "Cihaz bekleniyor"
        self._is_pressed = False
        self._is_pinching = False
        self.send_control = lambda message: False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(320)
        self.setAcceptDrops(True)

    def set_frame(self, image: QImage, source: QSize) -> None:
        """Show a frame; `source` is the phone-side size used for touch mapping."""
        self._image = image
        self._source = source
        self.update()

    def set_message(self, message: str) -> None:
        self._message = message
        self._image = None
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        area = self.size() - QSize(2 * BEZEL, 2 * BEZEL)
        self.target_changed.emit(area * self.devicePixelRatioF())

    def _screen_rect(self) -> QRectF:
        """Largest rect with the frame's aspect ratio that fits inside the bezel."""
        area = QRectF(self.rect()).adjusted(BEZEL, BEZEL, -BEZEL, -BEZEL)
        if self._image is None:
            return area
        scale = min(area.width() / self._source.width(), area.height() / self._source.height())
        width, height = self._source.width() * scale, self._source.height() * scale
        return QRectF(
            area.x() + (area.width() - width) / 2,
            area.y() + (area.height() - height) / 2,
            width,
            height,
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        screen = self._screen_rect()
        body = screen.adjusted(-BEZEL, -BEZEL, BEZEL, BEZEL)
        painter.setPen(QColor(theme.BORDER))
        painter.setBrush(QColor("#05060A"))
        painter.drawRoundedRect(body, CORNER_RADIUS + BEZEL, CORNER_RADIUS + BEZEL)
        if self._image is None:
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(screen, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message)
            return
        clip = QPainterPath()
        clip.addRoundedRect(screen, CORNER_RADIUS, CORNER_RADIUS)
        painter.setClipPath(clip)
        # Frames arrive pre-scaled; only smooth when the sizes really differ (e.g. mid-resize).
        expected_width = screen.width() * self.devicePixelRatioF()
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform,
            abs(self._image.width() - expected_width) > PRESCALE_TOLERANCE_PX,
        )
        painter.drawImage(screen, self._image)

    # --- input forwarding ---------------------------------------------------
    def _device_point(self, position: QPoint) -> tuple[int, int, int, int] | None:
        if self._image is None:
            return None
        screen = self._screen_rect()
        width, height = self._source.width(), self._source.height()
        x = (position.x() - screen.x()) * width / screen.width()
        y = (position.y() - screen.y()) * height / screen.height()
        return (
            int(min(max(x, 0), width - 1)),
            int(min(max(y, 0), height - 1)),
            width,
            height,
        )

    def _send_touch(self, action: Action, position: QPoint) -> None:
        point = self._device_point(position)
        if point is None:
            return
        self.send_control(protocol.touch(action, *point))
        if self._is_pinching:
            # Second finger mirrored through the screen centre: Ctrl+drag pinches.
            x, y, width, height = point
            mirrored = (width - 1 - x, height - 1 - y, width, height)
            self.send_control(
                protocol.touch(action, *mirrored, pointer_id=protocol.POINTER_ID_VIRTUAL_FINGER)
            )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus()
        if event.button() == Qt.MouseButton.RightButton:
            self.send_control(protocol.key_press(Key.BACK))
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.send_control(protocol.key_press(Key.HOME))
        elif event.button() == Qt.MouseButton.LeftButton:
            if not self._screen_rect().contains(event.position()):
                return
            self._is_pressed = True
            self._is_pinching = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            self._send_touch(Action.DOWN, event.position().toPoint())

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._is_pressed:
            self._send_touch(Action.MOVE, event.position().toPoint())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._is_pressed:
            self._is_pressed = False
            self._send_touch(Action.UP, event.position().toPoint())
            self._is_pinching = False

    def wheelEvent(self, event) -> None:  # noqa: N802
        point = self._device_point(event.position().toPoint())
        if point is None:
            return
        delta = event.angleDelta()
        self.send_control(
            protocol.scroll(*point, delta.x() / WHEEL_STEP, delta.y() / WHEEL_STEP)
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802
        is_ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if is_ctrl and event.key() == Qt.Key.Key_V:
            self.send_control(protocol.set_clipboard(QGuiApplication.clipboard().text(), paste=True))
            return
        android_key = _QT_KEYS.get(Qt.Key(event.key()))
        if android_key is not None:
            self.send_control(protocol.key_press(android_key))
        elif event.text() and event.text().isprintable() and not is_ctrl:
            self.send_control(protocol.text(event.text()))
        else:
            super().keyPressEvent(event)

    # --- drag and drop ------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.files_dropped.emit(paths)


class PhonePanel(QFrame):
    """Right-hand panel: live screen, hardware keys, recording and session management."""

    notify = Signal(str, str)

    def __init__(self, adb: Adb) -> None:
        super().__init__()
        self.setObjectName("phonePanel")
        self.setMinimumWidth(PANEL_MIN_WIDTH)
        self._adb = adb
        self._serial: str | None = None
        self._client: ScrcpyClient | None = None
        self._options = load_options()
        self._session = 0
        self._last_error = ""
        self._frame_count = 0
        self._record_started = 0.0
        self._bridge = FrameBridge()
        self._bridge.frame_ready.connect(self._show_frame)
        self._bridge.state_changed.connect(self._on_state)
        self._bridge.clipboard_changed.connect(QGuiApplication.clipboard().setText)

        self.view = MirrorView()
        self.view.send_control = self.send_control
        self.view.target_changed.connect(self._bridge.set_target)
        self.view.files_dropped.connect(self._handle_drop)
        self._status = label("Bağlı değil", "muted")
        self._fps = label("", "muted")
        self._record = self._action_button(theme.Glyph.RECORD, "Ekran kaydını başlat", self._toggle_recording)
        self._record.setCheckable(True)
        self._build_layout()

        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._tick)
        self._fps_timer.start(FPS_INTERVAL_MS)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(PANEL_DEFAULT_WIDTH, super().sizeHint().height())

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(label("Canlı Ekran", "sectionTitle"))
        header.addStretch()
        header.addWidget(self._fps)
        layout.addLayout(header)

        middle = QHBoxLayout()
        middle.setSpacing(10)
        middle.addWidget(self.view, 1)
        side = QVBoxLayout()
        side.setSpacing(8)
        side_actions = (
            (theme.Glyph.POWER, "Güç / Kilit", lambda: self._press(Key.POWER)),
            (theme.Glyph.VOLUME_UP, "Ses +", lambda: self._press(Key.VOLUME_UP)),
            (theme.Glyph.VOLUME_DOWN, "Ses −", lambda: self._press(Key.VOLUME_DOWN)),
            (theme.Glyph.ROTATE, "Döndür", lambda: self.send_control(protocol.simple(ControlType.ROTATE_DEVICE))),
            (theme.Glyph.BELL, "Bildirimler", lambda: self.send_control(protocol.simple(ControlType.EXPAND_NOTIFICATION_PANEL))),
            (theme.Glyph.CAMERA, "Ekran görüntüsü kaydet", self.save_screenshot),
        )
        for glyph, tooltip, handler in side_actions:
            side.addWidget(self._action_button(glyph, tooltip, handler))
        side.addWidget(self._record)
        side.addStretch()
        side.addWidget(self._action_button(theme.Glyph.SLIDERS, "Yayın ayarları", self._open_settings))
        middle.addLayout(side)
        layout.addLayout(middle, 1)

        navigation = QHBoxLayout()
        navigation.addStretch()
        for glyph, tooltip, key in (
            (theme.Glyph.BACK, "Geri", Key.BACK),
            (theme.Glyph.CIRCLE, "Ana ekran", Key.HOME),
            (theme.Glyph.RECENTS, "Son uygulamalar", Key.APP_SWITCH),
        ):
            navigation.addWidget(self._action_button(glyph, tooltip, lambda k=key: self._press(k), 44))
            navigation.addSpacing(10)
        navigation.addStretch()
        layout.addLayout(navigation)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._status)
        hint = QLabel(
            "Sağ tık: Geri  ·  Orta tık: Ana ekran  ·  Ctrl+sürükle: Yakınlaştır\n"
            "Ctrl+V: Yapıştır  ·  Dosya bırak: gönder / APK kur"
        )
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

    def _action_button(self, glyph: str, tooltip: str, handler, size: int = 40) -> QPushButton:
        widget = round_button(glyph, tooltip, size)
        widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        widget.clicked.connect(lambda _checked=False: handler())
        return widget

    # --- session ------------------------------------------------------------
    def set_device(self, serial: str | None) -> None:
        if serial == self._serial:
            return
        self._serial = serial
        self._stop_client()
        if serial is None:
            self.view.set_message("Cihaz bekleniyor")
            self._status.setText("Bağlı değil")
            return
        self._start_client()

    def _start_client(self) -> None:
        if self._serial is None or self._client is not None:
            return
        self.view.set_message("Bağlanıyor…")
        self._status.setText("Ekran akışı başlatılıyor…")
        self._session += 1
        session = self._session
        self._client = ScrcpyClient(
            self._adb,
            self._serial,
            on_frame=self._bridge.push,
            on_state=lambda state, detail: self._bridge.state_changed.emit(session, state, detail),
            on_clipboard=self._bridge.clipboard_changed.emit,
            options=self._options,
        )
        self._client.start()

    def _stop_client(self) -> ScrcpyClient | None:
        """Stop the session. An active recording is saved by the client's own cleanup."""
        client = self._client
        if client is not None and client.is_recording:
            self.notify.emit("Ekran kaydı kaydediliyor…", "info")
        self._session += 1
        self._last_error = ""
        self._client = None
        self._show_recording(False)
        if client is not None:
            client.stop()
        return client

    def shutdown(self) -> None:
        """Stop mirroring and wait briefly so the adb forward is removed."""
        self._serial = None
        client = self._stop_client()
        if client is not None:
            client.wait(SHUTDOWN_WAIT_SECONDS)

    def send_control(self, message: bytes) -> bool:
        return self._client.send(message) if self._client is not None else False

    def _press(self, key: Key) -> None:
        self.send_control(protocol.key_press(key))

    def _on_state(self, session: int, state: str, detail: str) -> None:
        if state == "recording_stopped":
            self._on_recording_stopped(session, detail)
            return
        if session != self._session:
            return  # Late event from a session that was already replaced.
        if state == "connected":
            self._last_error = ""
            self._status.setText(f"{detail} · bağlı")
        elif state == "error":
            self.view.set_message(f"Bağlantı hatası\n{detail}")
            self._status.setText("Hata · yeniden denenecek")
            if detail != self._last_error:
                self.notify.emit(detail, "error")
            self._last_error = detail
        elif state == "stopped" and self._serial is not None:
            # Session ended while the device is still selected: retry.
            self._client = None
            self._show_recording(False)
            if not self._last_error:
                self._status.setText("Yeniden bağlanılıyor…")
            QTimer.singleShot(RECONNECT_DELAY_MS, self._start_client)

    def _show_frame(self) -> None:
        latest = self._bridge.take()
        if latest is not None and self._client is not None:
            self._frame_count += 1
            self.view.set_frame(*latest)

    def _tick(self) -> None:
        """Once a second: frame rate and, while recording, the elapsed time."""
        text = f"{self._frame_count} FPS" if self._client is not None else ""
        if self._record.isChecked():
            minutes, seconds = divmod(int(time.monotonic() - self._record_started), SECONDS_PER_MINUTE)
            text = f"● KAYIT {minutes:02d}:{seconds:02d}  ·  {text}"
        self._fps.setText(text)
        self._frame_count = 0

    # --- settings -----------------------------------------------------------
    def _open_settings(self) -> None:
        dialog = StreamSettingsDialog(self, self._options)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.options() == self._options:
            return
        self._options = dialog.options()
        save_options(self._options)
        if self._serial is not None:
            self._stop_client()
            self._start_client()
            self.notify.emit("Yayın ayarları uygulandı", "success")

    # --- capture ------------------------------------------------------------
    def save_screenshot(self) -> None:
        """Capture a full-resolution screenshot on the phone and save it to Pictures."""
        serial = self._serial
        if serial is None:
            self.notify.emit("Cihaz bağlı değil.", "error")
            return
        target = media_path(QStandardPaths.StandardLocation.PicturesLocation, "ekran", ".png")
        adb = self._adb

        def capture() -> Path:
            data = adb.run_bytes(["exec-out", "screencap", "-p"], serial)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return target

        run_async(
            capture,
            lambda path: self.notify.emit(f"Kaydedildi: {path}", "success"),
            lambda message: self.notify.emit(f"Ekran görüntüsü alınamadı: {message}", "error"),
        )

    def _show_recording(self, is_recording: bool) -> None:
        self._record.setChecked(is_recording)
        self._record.setToolTip("Kaydı durdur ve kaydet" if is_recording else "Ekran kaydını başlat")
        self._record.setIcon(
            theme.make_icon(
                theme.Glyph.STOP if is_recording else theme.Glyph.RECORD,
                theme.DANGER if is_recording else theme.TEXT,
            )
        )

    def _toggle_recording(self) -> None:
        client = self._client
        if client is None:
            self._show_recording(False)
            self.notify.emit("Ekran akışı bağlı değil.", "error")
            return
        if client.is_recording:
            self._show_recording(False)
            # Closing the file writes its index; keep that off the UI thread.
            run_async(client.stop_recording, self._report_recording)
            return
        target = media_path(QStandardPaths.StandardLocation.MoviesLocation, "kayit", ".mp4")
        if client.start_recording(target):
            self._record_started = time.monotonic()
            self._show_recording(True)
            self.notify.emit("Ekran kaydı başladı", "info")
        else:
            self._show_recording(False)
            self.notify.emit("Kayıt başlatılamadı.", "error")

    def _report_recording(self, path: Path | None) -> None:
        if path is None:
            self.notify.emit("Kayıt boş: ekranda değişiklik olmadı.", "error")
        else:
            self.notify.emit(f"Kayıt kaydedildi: {path}", "success")

    def _on_recording_stopped(self, session: int, detail: str) -> None:
        """The client closed the file itself (rotation, session end or write error)."""
        if session == self._session:
            self._show_recording(False)
        if detail:
            self.notify.emit(f"Kayıt kaydedildi: {detail}", "success")
        else:
            self.notify.emit("Ekran kaydı durdu.", "error")

    # --- drag and drop ------------------------------------------------------
    def _handle_drop(self, paths: list[str]) -> None:
        """Dropped APKs are installed; any other file goes to the Download folder."""
        serial = self._serial
        if serial is None:
            self.notify.emit("Cihaz bağlı değil.", "error")
            return
        service = DeviceService(self._adb, serial)
        apks = [path for path in paths if path.lower().endswith(APK_SUFFIX)]
        files = [path for path in paths if path not in apks]
        if apks:
            names = "\n".join(Path(path).name for path in apks)
            answer = QMessageBox.question(self, "APK kur", f"Bu uygulamalar telefona kurulsun mu?\n\n{names}")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.notify.emit(f"{len(paths)} dosya aktarılıyor…", "info")

        def transfer() -> str:
            for path in apks:
                service.install_apk(path)
            for path in files:
                service.push(path, DROP_TARGET_DIR)
            parts = []
            if apks:
                parts.append(f"{len(apks)} APK kuruldu")
            if files:
                parts.append(f"{len(files)} dosya {DROP_TARGET_DIR} klasörüne gönderildi")
            return ", ".join(parts)

        run_async(
            transfer,
            lambda summary: self.notify.emit(summary, "success"),
            lambda message: self.notify.emit(f"Aktarım başarısız: {message}", "error"),
        )
