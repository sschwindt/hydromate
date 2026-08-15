"""Showing a job's log, with the two streams kept apart.

hydromate writes two different things (plan §23) and conflating them in one window would
undo that: ``runner.log`` is hydromate's narration - state transitions, the commands it
issued, exit codes - and is where a failure is explained; the solver's listing is the
solver talking, and is where a *numerical* problem is diagnosed. The toggle is the whole
interface.

Only the tail is read. A multi-day interFoam listing runs to hundreds of megabytes, and
loading one into a text widget would freeze QGIS - which is exactly the "do not load
complete result datasets into plugin memory" rule (plan §26) applied to a log file.
"""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QFont
from qgis.PyQt.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                                 QPushButton, QVBoxLayout)

from ..compat import open_in_file_manager

#: Read at most this much from the end of the file. Generous for a human, trivial for a
#: text widget.
TAIL_BYTES = 256 * 1024
FOLLOW_INTERVAL_MS = 2000


class LogDialog(QDialog):
    def __init__(self, job, context, parent=None) -> None:
        super().__init__(parent)
        self.job = job
        self.ctx = context
        self.setWindowTitle(f"HydroMate - {job.job_id}")
        self.resize(900, 600)

        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.solver_toggle = QCheckBox("Solver listing (instead of hydromate's log)")
        self.solver_toggle.toggled.connect(self._reload)
        controls.addWidget(self.solver_toggle)
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(not job.is_terminal)
        self.follow.toggled.connect(self._follow_changed)
        controls.addWidget(self.follow)
        controls.addStretch(1)
        open_button = QPushButton("Open folder")
        open_button.clicked.connect(lambda: open_in_file_manager(self.job.root))
        controls.addWidget(open_button)
        layout.addLayout(controls)

        self.path_label = QLabel("")
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)

        self.view = QPlainTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFont("Monospace")
        font.setStyleHint(QFont.StyleHint.TypeWriter)
        self.view.setFont(font)
        layout.addWidget(self.view, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._reload)

    def show_it(self) -> None:
        self._reload()
        self._follow_changed(self.follow.isChecked())
        self.show()

    # -- content ------------------------------------------------------------------
    def _path(self) -> Path | None:
        if self.solver_toggle.isChecked():
            folder = self.job.root / "solver"
            if not folder.is_dir():
                return None
            # The most recently touched: the one a user asking "what is it doing now"
            # means.
            logs = sorted(folder.glob("*.log"), key=lambda p: p.stat().st_mtime,
                          reverse=True)
            return logs[0] if logs else None
        runner = self.job.root / "runner.log"
        return runner if runner.exists() else None

    def _reload(self) -> None:
        path = self._path()
        if path is None:
            self.path_label.setText("nothing written yet")
            self.view.setPlainText("")
            return
        self.path_label.setText(str(path))
        text, truncated = _tail(path, TAIL_BYTES)
        at_bottom = (self.view.verticalScrollBar().value()
                     >= self.view.verticalScrollBar().maximum() - 4)
        prefix = (f"[showing the last {TAIL_BYTES // 1024} KiB of "
                  f"{path.stat().st_size / 1024 / 1024:.1f} MiB]\n\n") if truncated else ""
        self.view.setPlainText(prefix + text)
        if at_bottom:
            # Only auto-scroll if the user was already at the end; yanking the view back
            # while they are reading something further up is infuriating.
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _follow_changed(self, on: bool) -> None:
        if on:
            self._timer.start(FOLLOW_INTERVAL_MS)
        else:
            self._timer.stop()

    def closeEvent(self, event):        # noqa: N802 - Qt naming
        self._timer.stop()
        super().closeEvent(event)


def _tail(path: Path, limit: int) -> tuple[str, bool]:
    """The last *limit* bytes, decoded forgivingly."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                # The seek almost certainly landed mid-line; drop the partial one rather
                # than showing a fragment.
                fh.readline()
                return fh.read().decode("utf-8", "replace"), True
            return fh.read().decode("utf-8", "replace"), False
    except OSError as exc:
        return f"could not read {path}: {exc}", False
