"""SIM selection shared by the calls, messages and contacts pages."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QComboBox

from ..core.adb import Adb
from ..core.telephony import DEFAULT_SUBSCRIPTION, Sim, SimState, read_sims
from .async_runner import run_async

SWATCH_SIZE = 12
LAST_SIM_KEY = "telephony/last_sim_slot"
PHONE_DEFAULT_TEXT = "Telefon seçsin"


def sim_swatch(color: str) -> QIcon:
    """Small round color chip matching the SIM's color on the phone."""
    pixmap = QPixmap(SWATCH_SIZE, SWATCH_SIZE)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QColor(0, 0, 0, 0))
    painter.setBrush(QColor(color))
    painter.drawEllipse(0, 0, SWATCH_SIZE, SWATCH_SIZE)
    painter.end()
    return QIcon(pixmap)


class SimPicker(QComboBox):
    """Lists the inserted SIMs plus "let the phone decide"; hidden on single-SIM phones.

    The last choice is remembered by slot, so it survives app restarts.
    """

    def __init__(self) -> None:
        super().__init__()
        self._state = SimState()
        self.setToolTip("Arama ve SMS için kullanılacak SIM kart")
        self.setMinimumWidth(170)
        self.activated.connect(self._remember)
        self._fill()

    @property
    def state(self) -> SimState:
        return self._state

    def load(self, adb: Adb, serial: str) -> None:
        """Read the SIMs in the background; failures leave the phone-default entry."""
        run_async(lambda: read_sims(adb, serial), self.set_state)

    def set_state(self, state: SimState) -> None:
        self._state = state
        self._fill()

    def _fill(self) -> None:
        self.clear()
        for sim in self._state.sims:
            self.addItem(sim_swatch(sim.color), sim.label, sim.sub_id)
        self.addItem(PHONE_DEFAULT_TEXT, DEFAULT_SUBSCRIPTION)
        self.setVisible(len(self._state.sims) > 1)
        saved_slot = int(QSettings().value(LAST_SIM_KEY, -1))
        for index, sim in enumerate(self._state.sims):
            if sim.slot == saved_slot:
                self.setCurrentIndex(index)
                return
        self.setCurrentIndex(0)

    def _remember(self, index: int) -> None:
        sim = self._state.by_id(self.itemData(index))
        QSettings().setValue(LAST_SIM_KEY, sim.slot if sim else -1)

    def sub_id(self) -> int:
        """Chosen subscription id, or `DEFAULT_SUBSCRIPTION`."""
        value = self.currentData()
        return DEFAULT_SUBSCRIPTION if value is None else int(value)

    def sms_sub_id(self) -> int:
        """Subscription for SMS: an explicit choice, else the phone's default SMS SIM."""
        chosen = self.sub_id()
        if chosen != DEFAULT_SUBSCRIPTION:
            return chosen
        if self._state.default_sms != DEFAULT_SUBSCRIPTION:
            return self._state.default_sms
        return self._state.sims[0].sub_id if self._state.sims else DEFAULT_SUBSCRIPTION

    def current_sim(self) -> Sim | None:
        return self._state.by_id(self.sub_id())
