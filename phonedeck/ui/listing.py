"""Two-line list rows (avatar, title, subtitle, trailing note) shared by several pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from . import theme

ROW_HEIGHT = 58
AVATAR_SIZE = 36
META_WIDTH = 130
AVATAR_COLORS = ("#7C5CFF", "#22D3EE", "#34D399", "#FBBF24", "#F472B6", "#60A5FA")
MS_PER_SECOND = 1000


@dataclass(frozen=True)
class Row:
    title: str
    subtitle: str = ""
    meta: str = ""
    payload: Any = None
    icon: QPixmap | None = None
    color: str = ""
    is_bold: bool = True


def avatar_color(seed: str) -> str:
    return AVATAR_COLORS[sum(map(ord, seed)) % len(AVATAR_COLORS)]


def format_timestamp(timestamp_ms: int) -> str:
    """Time for today, otherwise day.month and time."""
    moment = datetime.fromtimestamp(timestamp_ms / MS_PER_SECOND)
    pattern = "%H:%M" if moment.date() == datetime.now().date() else "%d.%m.%Y %H:%M"
    return moment.strftime(pattern)


class RowDelegate(QStyledItemDelegate):
    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row: Row = index.data(Qt.ItemDataRole.UserRole)
        rect = option.rect.adjusted(4, 3, -4, -3)
        painter.save()
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        painter.setPen(Qt.PenStyle.NoPen)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setBrush(QColor(124, 92, 255, 56))
            painter.drawRoundedRect(rect, 10, 10)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setBrush(QColor(theme.CARD_HOVER))
            painter.drawRoundedRect(rect, 10, 10)

        avatar = QRect(rect.x() + 10, rect.center().y() - AVATAR_SIZE // 2 + 1, AVATAR_SIZE, AVATAR_SIZE)
        bold = QFont(option.font)
        bold.setBold(True)
        painter.setFont(bold)
        if row.icon is not None:
            painter.drawPixmap(avatar, row.icon)
        else:
            painter.setBrush(QColor(row.color or avatar_color(row.title)))
            painter.drawRoundedRect(avatar, 11, 11)
            painter.setPen(QColor("#0B0D12"))
            painter.drawText(avatar, Qt.AlignmentFlag.AlignCenter, row.title[:1].upper())

        text_x = avatar.right() + 14
        meta_width = META_WIDTH if row.meta else 0
        text_width = rect.right() - text_x - meta_width - 12
        vertical = Qt.AlignmentFlag.AlignVCenter
        painter.setFont(bold if row.is_bold else option.font)
        painter.setPen(QColor(theme.TEXT))
        metrics = painter.fontMetrics()
        title = metrics.elidedText(row.title, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(QRect(text_x, rect.y() + 8, text_width, 20), vertical, title)

        painter.setFont(option.font)
        painter.setPen(QColor(theme.MUTED))
        metrics = painter.fontMetrics()
        subtitle = metrics.elidedText(
            " ".join(row.subtitle.split()), Qt.TextElideMode.ElideRight, text_width
        )
        painter.drawText(QRect(text_x, rect.y() + 28, text_width, 18), vertical, subtitle)
        if row.meta:
            painter.drawText(
                QRect(rect.right() - META_WIDTH - 10, rect.y(), META_WIDTH, rect.height()),
                vertical | Qt.AlignmentFlag.AlignRight,
                row.meta,
            )
        painter.restore()


def make_row_list() -> QListWidget:
    widget = QListWidget()
    widget.setItemDelegate(RowDelegate(widget))
    widget.setMouseTracking(True)
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setMinimumHeight(360)
    return widget


def fill_rows(widget: QListWidget, rows: list[Row]) -> None:
    """Replace the list content, keeping the scroll position."""
    scrollbar = widget.verticalScrollBar()
    position = scrollbar.value()
    widget.clear()
    for row in rows:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, row)
        widget.addItem(item)
    scrollbar.setValue(position)


def selected_row(widget: QListWidget) -> Row | None:
    item = widget.currentItem()
    return item.data(Qt.ItemDataRole.UserRole) if item is not None else None
