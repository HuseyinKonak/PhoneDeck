"""Camera and audio page: watch the phone's cameras and listen to its microphone or playback."""

from __future__ import annotations

import logging
import time
import wave
from pathlib import Path

import av
import numpy as np
from PySide6.QtCore import QObject, QRectF, QSize, QStandardPaths, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QProgressBar, QSizePolicy, QSlider, QWidget

from ...core.scrcpy.client import AudioClient, ScrcpyClient, ScrcpyOptions
from .. import theme
from ..mirror import FrameBridge, media_path
from ..widgets import AppContext, Card, Page, button, label

logger = logging.getLogger(__name__)

CAMERA_CHOICES = (("Arka kamera", "back"), ("Ön kamera", "front"), ("Harici kamera", "external"))
CAMERA_SIZES = (("1920", 1920), ("1280", 1280), ("960", 960), ("640", 640))
AUDIO_CHOICES = (
    ("Mikrofon", "mic", False),
    ("Telefonun sesi (telefonda da çalsın)", "playback", True),
    ("Telefonun sesi (yalnızca bilgisayarda)", "output", False),
)
CAMERA_BIT_RATE = 8_000_000
CAMERA_FPS = 30
VIEW_MIN_HEIGHT = 300
SINK_BUFFER_MS = 120
MS_PER_SECOND = 1000
LEVEL_MAX = 100
INT16_PEAK = 32768
DEFAULT_VOLUME = 80
SHUTDOWN_WAIT_SECONDS = 2


class CameraView(QWidget):
    """Shows camera frames, letter-boxed and with rounded corners."""

    target_changed = Signal(QSize)

    def __init__(self) -> None:
        super().__init__()
        self._image: QImage | None = None
        self._message = "Kamera kapalı"
        self.setMinimumHeight(VIEW_MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_image(self, image: QImage | None, message: str = "") -> None:
        self._image = image
        self._message = message or self._message
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self.target_changed.emit(self.size() * self.devicePixelRatioF())

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        area = QRectF(self.rect())
        painter.setPen(QColor(theme.BORDER))
        painter.setBrush(QColor("#05060A"))
        painter.drawRoundedRect(area.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)
        if self._image is None:
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(area, Qt.AlignmentFlag.AlignCenter, self._message)
            return
        scale = min(area.width() / self._image.width(), area.height() / self._image.height())
        width, height = self._image.width() * scale, self._image.height() * scale
        target = QRectF((area.width() - width) / 2, (area.height() - height) / 2, width, height)
        painter.drawImage(target, self._image)


class AudioBridge(QObject):
    """Carries PCM chunks from the capture thread to the UI thread."""

    chunk = Signal(bytes)
    state_changed = Signal(int, str, str)


class MediaPage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Kamera ve Ses", "Telefonun kameralarını izle, mikrofonunu veya sesini dinle")
        self._camera: ScrcpyClient | None = None
        self._camera_session = 0
        self._last_frame: av.VideoFrame | None = None
        self._frames = FrameBridge()
        self._frames.frame_ready.connect(self._show_frame)
        self._frames.state_changed.connect(self._on_camera_state)

        self._audio: AudioClient | None = None
        self._audio_session = 0
        self._sink: QAudioSink | None = None
        self._sink_device = None
        self._wave: wave.Wave_write | None = None
        self._wave_path: Path | None = None
        self._sound = AudioBridge()
        self._sound.chunk.connect(self._play_chunk)
        self._sound.state_changed.connect(self._on_audio_state)

        self._view = CameraView()
        self._view.target_changed.connect(self._frames.set_target)
        self._facing = QComboBox()
        self._size = QComboBox()
        self._camera_toggle = button("Kamerayı Başlat", theme.Glyph.CAMERA, "primary", icon_color="white")
        self._camera_record = button("Video Kaydet", theme.Glyph.RECORD)
        self._camera_status = label("", "muted")
        self._source = QComboBox()
        self._audio_toggle = button("Dinlemeyi Başlat", theme.Glyph.VOLUME_UP, "primary", icon_color="white")
        self._save_audio = QCheckBox("WAV olarak kaydet")
        self._level = QProgressBar()
        self._volume = QSlider(Qt.Orientation.Horizontal)
        self._audio_status = label("", "muted")
        self._build_camera_card()
        self._build_audio_card()

    # --- layout -------------------------------------------------------------
    def _build_camera_card(self) -> None:
        card = Card()
        card.body.addWidget(label("Kamera", "sectionTitle"))
        for text, facing in CAMERA_CHOICES:
            self._facing.addItem(text, facing)
        for text, size in CAMERA_SIZES:
            self._size.addItem(f"{text} piksel", size)
        self._size.setCurrentIndex(1)
        photo = button("Fotoğraf Çek", theme.Glyph.CAMERA, tooltip="Görüntüyü Resimler klasörüne kaydet")
        photo.clicked.connect(self._take_photo)
        self._camera_toggle.clicked.connect(self._toggle_camera)
        self._camera_record.setCheckable(True)
        self._camera_record.clicked.connect(self._toggle_camera_recording)
        controls = QHBoxLayout()
        controls.setSpacing(10)
        for widget in (self._facing, self._size, self._camera_toggle, photo, self._camera_record):
            controls.addWidget(widget)
        controls.addStretch()
        card.body.addLayout(controls)
        card.body.addWidget(self._view, 1)
        card.body.addWidget(self._camera_status)
        self.content.addWidget(card, 1)

    def _build_audio_card(self) -> None:
        card = Card()
        card.body.addWidget(label("Ses", "sectionTitle"))
        for text, source, keep_on_phone in AUDIO_CHOICES:
            self._source.addItem(text, (source, keep_on_phone))
        self._audio_toggle.clicked.connect(self._toggle_audio)
        self._volume.setRange(0, 100)
        self._volume.setValue(DEFAULT_VOLUME)
        self._volume.setFixedWidth(140)
        self._volume.setToolTip("Bilgisayardaki çalma sesi")
        self._volume.valueChanged.connect(self._apply_volume)
        self._level.setRange(0, LEVEL_MAX)
        self._level.setTextVisible(False)
        controls = QHBoxLayout()
        controls.setSpacing(10)
        for widget in (self._source, self._audio_toggle, self._save_audio, self._volume):
            controls.addWidget(widget)
        controls.addStretch()
        card.body.addLayout(controls)
        card.body.addWidget(self._level)
        card.body.addWidget(self._audio_status)
        self.content.addWidget(card)

    # --- lifecycle ----------------------------------------------------------
    def set_service(self, service) -> None:
        if service is not self.service:
            self._stop_camera()
            self._stop_audio()
        super().set_service(service)

    def shutdown(self) -> None:
        camera, audio = self._camera, self._audio
        self._stop_camera()
        self._stop_audio()
        for client in (camera, audio):
            if client is not None:
                client.wait(SHUTDOWN_WAIT_SECONDS)

    # --- camera -------------------------------------------------------------
    def _toggle_camera(self) -> None:
        if self._camera is not None:
            self._stop_camera()
            return
        if self.service is None:
            return
        self._camera_session += 1
        session = self._camera_session
        options = ScrcpyOptions(
            camera_facing=self._facing.currentData(),
            max_size=self._size.currentData(),
            bit_rate=CAMERA_BIT_RATE,
            max_fps=CAMERA_FPS,
        )
        self._camera = ScrcpyClient(
            self.context.adb,
            self.service.serial,
            on_frame=self._on_frame,
            on_state=lambda state, detail: self._frames.state_changed.emit(session, state, detail),
            options=options,
        )
        self._camera.start()
        self._camera_toggle.setText(" Kamerayı Durdur")
        self._camera_status.setText("Kamera açılıyor…")
        self._view.set_image(None, "Kamera açılıyor…")

    def _on_frame(self, frame: av.VideoFrame) -> None:
        """Decoder thread: keep the full frame for photos, hand a scaled copy to the UI."""
        self._last_frame = frame
        self._frames.push(frame)

    def _stop_camera(self) -> None:
        self._camera_session += 1
        client, self._camera = self._camera, None
        self._last_frame = None
        if client is not None:
            client.stop()
        self._camera_toggle.setText(" Kamerayı Başlat")
        self._camera_record.setChecked(False)
        self._camera_status.setText("")
        self._view.set_image(None, "Kamera kapalı")

    def _show_frame(self) -> None:
        latest = self._frames.take()
        if latest is not None and self._camera is not None:
            self._view.set_image(latest[0])

    def _on_camera_state(self, session: int, state: str, detail: str) -> None:
        if state == "recording_stopped":
            self._camera_record.setChecked(False)
            if detail:
                self.context.notify(f"Video kaydedildi: {detail}", "success")
            return
        if session != self._camera_session:
            return
        if state == "connected":
            self._camera_status.setText(f"{self._facing.currentText()} · canlı")
        elif state == "error":
            self.context.notify(f"Kamera açılamadı: {detail}", "error")
        elif state == "stopped":
            self._stop_camera()

    def _take_photo(self) -> None:
        frame = self._last_frame
        if frame is None:
            self.context.notify("Önce kamerayı başlat.", "error")
            return
        target = media_path(QStandardPaths.StandardLocation.PicturesLocation, "kamera", ".png")
        array = np.ascontiguousarray(frame.to_ndarray(format="rgb24"))
        height, width, _ = array.shape
        image = QImage(array.data, width, height, 3 * width, QImage.Format.Format_RGB888)
        target.parent.mkdir(parents=True, exist_ok=True)
        if image.save(str(target)):
            self.context.notify(f"Fotoğraf kaydedildi: {target}", "success")
        else:
            self.context.notify("Fotoğraf kaydedilemedi.", "error")

    def _toggle_camera_recording(self) -> None:
        client = self._camera
        if client is None:
            self._camera_record.setChecked(False)
            self.context.notify("Önce kamerayı başlat.", "error")
            return
        if client.is_recording:
            self._camera_record.setChecked(False)
            self.run(
                client.stop_recording,
                lambda path: self.context.notify(
                    f"Video kaydedildi: {path}" if path else "Kayıt boş kaldı.", "success" if path else "error"
                ),
            )
            return
        target = media_path(QStandardPaths.StandardLocation.MoviesLocation, "kamera", ".mp4")
        is_started = client.start_recording(target)
        self._camera_record.setChecked(is_started)
        if is_started:
            self.context.notify("Video kaydı başladı (ilk anahtar kareden itibaren)", "info")

    # --- audio --------------------------------------------------------------
    def _toggle_audio(self) -> None:
        if self._audio is not None:
            self._stop_audio()
            return
        if self.service is None:
            return
        source, keep_on_phone = self._source.currentData()
        self._audio_session += 1
        session = self._audio_session
        self._open_sink()
        if self._save_audio.isChecked():
            self._open_wave(source)
        self._audio = AudioClient(
            self.context.adb,
            self.service.serial,
            on_audio=self._sound.chunk.emit,
            on_state=lambda state, detail: self._sound.state_changed.emit(session, state, detail),
            source=source,
            keep_on_phone=keep_on_phone,
        )
        self._audio.start()
        self._audio_toggle.setText(" Dinlemeyi Durdur")
        self._audio_status.setText("Ses bağlantısı kuruluyor…")

    def _open_sink(self) -> None:
        audio_format = QAudioFormat()
        audio_format.setSampleRate(AudioClient.SAMPLE_RATE)
        audio_format.setChannelCount(AudioClient.CHANNELS)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        self._sink = QAudioSink(QMediaDevices.defaultAudioOutput(), audio_format, self)
        frame_bytes = AudioClient.CHANNELS * AudioClient.SAMPLE_BYTES
        self._sink.setBufferSize(AudioClient.SAMPLE_RATE * frame_bytes * SINK_BUFFER_MS // MS_PER_SECOND)
        self._sink_device = self._sink.start()
        self._apply_volume()

    def _open_wave(self, source: str) -> None:
        self._wave_path = media_path(QStandardPaths.StandardLocation.MusicLocation, f"ses-{source}", ".wav")
        try:
            self._wave_path.parent.mkdir(parents=True, exist_ok=True)
            self._wave = wave.open(str(self._wave_path), "wb")
            self._wave.setnchannels(AudioClient.CHANNELS)
            self._wave.setsampwidth(AudioClient.SAMPLE_BYTES)
            self._wave.setframerate(AudioClient.SAMPLE_RATE)
        except OSError as exc:
            self._wave = None
            self.context.notify(f"Ses dosyası açılamadı: {exc}", "error")

    def _apply_volume(self) -> None:
        if self._sink is not None:
            self._sink.setVolume(self._volume.value() / 100)

    def _play_chunk(self, data: bytes) -> None:
        """UI thread: play, record and meter one PCM chunk."""
        if self._audio is None:
            return
        if self._sink_device is not None and self._sink is not None:
            # Drop what does not fit instead of queueing, so latency never grows.
            self._sink_device.write(data[: self._sink.bytesFree()])
        if self._wave is not None:
            self._wave.writeframes(data)
        samples = np.frombuffer(data, dtype=np.int16)
        peak = int(np.abs(samples).max()) if samples.size else 0
        self._level.setValue(min(LEVEL_MAX, peak * LEVEL_MAX // INT16_PEAK))

    def _stop_audio(self) -> None:
        self._audio_session += 1
        client, self._audio = self._audio, None
        if client is not None:
            client.stop()
        if self._sink is not None:
            self._sink.stop()
            self._sink = None
            self._sink_device = None
        if self._wave is not None:
            self._wave.close()
            self._wave = None
            self.context.notify(f"Ses kaydedildi: {self._wave_path}", "success")
        self._level.setValue(0)
        self._audio_toggle.setText(" Dinlemeyi Başlat")
        self._audio_status.setText("")

    def _on_audio_state(self, session: int, state: str, detail: str) -> None:
        if session != self._audio_session:
            return
        if state == "connected":
            self._audio_status.setText(f"{self._source.currentText()} · canlı · 48 kHz stereo")
        elif state == "error":
            self.context.notify(f"Ses alınamadı: {detail}", "error")
        elif state == "stopped":
            self._stop_audio()
