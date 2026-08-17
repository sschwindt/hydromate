"""The job dashboard: a table, a timer, and the six actions.

The table is the plan's (§18), with one column added and one removed. ``Best objective``
stays because a calibration is what it was designed around, but ``Iteration`` is replaced
by a single **Progress** column that reads correctly for every job kind - a steady run's
percentage of simulated time, a study's level count, a calibration's iteration. That is
the visible consequence of typing progress per kind rather than flattening it: one column
that is always meaningful instead of several that are usually empty.

The timer follows :mod:`..core.job_model`'s policy exactly, and it is genuinely adaptive:
it stops when nothing is running, slows to 30 s when the dock is hidden, and even when it
fires it usually does no more than ``stat`` a file.
"""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel,
                                 QMessageBox, QPushButton, QTableWidget,
                                 QTableWidgetItem, QVBoxLayout, QWidget)

from ..compat import USER_ROLE, color, open_in_file_manager
from ..core.job_model import JobTable
from ..core.tasks import run_async

COLUMNS = ["Job ID", "Case", "Solver", "Kind", "State", "Progress", "Best objective"]

#: Colours chosen to survive both QGIS themes - a light wash rather than a saturated fill,
#: so the text stays readable on a dark palette too.
STATE_COLOURS = {
    "RUNNING": "#d7ecff", "STARTING": "#d7ecff", "POSTPROCESSING": "#e6dcff",
    "COMPLETED": "#dcf3dc", "FAILED": "#ffdcdc", "CANCELLED": "#ececec",
    "CANCEL_REQUESTED": "#ffeccc", "QUEUED": "#f5f5f5",
}


class JobsWidget(QWidget):
    """The dashboard. Owns no solver, blocks on nothing."""

    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)
        self.ctx = context                 # PluginContext: client, project, messages
        self.table_model = JobTable()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._build()

    # -- construction -------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(COLUMNS)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        self.status_line = QLabel("")
        self.status_line.setWordWrap(True)
        layout.addWidget(self.status_line)

        buttons = QHBoxLayout()
        self.actions = {}
        for key, label, slot, needs_selection in (
            ("refresh", "Refresh", self.refresh, False),
            ("cancel", "Cancel", self._cancel, True),
            ("logs", "View logs", self._view_logs, True),
            ("open", "Open job directory", self._open_directory, True),
            ("load", "Load results", self._load_results, True),
        ):
            button = QPushButton(label, self)
            button.clicked.connect(slot)
            if needs_selection:
                button.setEnabled(False)
            buttons.addWidget(button)
            self.actions[key] = button
        buttons.addStretch(1)
        layout.addLayout(buttons)

    # -- data ---------------------------------------------------------------------
    def refresh(self) -> None:
        """Ask axqua for the job list, off the GUI thread."""
        client = self.ctx.client_or_warn()
        if client is None:
            return
        job_root = self.ctx.project.job_root or None
        case = None
        run_async("aXqua: listing jobs",
                  lambda: client.list_jobs(job_root=job_root, case=case),
                  on_success=self._rows_arrived,
                  on_error=lambda exc: self.ctx.warn(str(exc)))

    def _rows_arrived(self, rows) -> None:
        self.table_model.replace(rows or [])
        # Seed each row from its own status.json so the progress column is populated
        # immediately rather than only after the first timer tick.
        self.table_model.poll()
        self._repaint()
        self._reschedule()

    def _repaint(self) -> None:
        selected = self.selected_job_id()
        self.table.setRowCount(len(self.table_model))
        for row, job in enumerate(self.table_model):
            values = [job.job_id, job.case_name, job.solver, job.kind, job.state,
                      job.progress_text, job.objective_text]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(USER_ROLE, job.job_id)
                if col == 4:
                    tint = STATE_COLOURS.get(job.state)
                    if tint:
                        item.setBackground(color(tint))
                if job.error_text:
                    item.setToolTip(job.error_text)
                self.table.setItem(row, col, item)
        if selected:
            self.select(selected)
        self._selection_changed()

    # -- polling ------------------------------------------------------------------
    def _tick(self) -> None:
        changed = self.table_model.poll(self._visible_job_ids())
        if changed:
            self._repaint()
        self._reschedule()

    def _reschedule(self) -> None:
        """Set the next interval, or stop.

        Stopping is the important half: with no running job there is nothing that can
        change, and a timer that keeps firing over a scratch volume is a cost the user
        pays for nothing.
        """
        interval = self.table_model.interval_ms(visible=self.isVisible())
        if interval <= 0:
            self._timer.stop()
            return
        if self._timer.interval() != interval or not self._timer.isActive():
            self._timer.start(interval)

    def _visible_job_ids(self) -> list[str]:
        """Only the rows actually on screen - the rest cannot be looked at anyway."""
        ids = []
        viewport = self.table.viewport().rect()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            rect = self.table.visualItemRect(item)
            if rect.intersects(viewport):
                ids.append(item.text())
        return ids or [j.job_id for j in self.table_model]

    def showEvent(self, event):          # noqa: N802 - Qt naming
        super().showEvent(event)
        self._reschedule()

    def hideEvent(self, event):          # noqa: N802 - Qt naming
        super().hideEvent(event)
        self._reschedule()

    # -- selection ----------------------------------------------------------------
    def selected_job_id(self) -> str | None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        if not rows:
            return None
        item = self.table.item(sorted(rows)[0], 0)
        return item.text() if item else None

    def select(self, job_id: str) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.text() == job_id:
                self.table.selectRow(row)
                return

    def _selection_changed(self) -> None:
        job = self.table_model.get(self.selected_job_id() or "")
        for key in ("cancel", "logs", "open", "load"):
            self.actions[key].setEnabled(job is not None)
        if job is None:
            self.status_line.setText("")
            return
        # Cancelling something already finished is meaningless, and offering it invites
        # the user to wonder whether it did something.
        self.actions["cancel"].setEnabled(not job.is_terminal)
        self.actions["load"].setEnabled(job.state == "COMPLETED"
                                        or (job.root / "results").is_dir())
        self.status_line.setText(job.error_text or job.phase or "")

    def _selected_or_warn(self):
        job = self.table_model.get(self.selected_job_id() or "")
        if job is None:
            self.ctx.warn("Select a job first.")
        return job

    # -- actions ------------------------------------------------------------------
    def _cancel(self) -> None:
        job = self._selected_or_warn()
        if job is None:
            return
        confirm = QMessageBox.question(
            self, "Cancel job",
            f"Stop {job.job_id} and everything it started?\n\n"
            "The solver and its MPI processes are terminated as a group. Work already "
            "written to disk is kept.")
        if confirm != QMessageBox.StandardButton.Yes:
            return
        client = self.ctx.client_or_warn()
        if client is None:
            return
        job_root = self.ctx.project.job_root or None
        run_async(f"aXqua: cancelling {job.job_id}",
                  lambda: client.cancel(job.job_id, job_root=job_root),
                  on_success=lambda data: self._cancelled(data),
                  on_error=lambda exc: self.ctx.warn(str(exc)))

    def _cancelled(self, data) -> None:
        self.ctx.info(f"Job is now {(data or {}).get('state', 'cancelled')}.")
        self.refresh()

    def _view_logs(self) -> None:
        job = self._selected_or_warn()
        if job is None:
            return
        from .log_dialog import LogDialog
        LogDialog(job, self.ctx, self).show_it()

    def _open_directory(self) -> None:
        job = self._selected_or_warn()
        if job is None:
            return
        if not open_in_file_manager(job.root):
            self.ctx.warn(f"Could not open {job.root}")

    def _load_results(self) -> None:
        job = self._selected_or_warn()
        if job is None:
            return
        from ..core.result_loader import ResultLoader, discover
        results = discover(job.root)
        if not results.layers:
            self.ctx.warn(
                f"{job.job_id} has produced nothing QGIS can open yet."
                + ("" if results.from_manifest else
                   " (No result manifest either, so this was a glob of the job folder.)"))
            return
        loader = ResultLoader(min_depth=self.ctx.min_depth,
                              velocity_cap=self.ctx.velocity_cap)
        added = loader.load(results)
        for warning in loader.warnings:
            self.ctx.warn(warning)
        self.ctx.info(f"Loaded {len(added)} layer(s) into axqua/{job.job_id}.")


def job_directory(job) -> Path:
    return Path(job.root)
