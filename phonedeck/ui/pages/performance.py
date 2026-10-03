"""Performance page: live charts for CPU, memory, battery and network, plus a process table."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from ...core.adb import Adb
from ...core.parsers import format_size
from ...core.perf import (
    PerfRates,
    PerfSample,
    ProcessInfo,
    compute_rates,
    force_stop,
    read_sample,
)
from .. import theme
from ..widgets import AppContext, Card, Page, button, label

logger = logging.getLogger(__name__)

WINDOW_SAMPLES = 60
INTERVAL_CHOICES_MS = (("1 sn", 1_000), ("2 sn", 2_000), ("5 sn", 5_000))
DEFAULT_INTERVAL_INDEX = 1
CHART_MIN_HEIGHT = 170
CHART_PADDING = 14
CHART_HEADER_HEIGHT = 58
GRID_LINES = 3
FILL_ALPHA = 110
LINE_WIDTH = 2.0
MIN_TEMPERATURE_SPAN = 6.0
CORE_BAR_WIDTH = 22
CORE_BAR_GAP = 10
CORE_AREA_HEIGHT = 120
PROCESS_MIN_HEIGHT = 340
NAME_COLUMN_MIN = 240
NUMBER_COLUMN_WIDTH = 100
KHZ_PER_GHZ = 1_000_000
PERCENT_SCALE = 100.0
USER_ROLE = Qt.ItemDataRole.UserRole
MIN_RATE_SPAN = 1024.0


def percent_text(value: float) -> str:
    return f"%{value:.0f}"


def temperature_text(value: float) -> str:
    return f"{value:.1f}°C"


def rate_text(value: float) -> str:
    return f"{format_size(int(value))}/sn"


@dataclass(frozen=True)
class SeriesStyle:
    prefix: str
    color: str


def value_range(
    values: list[float], fixed: tuple[float, float] | None, min_span: float
) -> tuple[float, float]:
    """Y-axis bounds: fixed when given, otherwise fitted to the data with some headroom."""
    if fixed is not None:
        return fixed
    low, high = min(values, default=0.0), max(values, default=0.0)
    if high - low < min_span:
        middle = (high + low) / 2
        low, high = middle - min_span / 2, middle + min_span / 2
    return low, high


class LineChart(QWidget):
    """Antialiased rolling line chart with a gradient fill, current value and min/max."""

    def __init__(
        self,
        title: str,
        value_text: Callable[[float], str],
        series: tuple[SeriesStyle, ...],
        fixed_range: tuple[float, float] | None = None,
        min_span: float = 1.0,
        starts_at_zero: bool = True,
    ) -> None:
        super().__init__()
        self._title = title
        self._value_text = value_text
        self._styles = series
        self._fixed_range = fixed_range
        self._min_span = min_span
        self._starts_at_zero = starts_at_zero
        self._detail = ""
        self._series: list[deque[float]] = [deque(maxlen=WINDOW_SAMPLES) for _ in series]
        self.setMinimumHeight(CHART_MIN_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def push(self, *values: float) -> None:
        for history, value in zip(self._series, values):
            history.append(value)
        self.update()

    def set_detail(self, text: str) -> None:
        if text != self._detail:
            self._detail = text
            self.update()

    def clear(self) -> None:
        for history in self._series:
            history.clear()
        self._detail = ""
        self.update()

    # --- painting -----------------------------------------------------------
    def _bounds(self) -> tuple[float, float]:
        everything = [value for history in self._series for value in history]
        if self._fixed_range is None and self._starts_at_zero:
            return 0.0, max(max(everything, default=0.0) * 1.15, self._min_span)
        return value_range(everything, self._fixed_range, self._min_span)

    def _plot_rect(self) -> QRectF:
        return QRectF(
            CHART_PADDING,
            CHART_HEADER_HEIGHT,
            self.width() - 2 * CHART_PADDING,
            self.height() - CHART_HEADER_HEIGHT - CHART_PADDING,
        )

    def _points(self, history: deque[float], bounds: tuple[float, float]) -> list[QPointF]:
        plot = self._plot_rect()
        low, high = bounds
        step = plot.width() / (WINDOW_SAMPLES - 1)
        count = len(history)
        return [
            QPointF(
                plot.right() - (count - 1 - index) * step,
                plot.bottom() - plot.height() * (min(max(value, low), high) - low) / (high - low),
            )
            for index, value in enumerate(history)
        ]

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.setBrush(QColor(theme.CARD))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 16, 16)
        bounds = self._bounds()
        self._draw_grid(painter)
        for history, style in zip(self._series, self._styles):
            self._draw_series(painter, self._points(history, bounds), QColor(style.color))
        self._draw_header(painter)
        painter.end()

    def _draw_grid(self, painter: QPainter) -> None:
        plot = self._plot_rect()
        painter.setPen(QPen(QColor(theme.BORDER), 1, Qt.PenStyle.DotLine))
        for line in range(GRID_LINES + 1):
            y = plot.top() + plot.height() * line / GRID_LINES
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

    def _draw_series(self, painter: QPainter, points: list[QPointF], color: QColor) -> None:
        if len(points) < 2:
            return
        plot = self._plot_rect()
        line = QPainterPath(points[0])
        for point in points[1:]:
            line.lineTo(point)
        area = QPainterPath(line)
        area.lineTo(points[-1].x(), plot.bottom())
        area.lineTo(points[0].x(), plot.bottom())
        area.closeSubpath()
        gradient = QLinearGradient(0, plot.top(), 0, plot.bottom())
        top_color = QColor(color)
        top_color.setAlpha(FILL_ALPHA)
        bottom_color = QColor(color)
        bottom_color.setAlpha(0)
        gradient.setColorAt(0, top_color)
        gradient.setColorAt(1, bottom_color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawPath(area)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, LINE_WIDTH, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.drawPath(line)

    def _current_text(self) -> str:
        parts = []
        for history, style in zip(self._series, self._styles):
            if history:
                parts.append(f"{style.prefix}{self._value_text(history[-1])}".strip())
        return "   ".join(parts) if parts else "—"

    def _range_text(self) -> str:
        everything = [value for history in self._series for value in history]
        if not everything:
            return ""
        return f"min {self._value_text(min(everything))} · maks {self._value_text(max(everything))}"

    def _draw_header(self, painter: QPainter) -> None:
        width = self.width() - 2 * CHART_PADDING
        left = CHART_PADDING
        painter.setFont(self._font(12, QFont.Weight.DemiBold))
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(QRectF(left, 10, width, 18), Qt.AlignmentFlag.AlignLeft, self._title)
        painter.setFont(self._font(15, QFont.Weight.Bold))
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(QRectF(left, 8, width, 22), Qt.AlignmentFlag.AlignRight, self._current_text())
        painter.setFont(self._font(11, QFont.Weight.Normal))
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(QRectF(left, 32, width * 0.62, 16), Qt.AlignmentFlag.AlignLeft, self._detail)
        painter.drawText(QRectF(left, 32, width, 16), Qt.AlignmentFlag.AlignRight, self._range_text())

    @staticmethod
    def _font(pixel_size: int, weight: QFont.Weight) -> QFont:
        font = QFont(QGuiApplication.font())
        font.setPixelSize(pixel_size)
        font.setWeight(weight)
        return font


class CoreBars(QWidget):
    """One small vertical bar per CPU core."""

    def __init__(self) -> None:
        super().__init__()
        self._usage: tuple[float, ...] = ()
        self.setMinimumHeight(CORE_AREA_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_usage(self, usage: tuple[float, ...]) -> None:
        self._usage = usage
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        count = len(self._usage)
        if not count:
            return
        slot = CORE_BAR_WIDTH + CORE_BAR_GAP
        left = (self.width() - (count * slot - CORE_BAR_GAP)) / 2
        text_height = 14
        track_height = self.height() - 2 * text_height - 6
        font = QFont(QGuiApplication.font())
        font.setPixelSize(11)
        painter.setFont(font)
        for index, value in enumerate(self._usage):
            x = left + index * slot
            track = QRectF(x, text_height + 2, CORE_BAR_WIDTH, track_height)
            self._draw_bar(painter, track, value)
            painter.setPen(QColor(theme.MUTED))
            caption = QRectF(x - 4, 0, CORE_BAR_WIDTH + 8, text_height)
            painter.drawText(caption, Qt.AlignmentFlag.AlignCenter, f"{value:.0f}")
            below = QRectF(x - 4, track.bottom() + 2, CORE_BAR_WIDTH + 8, text_height)
            painter.drawText(below, Qt.AlignmentFlag.AlignCenter, str(index))

    @staticmethod
    def _draw_bar(painter: QPainter, track: QRectF, value: float) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BACKGROUND))
        painter.drawRoundedRect(track, 6, 6)
        filled = track.height() * min(max(value, 0.0), PERCENT_SCALE) / PERCENT_SCALE
        if filled <= 0:
            return
        bar = QRectF(track.left(), track.bottom() - filled, track.width(), filled)
        gradient = QLinearGradient(0, bar.top(), 0, track.bottom())
        gradient.setColorAt(0, QColor(theme.ACCENT_ALT))
        gradient.setColorAt(1, QColor(theme.ACCENT))
        painter.setBrush(gradient)
        painter.drawRoundedRect(bar, 6, 6)


class ProcessItem(QTreeWidgetItem):
    """Table row that sorts numbers numerically and names case-insensitively."""

    def __init__(self, process: ProcessInfo) -> None:
        super().__init__()
        self.process = process
        for column in (1, 2, 3):
            self.setTextAlignment(column, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.update_from(process)

    def update_from(self, process: ProcessInfo) -> None:
        self.process = process
        texts = (process.name, str(process.pid), f"{process.cpu_percent:.1f}", format_size(process.rss_bytes))
        keys = (process.name.casefold(), process.pid, process.cpu_percent, process.rss_bytes)
        for column, (text, key) in enumerate(zip(texts, keys)):
            if self.text(column) != text:
                self.setText(column, text)
            self.setData(column, USER_ROLE, key)
        self.setToolTip(0, f"{process.name}\nKullanıcı: {process.user}")

    def __lt__(self, other: QTreeWidgetItem) -> bool:
        column = self.treeWidget().sortColumn()
        return self.data(column, USER_ROLE) < other.data(column, USER_ROLE)


def matches_filter(process: ProcessInfo, query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return True
    return needle in process.name.casefold() or needle in str(process.pid) or needle in process.user.casefold()


def read_quietly(adb: Adb, serial: str) -> PerfSample | None:
    """Poll result or None; always returns so the 'one sample at a time' guard is released."""
    try:
        return read_sample(adb, serial)
    except Exception as exc:  # noqa: BLE001 - a failed poll must never raise into the UI
        logger.info("performance sample failed: %s", exc)
        return None


class PerformancePage(Page):
    def __init__(self, context: AppContext) -> None:
        super().__init__(context, "Performans", "CPU, bellek, pil ve ağ durumunu canlı izle")
        self._previous: PerfSample | None = None
        self._is_sampling = False
        self._items: dict[int, ProcessItem] = {}
        self._last_processes: tuple[ProcessInfo, ...] = ()

        self._status = label("", "muted")
        self._interval = QComboBox()
        for text, milliseconds in INTERVAL_CHOICES_MS:
            self._interval.addItem(text, milliseconds)
        self._interval.setCurrentIndex(DEFAULT_INTERVAL_INDEX)
        self._interval.currentIndexChanged.connect(self._apply_interval)
        self._pause = button("Duraklat", theme.Glyph.PAUSE, tooltip="Örneklemeyi durdur veya sürdür")
        self._pause.setCheckable(True)
        self._pause.toggled.connect(self._on_pause_toggled)
        self.header.addWidget(self._status)
        self.header.addWidget(label("Aralık", "muted"))
        self.header.addWidget(self._interval)
        self.header.addWidget(self._pause)

        self._build_charts()
        self._build_processes()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._apply_interval()

    # --- layout -------------------------------------------------------------
    def _build_charts(self) -> None:
        self._cpu_chart = LineChart("İşlemci", percent_text, (SeriesStyle("", theme.ACCENT),), (0.0, PERCENT_SCALE))
        self._memory_chart = LineChart("Bellek", percent_text, (SeriesStyle("", theme.ACCENT_ALT),), (0.0, PERCENT_SCALE))
        self._temperature_chart = LineChart(
            "Batarya sıcaklığı", temperature_text, (SeriesStyle("", theme.WARNING),),
            min_span=MIN_TEMPERATURE_SPAN, starts_at_zero=False,
        )
        self._network_chart = LineChart(
            "Ağ", rate_text, (SeriesStyle("↓ ", theme.ACCENT_ALT), SeriesStyle("↑ ", theme.ACCENT)),
            min_span=MIN_RATE_SPAN,
        )
        grid = QGridLayout()
        grid.setSpacing(16)
        grid.addWidget(self._cpu_chart, 0, 0)
        grid.addWidget(self._memory_chart, 0, 1)
        grid.addWidget(self._temperature_chart, 1, 0)
        grid.addWidget(self._network_chart, 1, 1)
        self.content.addLayout(grid)

        cores = Card(spacing=8)
        self._core_title = label("Çekirdek yükü", "sectionTitle")
        self._core_detail = label("", "muted")
        title_row = QHBoxLayout()
        title_row.addWidget(self._core_title)
        title_row.addStretch()
        title_row.addWidget(self._core_detail)
        self._core_bars = CoreBars()
        cores.body.addLayout(title_row)
        cores.body.addWidget(self._core_bars)
        self.content.addWidget(cores)

    def _build_processes(self) -> None:
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title_row.addWidget(label("Süreçler", "sectionTitle"))
        self._process_count = label("", "muted")
        title_row.addWidget(self._process_count)
        title_row.addStretch()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Ada, PID'ye veya kullanıcıya göre ara")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(260)
        self._search.textChanged.connect(self._apply_filter)
        title_row.addWidget(self._search)
        self.content.addLayout(title_row)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Ad", "PID", "CPU %", "Bellek"])
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setMinimumHeight(PROCESS_MIN_HEIGHT)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setMinimumSectionSize(NAME_COLUMN_MIN // 3)
        header.setStretchLastSection(False)
        for column in (1, 2, 3):
            self._tree.setColumnWidth(column, NUMBER_COLUMN_WIDTH)
            self._tree.headerItem().setTextAlignment(column, Qt.AlignmentFlag.AlignRight)
        self._tree.setSortingEnabled(True)
        self._tree.sortByColumn(2, Qt.SortOrder.DescendingOrder)
        self._tree.itemSelectionChanged.connect(self._update_buttons)
        self.content.addWidget(self._tree, 1)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self._stop_button = button("Uygulamayı Durdur", theme.Glyph.STOP, "danger", icon_color=theme.DANGER)
        self._stop_button.clicked.connect(self._stop_selected)
        self._copy_button = button("PID'yi Kopyala", theme.Glyph.LINK)
        self._copy_button.clicked.connect(self._copy_pid)
        actions.addWidget(self._stop_button)
        actions.addWidget(self._copy_button)
        actions.addStretch()
        self.content.addLayout(actions)
        self._update_buttons()

    # --- sampling -----------------------------------------------------------
    def set_service(self, service) -> None:
        self._previous = None
        self._is_sampling = False
        self._clear_display()
        super().set_service(service)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        # After a pause in sampling the old counters would give one misleading average.
        self._previous = None
        super().hideEvent(event)

    def refresh(self) -> None:
        self._sample()

    def _apply_interval(self) -> None:
        self._timer.start(int(self._interval.currentData()))

    def _on_pause_toggled(self, is_paused: bool) -> None:
        self._pause.setText(" Sürdür" if is_paused else " Duraklat")
        self._pause.setIcon(theme.make_icon(theme.Glyph.PLAY if is_paused else theme.Glyph.PAUSE, theme.TEXT, 16))
        if is_paused:
            self._previous = None
        else:
            self._sample()

    def _tick(self) -> None:
        if self._pause.isChecked() or not self.isVisible():
            return
        self._sample()

    def _sample(self) -> None:
        service = self.service
        if service is None or self._is_sampling:
            return
        self._is_sampling = True
        adb, serial = self.context.adb, service.serial
        self.read(lambda: read_quietly(adb, serial), lambda sample: self._on_sample(service, sample), is_quiet=True)

    def _on_sample(self, service, sample: PerfSample | None) -> None:
        if self.service is not service:
            return
        self._is_sampling = False
        if sample is None:
            self._status.setText("Veri alınamıyor…")
            return
        self._status.setText("")
        previous, self._previous = self._previous, sample
        rates = compute_rates(previous, sample) if previous and previous.cpu and sample.cpu else None
        self._show_charts(sample, rates)
        self._show_processes(sample.processes)

    # --- display ------------------------------------------------------------
    def _clear_display(self) -> None:
        for chart in (self._cpu_chart, self._memory_chart, self._temperature_chart, self._network_chart):
            chart.clear()
        self._core_bars.set_usage(())
        self._core_detail.setText("")
        self._status.setText("")
        self._tree.clear()
        self._items.clear()
        self._last_processes = ()
        self._process_count.setText("")

    def _show_charts(self, sample: PerfSample, rates: PerfRates | None) -> None:
        memory = sample.memory
        self._memory_chart.push(memory.percent)
        self._memory_chart.set_detail(
            f"{format_size(memory.used)} / {format_size(memory.total)} · Takas {format_size(memory.swap_used)}"
        )
        battery = sample.battery
        if battery.temperature_c is not None:
            self._temperature_chart.push(battery.temperature_c)
        self._temperature_chart.set_detail(self._battery_detail(sample))
        if rates is None:
            return
        self._cpu_chart.push(rates.cpu_total)
        self._cpu_chart.set_detail(self._frequency_detail(sample))
        self._network_chart.push(rates.rx_bytes_per_s, rates.tx_bytes_per_s)
        self._core_bars.set_usage(rates.cpu_cores)
        self._core_detail.setText(f"{len(rates.cpu_cores)} çekirdek")

    @staticmethod
    def _battery_detail(sample: PerfSample) -> str:
        battery = sample.battery
        parts = []
        if battery.level is not None:
            parts.append(f"%{battery.level}")
        if battery.voltage_mv is not None:
            parts.append(f"{battery.voltage_mv / 1000:.2f} V")
        if battery.current_ma is not None:
            parts.append(f"{battery.current_ma} mA")
        parts.append(battery.health_label)
        return " · ".join(parts)

    @staticmethod
    def _frequency_detail(sample: PerfSample) -> str:
        if not sample.frequencies_khz:
            return ""
        low, high = min(sample.frequencies_khz), max(sample.frequencies_khz)
        return f"Frekans {low / KHZ_PER_GHZ:.2f}–{high / KHZ_PER_GHZ:.2f} GHz"

    # --- process table ------------------------------------------------------
    def _show_processes(self, processes: tuple[ProcessInfo, ...]) -> None:
        self._last_processes = processes
        scroll = self._tree.verticalScrollBar()
        position = scroll.value()
        selected = self._selected_process()
        self._tree.setUpdatesEnabled(False)
        self._tree.setSortingEnabled(False)
        self._merge_items(processes)
        self._tree.setSortingEnabled(True)
        if selected is not None and selected.pid in self._items:
            self._tree.setCurrentItem(self._items[selected.pid])
        scroll.setValue(position)
        self._tree.setUpdatesEnabled(True)
        self._apply_filter()

    def _merge_items(self, processes: tuple[ProcessInfo, ...]) -> None:
        alive = {process.pid for process in processes}
        for pid in [pid for pid in self._items if pid not in alive]:
            item = self._items.pop(pid)
            self._tree.takeTopLevelItem(self._tree.indexOfTopLevelItem(item))
        for process in processes:
            item = self._items.get(process.pid)
            if item is None:
                item = ProcessItem(process)
                self._items[process.pid] = item
                self._tree.addTopLevelItem(item)
            else:
                item.update_from(process)

    def _apply_filter(self) -> None:
        query = self._search.text()
        visible = 0
        for item in self._items.values():
            is_match = matches_filter(item.process, query)
            item.setHidden(not is_match)
            visible += is_match
        total = len(self._items)
        self._process_count.setText(f"{visible} / {total}" if query.strip() else f"{total} süreç")

    def _selected_process(self) -> ProcessInfo | None:
        items = self._tree.selectedItems()
        return items[0].process if items and isinstance(items[0], ProcessItem) else None

    def _update_buttons(self) -> None:
        has_selection = self._selected_process() is not None
        self._stop_button.setEnabled(has_selection)
        self._copy_button.setEnabled(has_selection)

    # --- actions ------------------------------------------------------------
    def _copy_pid(self) -> None:
        process = self._selected_process()
        if process is None:
            self.context.notify("Önce bir süreç seç.", "error")
            return
        QGuiApplication.clipboard().setText(str(process.pid))
        self.context.notify(f"PID kopyalandı: {process.pid}", "success")

    def _stop_selected(self) -> None:
        process = self._selected_process()
        if process is None:
            self.context.notify("Önce bir süreç seç.", "error")
            return
        package = process.package
        if package is None:
            self.context.notify("Bu bir sistem süreci; yalnızca uygulamalar durdurulabilir.", "error")
            return
        answer = QMessageBox.warning(
            self,
            "Uygulamayı durdur",
            f"{package} zorla durdurulacak. Kaydedilmemiş veriler kaybolabilir.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        adb, serial = self.context.adb, self.service.serial
        self.run(lambda: force_stop(adb, serial, package), success=f"Durduruldu: {package}")
