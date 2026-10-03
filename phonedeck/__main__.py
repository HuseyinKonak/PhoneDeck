"""Entry point: `python -m phonedeck`."""

from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from .core.adb import Adb, AdbError
from .ui import theme
from .ui.main_window import APP_NAME, MainWindow


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setOrganizationName(APP_NAME)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(theme.STYLESHEET)
    try:
        adb = Adb()
    except AdbError as exc:
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1
    window = MainWindow(adb)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
