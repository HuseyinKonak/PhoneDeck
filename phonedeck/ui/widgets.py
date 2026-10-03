"""Reusable building blocks for the pages."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.adb import Adb
from . import theme

logger = logging.getLogger(__name__)

TOAST_DURATION_MS = 2800
TOAST_MARGIN = 28
TOGGLE_SIZE = QSize(44, 24)
TOGGLE_ANIMATION_MS = 140
PAGE_MARGIN = 28


@dataclass(frozen=True)
class AppContext:
    """Shared services handed to every page."""

    adb: Adb
    notify: Callable[[str, str], None]
    send_control: Callable[[bytes], bool]


def label(text: str, object_name: str = "") -> QLabel:
    widget = QLabel(text)
    # Names and texts from the phone are shown as typed, never interpreted as HTML.
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if object_name:
        widget.setObjectName(object_name)
    return widget


def button(
    text: str = "",
    glyph: str = "",
    variant: str = "",
    tooltip: str = "",
    icon_color: str = theme.TEXT,
    icon_size: int = 16,
) -> QPushButton:
    widget = QPushButton(f" {text}" if text and glyph else text)
    if glyph:
        widget.setIcon(theme.make_icon(glyph, icon_color, icon_size))
        widget.setIconSize(QSize(icon_size, icon_size))
    if variant:
        widget.setProperty("variant", variant)
    if tooltip:
        widget.setToolTip(tooltip)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


def round_button(glyph: str, tooltip: str, size: int = 40) -> QPushButton:
    widget = button(glyph=glyph, variant="round", tooltip=tooltip)
    widget.setFixedSize(size, size)
    return widget


class Card(QFrame):
    def __init__(self, object_name: str = "card", spacing: int = 12) -> None:
        super().__init__()
        self.setObjectName(object_name)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 18, 20, 18)
        self.body.setSpacing(spacing)


class StatCard(Card):
    """Title, large value, detail line and a usage bar."""

    def __init__(self, title: str, glyph: str) -> None:
        super().__init__(spacing=8)
        header = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(theme.make_icon(glyph, theme.ACCENT_ALT, 16).pixmap(16, 16))
        header.addWidget(icon)
        header.addWidget(label(title, "muted"))
        header.addStretch()
        self._value = label("—", "statValue")
        self._detail = label("", "muted")
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self.body.addLayout(header)
        self.body.addWidget(self._value)
        self.body.addWidget(self._bar)
        self.body.addWidget(self._detail)

    def update_stat(self, value: str, detail: str, percent: int) -> None:
        self._value.setText(value)
        self._detail.setText(detail)
        self._bar.setValue(min(max(percent, 0), 100))


class ToggleSwitch(QAbstractButton):
    """Animated on/off switch."""

    def __init__(self) -> None:
        super().__init__()
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(TOGGLE_SIZE)
        self._offset = 0.0
        self._animation = QPropertyAnimation(self, b"offset", self)
        self._animation.setDuration(TOGGLE_ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def _animate(self, is_checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._offset)
        self._animation.setEndValue(1.0 if is_checked else 0.0)
        self._animation.start()

    def set_checked_silently(self, is_checked: bool) -> None:
        """Reflect device state without emitting `toggled`."""
        was_blocked = self.blockSignals(True)
        self.setChecked(is_checked)
        self.blockSignals(was_blocked)
        self._set_offset(1.0 if is_checked else 0.0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QColor(theme.ACCENT) if self.isChecked() else QColor("#2C3344")
        painter.setBrush(track)
        radius = self.height() / 2
        painter.drawRoundedRect(self.rect(), radius, radius)
        knob = self.height() - 6
        x = 3 + self._offset * (self.width() - knob - 6)
        painter.setBrush(QColor("white"))
        painter.drawEllipse(int(x), 3, knob, knob)


class Toast(QLabel):
    """Transient message pinned to the bottom of its parent."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, message: str, kind: str = "info") -> None:
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setText(message)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._timer.start(TOAST_DURATION_MS)

    def reposition(self) -> None:
        parent = self.parentWidget()
        x = (parent.width() - self.width()) // 2
        self.move(x, parent.height() - self.height() - TOAST_MARGIN)


class Page(QWidget):
    """Scrollable page with a title block. Subclasses fill `self.content`."""

    def __init__(self, context: AppContext, title: str, subtitle: str) -> None:
        super().__init__()
        self.context = context
        self.service = None
        self._epoch = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("scrollBody")
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.content = QVBoxLayout(body)
        self.content.setContentsMargins(PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN, PAGE_MARGIN)
        self.content.setSpacing(16)
        self.header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label(title, "pageTitle"))
        titles.addWidget(label(subtitle, "pageSubtitle"))
        self.header.addLayout(titles)
        self.header.addStretch()
        self.content.addLayout(self.header)

    def set_service(self, service) -> None:
        self.service = service
        self.setEnabled(service is not None)
        # Hidden pages load when they are opened, keeping connection time light.
        if service is not None and self.isVisible():
            self.refresh()

    def refresh(self) -> None:
        """Reload device data. Overridden by pages that show device state."""

    def on_shown(self) -> None:
        """Called when the user opens the page. Pages with cached lists override this."""
        self.refresh()

    def mark_changed(self) -> None:
        """Note a user action so reads that started earlier cannot undo it on screen."""
        self._epoch += 1

    def read(self, work: Callable, on_done: Callable, is_quiet: bool = False) -> None:
        """Like `run`, but the result is dropped if the user acted in the meantime."""
        epoch = self._epoch

        def deliver(result) -> None:
            if epoch == self._epoch:
                on_done(result)

        self.run(work, deliver, is_quiet=is_quiet)

    def run(
        self,
        work: Callable,
        on_done: Callable | None = None,
        success: str = "",
        is_quiet: bool = False,
    ) -> None:
        """Run a device call in the background with standard error reporting.

        `is_quiet` is for periodic polls: failures are logged instead of shown.
        """
        from .async_runner import run_async

        service = self.service

        def guarded():
            # Never let a queued action land on a device selected afterwards.
            if self.service is not service:
                raise RuntimeError("Cihaz değişti, işlem iptal edildi.")
            return work()

        def handle_done(result) -> None:
            if success:
                self.context.notify(success, "success")
            if on_done:
                on_done(result)

        def handle_error(message: str) -> None:
            if is_quiet:
                logger.info("background read failed: %s", message)
            else:
                self.context.notify(message, "error")

        run_async(guarded, handle_done, handle_error)
