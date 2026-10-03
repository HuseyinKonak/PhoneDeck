"""Run blocking device calls off the UI thread."""

from __future__ import annotations

import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot

logger = logging.getLogger(__name__)
_active_tasks: set["_Task"] = set()


class _Relay(QObject):
    """Lives on the UI thread and delivers results there."""

    finished = Signal(object, object)

    def __init__(self, task: "_Task") -> None:
        super().__init__()
        self._task = task
        self.finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    @Slot(object, object)
    def _deliver(self, result: Any, error: Any) -> None:
        _active_tasks.discard(self._task)
        callback = self._task.on_error if error is not None else self._task.on_done
        if callback is None:
            return
        try:
            callback(str(error) if error is not None else result)
        except RuntimeError as exc:
            # The widget behind the callback was destroyed while the task ran.
            logger.debug("callback target gone: %s", exc)


class _Task(QRunnable):
    def __init__(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None] | None,
        on_error: Callable[[str], None] | None,
    ) -> None:
        super().__init__()
        self._work = work
        self.on_done = on_done
        self.on_error = on_error
        self._relay = _Relay(self)

    def run(self) -> None:
        try:
            result, error = self._work(), None
        except Exception as exc:  # noqa: BLE001 - every failure must reach the UI
            logger.warning("background task failed: %s", exc)
            result, error = None, exc
        try:
            self._relay.finished.emit(result, error)
        except RuntimeError as exc:
            # The application is shutting down and the relay is already gone.
            logger.debug("result dropped at shutdown: %s", exc)


def run_async(
    work: Callable[[], Any],
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """Run `work` on the thread pool; callbacks fire on the UI thread."""
    task = _Task(work, on_done, on_error)
    task.setAutoDelete(False)
    _active_tasks.add(task)
    QThreadPool.globalInstance().start(task)
