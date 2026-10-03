"""Colors, icon glyphs and the application stylesheet."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap

BACKGROUND = "#0B0D12"
SURFACE = "#11141B"
CARD = "#161A23"
CARD_HOVER = "#1C212C"
BORDER = "#242A38"
TEXT = "#E9EBF1"
MUTED = "#8B93A7"
ACCENT = "#7C5CFF"
ACCENT_HOVER = "#8F73FF"
ACCENT_ALT = "#22D3EE"
SUCCESS = "#34D399"
WARNING = "#FBBF24"
DANGER = "#F2555A"

ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
ICON_RENDER_SCALE = 3


class Glyph:
    """Segoe Fluent Icons code points."""

    HOME = ""
    APPS = ""
    FOLDER = ""
    FILE = ""
    SLIDERS = ""
    TERMINAL = ""
    BACK = ""
    CIRCLE = ""
    RECENTS = ""
    POWER = ""
    VOLUME_UP = ""
    VOLUME_DOWN = ""
    MUTE = ""
    CAMERA = ""
    ROTATE = ""
    REFRESH = ""
    DELETE = ""
    DOWNLOAD = ""
    UPLOAD = ""
    PLAY = ""
    PAUSE = ""
    NEXT = ""
    PREVIOUS = ""
    STOP = ""
    WIFI = ""
    BLUETOOTH = ""
    BRIGHTNESS = ""
    PHONE = ""
    SEND = ""
    ADD = ""
    UP = ""
    GLOBE = ""
    BELL = ""
    SCREEN_OFF = ""
    RESTART = ""
    NEW_FOLDER = ""
    LINK = ""
    CALL = ""
    MESSAGE = ""
    CLOSE = ""
    RECORD = ""
    INFO = ""
    COPY = ""
    PASTE = ""
    CUT = ""
    RENAME = ""
    SHIELD = ""
    SEARCH = ""
    HISTORY = ""
    CONTACT = ""
    CHART = ""
    TOOLS = ""


def icon_font(pixel_size: int) -> QFont:
    families = set(QFontDatabase.families())
    family = next((name for name in ICON_FONTS if name in families), ICON_FONTS[-1])
    font = QFont(family)
    font.setPixelSize(pixel_size)
    return font


def make_icon(glyph: str, color: str = TEXT, size: int = 18) -> QIcon:
    """Render an icon-font glyph into a crisp QIcon."""
    side = size * ICON_RENDER_SCALE
    pixmap = QPixmap(side, side)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setFont(icon_font(side))
    painter.setPen(QColor(color))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    pixmap.setDevicePixelRatio(ICON_RENDER_SCALE)
    return QIcon(pixmap)


STYLESHEET = f"""
* {{
    font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {TEXT};
    outline: none;
}}
QMainWindow, QDialog, QMessageBox, QInputDialog {{ background: {BACKGROUND}; }}
QToolTip {{
    background: {CARD_HOVER}; color: {TEXT}; border: 1px solid {BORDER};
    padding: 5px 8px; border-radius: 6px;
}}

#sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
#brand {{ font-size: 18px; font-weight: 700; }}
#brandSub {{ color: {MUTED}; font-size: 11px; }}
#pageTitle {{ font-size: 24px; font-weight: 700; }}
#pageSubtitle {{ color: {MUTED}; font-size: 13px; }}
#sectionTitle {{ font-size: 14px; font-weight: 600; }}
#muted {{ color: {MUTED}; }}
#statValue {{ font-size: 26px; font-weight: 700; }}
#heroTitle {{ font-size: 22px; font-weight: 700; }}
#chip {{
    background: rgba(124, 92, 255, 0.16); color: #C9BDFF;
    border-radius: 10px; padding: 3px 10px; font-size: 12px; font-weight: 600;
}}

#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 16px; }}
#heroCard {{
    border: 1px solid #3A2F7A; border-radius: 16px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #2A1F66, stop:0.55 #1B1D3A, stop:1 #10202B);
}}
#phonePanel {{ background: {SURFACE}; border-left: 1px solid {BORDER}; }}
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}
#navSection {{ color: #5E667A; font-size: 10px; font-weight: 700; padding: 10px 12px 2px 12px; }}
QMenu {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 10px; padding: 6px;
}}
QMenu::item {{ padding: 7px 22px 7px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: rgba(124, 92, 255, 0.24); }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 8px; }}
QTabWidget::pane {{ border: none; }}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {MUTED}; padding: 8px 14px; margin-right: 4px;
    border-bottom: 2px solid transparent; font-weight: 600;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom-color: {ACCENT}; }}

QPushButton {{
    background: {CARD_HOVER}; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 8px 14px; font-weight: 600;
}}
QPushButton:hover {{ background: #232938; border-color: #323A4D; }}
QPushButton:pressed {{ background: #1A1F2A; }}
QPushButton:disabled {{ color: #565D70; background: {CARD}; }}
QPushButton[variant="primary"] {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton[variant="primary"]:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton[variant="danger"] {{ color: {DANGER}; }}
QPushButton[variant="danger"]:hover {{ background: rgba(242, 85, 90, 0.14); border-color: {DANGER}; }}
QPushButton[variant="ghost"] {{ background: transparent; border-color: transparent; }}
QPushButton[variant="ghost"]:hover {{ background: {CARD_HOVER}; }}
QPushButton[variant="tile"] {{
    background: {CARD}; border-radius: 14px; padding: 14px 12px; text-align: left;
}}
QPushButton[variant="tile"]:hover {{ background: {CARD_HOVER}; border-color: {ACCENT}; }}
QPushButton[variant="tile"]:checked, QPushButton[variant="round"]:checked {{
    background: rgba(124, 92, 255, 0.24); border-color: {ACCENT};
}}
QPushButton[variant="round"] {{ border-radius: 20px; padding: 0; background: {CARD}; }}
QPushButton[variant="round"]:hover {{ background: {CARD_HOVER}; border-color: {ACCENT}; }}

#navButton {{
    background: transparent; border: none; border-radius: 10px;
    padding: 10px 12px; text-align: left; color: {MUTED}; font-weight: 600;
}}
#navButton:hover {{ background: {CARD}; color: {TEXT}; }}
#navButton:checked {{ background: rgba(124, 92, 255, 0.18); color: {TEXT}; }}

QLineEdit, QComboBox {{
    background: {BACKGROUND}; border: 1px solid {BORDER}; border-radius: 10px;
    padding: 8px 12px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
    selection-background-color: {ACCENT}; padding: 4px;
}}
QCheckBox {{ spacing: 8px; color: {MUTED}; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border-radius: 5px;
    border: 1px solid {BORDER}; background: {BACKGROUND};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QProgressBar {{ background: {BACKGROUND}; border: none; border-radius: 4px; max-height: 8px; }}
QProgressBar::chunk {{
    border-radius: 4px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_ALT});
}}
QSlider::groove:horizontal {{ height: 6px; background: {BACKGROUND}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{
    border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_ALT});
}}
QSlider::handle:horizontal {{
    width: 18px; height: 18px; margin: -6px 0; border-radius: 9px;
    background: white; border: 3px solid {ACCENT};
}}

QListWidget, QTreeWidget, QPlainTextEdit {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 14px; padding: 6px;
}}
QListWidget::item, QTreeWidget::item {{ border-radius: 8px; padding: 6px; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {CARD_HOVER}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: rgba(124, 92, 255, 0.22); color: {TEXT};
}}
QHeaderView {{ background: transparent; border: none; }}
QTreeWidget QTableCornerButton::section {{ background: transparent; }}
QHeaderView::section {{
    background: transparent; color: {MUTED}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 8px 6px; font-weight: 600;
}}
QPlainTextEdit {{
    font-family: "Cascadia Mono", "Consolas", monospace; font-size: 12px;
    background: #090B10; padding: 12px;
}}
#terminalInput {{ font-family: "Cascadia Mono", "Consolas", monospace; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #2C3344; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #3B4459; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #2C3344; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent; border: none; width: 0; height: 0;
}}
QScrollArea {{ background: transparent; border: none; }}
#scrollBody {{ background: transparent; }}

#toast {{
    background: {CARD_HOVER}; border: 1px solid {BORDER}; border-radius: 12px;
    padding: 10px 18px; font-weight: 600;
}}
#toast[kind="error"] {{ border-color: {DANGER}; color: #FFB4B7; }}
#toast[kind="success"] {{ border-color: {SUCCESS}; color: #A7F3D0; }}
"""
