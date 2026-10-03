"""Runs the scrcpy server on the device and streams decoded frames."""

from __future__ import annotations

import logging
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import av

from ..adb import Adb, AdbError
from . import protocol
from .recorder import Mp4Recorder

logger = logging.getLogger(__name__)

SERVER_ASSET = Path(__file__).resolve().parents[2] / "assets" / "scrcpy-server"
REMOTE_SERVER_PATH = "/data/local/tmp/phonedeck-scrcpy-server.jar"
LOCALHOST = "127.0.0.1"
CONNECT_ATTEMPTS = 100
CONNECT_DELAY = 0.1
START_ATTEMPTS = 3
START_RETRY_DELAY = 0.5
MAX_SCID = 0x7FFFFFFF
DEFAULT_MAX_SIZE = 1920
DEFAULT_BIT_RATE = 8_000_000
DEFAULT_MAX_FPS = 60

DEVICE_MSG_CLIPBOARD = 0
DEVICE_MSG_ACK_CLIPBOARD = 1
DEVICE_MSG_UHID_OUTPUT = 2

FrameCallback = Callable[[av.VideoFrame], None]
StateCallback = Callable[[str, str], None]
ClipboardCallback = Callable[[str], None]


CAMERA_FACINGS = ("back", "front", "external")
AUDIO_SOURCES = ("mic", "output", "playback")
AUDIO_CODEC_META_LENGTH = 4
AUDIO_DISABLED_CODECS = (0, 1)  # 0 = the device refused to capture, 1 = configuration error

AudioCallback = Callable[[bytes], None]


@dataclass(frozen=True)
class ScrcpyOptions:
    """Stream quality and session behaviour."""

    max_size: int = DEFAULT_MAX_SIZE
    bit_rate: int = DEFAULT_BIT_RATE
    max_fps: int = DEFAULT_MAX_FPS
    stay_awake: bool = False
    show_touches: bool = False
    power_off_on_close: bool = False
    # "" mirrors the screen; "back" / "front" / "external" streams that camera instead.
    camera_facing: str = ""

    @property
    def is_camera(self) -> bool:
        return bool(self.camera_facing)

    def server_arguments(self) -> list[str]:
        if self.is_camera and self.camera_facing not in CAMERA_FACINGS:
            raise ValueError(f"Geçersiz kamera yönü: {self.camera_facing}")
        flags = {
            "stay_awake": self.stay_awake,
            "show_touches": self.show_touches,
            "power_off_on_close": self.power_off_on_close,
        }
        source = (
            ["video_source=camera", f"camera_facing={self.camera_facing}", "control=false"]
            if self.is_camera
            else ["control=true"]
        )
        return [
            "audio=false",
            *source,
            f"max_size={int(self.max_size)}",
            f"video_bit_rate={int(self.bit_rate)}",
            f"max_fps={int(self.max_fps)}",
            *(f"{name}=true" for name, is_enabled in flags.items() if is_enabled and not self.is_camera),
        ]


class ScrcpyClient:
    """One mirroring session. Callbacks fire on background threads.

    States reported through `on_state`: "connected" (detail = device name),
    "error" (detail = message), "recording_stopped" (detail = file path) and "stopped".
    """

    def __init__(
        self,
        adb: Adb,
        serial: str,
        on_frame: FrameCallback,
        on_state: StateCallback,
        on_clipboard: ClipboardCallback | None = None,
        options: ScrcpyOptions | None = None,
    ) -> None:
        self._adb = adb
        self._serial = serial
        self._options = options or ScrcpyOptions()
        self._record_lock = threading.Lock()
        self._recorder: Mp4Recorder | None = None
        self._record_request: Path | None = None
        self._config = b""
        self._frame_size = (0, 0)
        self._on_frame = on_frame
        self._on_state = on_state
        self._on_clipboard = on_clipboard
        self._stop_event = threading.Event()
        self._send_lock = threading.Lock()
        self._video: socket.socket | None = None
        self._control: socket.socket | None = None
        self._process = None
        self._port = 0
        self._thread = threading.Thread(target=self._run, name="scrcpy-video", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_sockets()

    # --- recording ----------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        with self._record_lock:
            return self._recorder is not None or self._record_request is not None

    def start_recording(self, path: Path) -> bool:
        """Record the stream to `path` (MP4). Writing starts at the next key frame."""
        with self._record_lock:
            if self._recorder is not None or self._record_request is not None:
                return False
            self._record_request = path
        # Ask the phone for a fresh key frame so the file starts immediately. Camera
        # sessions have no control channel; there the file starts at the next key frame.
        is_requested = self.send(protocol.simple(protocol.ControlType.RESET_VIDEO))
        if not is_requested and not self._options.is_camera:
            with self._record_lock:
                self._record_request = None
            return False
        return True

    def stop_recording(self) -> Path | None:
        """Finish the file. Returns its path, or None if nothing was recorded."""
        with self._record_lock:
            recorder, self._recorder = self._recorder, None
            self._record_request = None
        return self._finish(recorder)

    def _finish(self, recorder: Mp4Recorder | None) -> Path | None:
        if recorder is None:
            return None
        try:
            recorder.close()
        except (av.FFmpegError, OSError) as exc:
            logger.warning("closing recording failed: %s", exc)
            return None
        return recorder.path if recorder.packet_count else None

    def _record(self, header: protocol.FrameHeader, data: bytes) -> None:
        """Feed one media packet to the recorder, starting it on a key frame."""
        with self._record_lock:
            if self._recorder is None and self._record_request and header.is_key_frame and self._config:
                width, height = self._frame_size
                self._recorder = Mp4Recorder(self._record_request, width, height, self._config)
                self._record_request = None
            recorder = self._recorder
            if recorder is None:
                return
            try:
                recorder.write(data, header.pts_us, header.is_key_frame)
            except (av.FFmpegError, OSError, ValueError) as exc:
                logger.warning("recording failed: %s", exc)
                self._recorder = None
                self._finish(recorder)
                self._on_state("recording_stopped", "")

    def _restart_recording_on_new_config(self, config: bytes) -> None:
        """A new config means the video size changed (rotation): close the file."""
        with self._record_lock:
            if self._recorder is None or config == self._config:
                return
            recorder, self._recorder = self._recorder, None
        path = self._finish(recorder)
        self._on_state("recording_stopped", str(path) if path else "")

    def wait(self, timeout: float) -> None:
        """Block until the session thread has cleaned up (used at exit)."""
        if self._thread.is_alive():
            self._thread.join(timeout)

    def send(self, message: bytes) -> bool:
        """Send a control message. Returns False when not connected."""
        control = self._control
        if control is None or self._stop_event.is_set():
            return False
        try:
            with self._send_lock:
                control.sendall(message)
            return True
        except OSError as exc:
            logger.warning("control send failed: %s", exc)
            return False

    # --- session lifecycle --------------------------------------------------
    def _run(self) -> None:
        try:
            self._video = self._start_and_connect()
            self._stream(self._video)
        except (AdbError, OSError, av.FFmpegError) as exc:
            if not self._stop_event.is_set():
                logger.exception("mirroring session failed")
                self._on_state("error", str(exc))
        except Exception as exc:  # noqa: BLE001 - a frame callback bug must not kill the thread silently
            logger.exception("mirroring session crashed")
            self._on_state("error", f"Beklenmeyen hata: {exc}")
        finally:
            self._cleanup()
            self._on_state("stopped", "")

    def _server_arguments(self) -> list[str]:
        return self._options.server_arguments()

    def _stream(self, first: socket.socket) -> None:
        """Handle the session once the first socket is open (video here)."""
        if not self._options.is_camera:
            # The control socket must be opened before the server sends anything.
            self._control = socket.create_connection((LOCALHOST, self._port))
            self._control.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        device_name = protocol.parse_device_name(_recv_exact(first, protocol.DEVICE_NAME_LENGTH))
        _recv_exact(first, protocol.CODEC_META_LENGTH)
        if self._control is not None:
            threading.Thread(target=self._read_control, name="scrcpy-control", daemon=True).start()
        self._on_state("connected", device_name)
        self._decode_loop(first)

    def _start_and_connect(self) -> socket.socket:
        """Start the server and open the video socket, retrying a failed start.

        The server occasionally aborts while the phone is busy; a second try succeeds.
        """
        for attempt in range(1, START_ATTEMPTS + 1):
            try:
                self._start_server()
                return self._connect_video()
            except AdbError as exc:
                self._release_server()
                if attempt == START_ATTEMPTS or self._stop_event.is_set():
                    raise
                logger.warning("server start failed (attempt %d): %s", attempt, exc)
                time.sleep(START_RETRY_DELAY)
        raise AdbError("scrcpy sunucusu başlatılamadı.")

    def _start_server(self) -> None:
        if not SERVER_ASSET.is_file():
            raise AdbError(f"scrcpy-server dosyası eksik: {SERVER_ASSET}")
        self._adb.run(["push", str(SERVER_ASSET), REMOTE_SERVER_PATH], self._serial, timeout=60)
        # The id names the socket on the phone; unguessable so other apps cannot squat it.
        scid = f"{secrets.randbelow(MAX_SCID) + 1:08x}"
        self._port = _free_port()
        self._adb.run(
            ["forward", f"tcp:{self._port}", f"localabstract:scrcpy_{scid}"], self._serial
        )
        self._process = self._adb.popen(
            [
                "shell",
                f"CLASSPATH={REMOTE_SERVER_PATH}",
                "app_process",
                "/",
                "com.genymobile.scrcpy.Server",
                protocol.SERVER_VERSION,
                f"scid={scid}",
                "log_level=info",
                "tunnel_forward=true",
                *self._server_arguments(),
            ],
            self._serial,
        )

    def _connect_video(self) -> socket.socket:
        """Connect until the server answers with its dummy byte."""
        for _ in range(CONNECT_ATTEMPTS):
            if self._stop_event.is_set():
                raise OSError("Bağlantı iptal edildi.")
            if self._process.poll() is not None:
                output = self._process.stdout.read().decode("utf-8", "replace").strip()
                raise AdbError(f"scrcpy sunucusu başlamadı: {output[-300:]}")
            candidate = None
            try:
                candidate = socket.create_connection((LOCALHOST, self._port), timeout=2)
                if candidate.recv(1):
                    candidate.settimeout(None)
                    candidate.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    return candidate
                candidate.close()
            except OSError:
                if candidate is not None:
                    candidate.close()
            time.sleep(CONNECT_DELAY)
        raise AdbError("scrcpy sunucusuna bağlanılamadı.")

    def _decode_loop(self, video: socket.socket) -> None:
        codec = av.CodecContext.create("h264", "r")
        codec.thread_type = "SLICE"
        pending_config = b""
        while not self._stop_event.is_set():
            header = protocol.parse_frame_header(
                _recv_exact(video, protocol.FRAME_HEADER_LENGTH)
            )
            data = _recv_exact(video, header.size)
            if header.is_config:
                self._restart_recording_on_new_config(data)
                self._config = data
                pending_config = data
                continue
            packet = av.Packet(pending_config + data)
            pending_config = b""
            for frame in codec.decode(packet):
                self._frame_size = (frame.width, frame.height)
                self._on_frame(frame)
            self._record(header, data)

    def _read_control(self) -> None:
        """Drain device messages; forwards clipboard changes."""
        control = self._control
        try:
            while not self._stop_event.is_set():
                message_type = _recv_exact(control, 1)[0]
                if message_type == DEVICE_MSG_CLIPBOARD:
                    (length,) = struct.unpack(">I", _recv_exact(control, 4))
                    content = _recv_exact(control, length).decode("utf-8", "replace")
                    if self._on_clipboard:
                        self._on_clipboard(content)
                elif message_type == DEVICE_MSG_ACK_CLIPBOARD:
                    _recv_exact(control, 8)
                elif message_type == DEVICE_MSG_UHID_OUTPUT:
                    _, size = struct.unpack(">HH", _recv_exact(control, 4))
                    _recv_exact(control, size)
                else:
                    logger.warning("unknown device message type %d", message_type)
                    return
        except OSError:
            return

    def _close_sockets(self) -> None:
        for sock in (self._video, self._control):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _cleanup(self) -> None:
        self._stop_event.set()
        path = self.stop_recording()
        if path is not None:
            self._on_state("recording_stopped", str(path))
        self._close_sockets()
        self._control = None
        self._release_server()

    def _release_server(self) -> None:
        """Stop the server process and remove its port forward."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
        self._process = None
        if self._port:
            try:
                self._adb.run(["forward", "--remove", f"tcp:{self._port}"], self._serial, 10)
            except AdbError as exc:
                logger.debug("forward cleanup failed: %s", exc)
            self._port = 0


class AudioClient(ScrcpyClient):
    """Streams the phone's microphone or playback as raw PCM (48 kHz, stereo, 16-bit).

    Sources: "mic", "output" (playback, muted on the phone) and "playback" with
    `keep_on_phone` so the phone keeps playing too (Android 13+).
    """

    SAMPLE_RATE = 48_000
    CHANNELS = 2
    SAMPLE_BYTES = 2

    def __init__(
        self,
        adb: Adb,
        serial: str,
        on_audio: AudioCallback,
        on_state: StateCallback,
        source: str = "mic",
        keep_on_phone: bool = False,
    ) -> None:
        if source not in AUDIO_SOURCES:
            raise ValueError(f"Geçersiz ses kaynağı: {source}")
        super().__init__(adb, serial, on_frame=lambda frame: None, on_state=on_state)
        self._on_audio = on_audio
        self._source = source
        self._keep_on_phone = keep_on_phone and source == "playback"

    def _server_arguments(self) -> list[str]:
        arguments = [
            "video=false", "control=false", "audio=true", "audio_codec=raw",
            f"audio_source={self._source}",
        ]
        if self._keep_on_phone:
            arguments.append("audio_dup=true")
        return arguments

    def _stream(self, first: socket.socket) -> None:
        device_name = protocol.parse_device_name(_recv_exact(first, protocol.DEVICE_NAME_LENGTH))
        (codec,) = struct.unpack(">I", _recv_exact(first, AUDIO_CODEC_META_LENGTH))
        if codec in AUDIO_DISABLED_CODECS:
            raise AdbError("Telefon ses yakalamaya izin vermedi (ekran kilitli olabilir).")
        self._on_state("connected", device_name)
        while not self._stop_event.is_set():
            header = protocol.parse_frame_header(_recv_exact(first, protocol.FRAME_HEADER_LENGTH))
            data = _recv_exact(first, header.size)
            if not header.is_config:
                self._on_audio(data)


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise OSError("Bağlantı kapandı.")
        chunks.extend(chunk)
    return bytes(chunks)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOCALHOST, 0))
        return probe.getsockname()[1]
