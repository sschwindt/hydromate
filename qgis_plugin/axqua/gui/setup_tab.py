"""The Setup tab: the gate everything else sits behind.

Until axqua can be found and a case is chosen, no other tab can do anything useful,
so this tab is first and the rest stay empty until it is satisfied (plan §17). The check
is deliberately at *configuration* time rather than at submit time: an executable that is
missing or wrong is a sentence here and an archaeology exercise later.

It is also where a ``.axqua-prj`` is opened, which is the whole rediscovery story
(plan §19): choose the project, and the jobs it knows about are found and shown with
their current state, whether they finished last night or are still running.
"""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGroupBox,
                                 QHBoxLayout, QLabel, QLineEdit, QListWidget,
                                 QPushButton, QVBoxLayout, QWidget)

from ..compat import MATCH_EXACTLY
from ..core import project as project_io
from ..core.runner_client import RunnerError
from ..core.tasks import run_async


class SetupTab(QWidget):
    def __init__(self, context, parent=None) -> None:
        super().__init__(parent)
        self.ctx = context
        self._build()
        self.refresh()

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        # -- axqua itself -----------------------------------------------------
        runner_box = QGroupBox("axqua")
        runner_form = QFormLayout(runner_box)
        self.runner_label = QLabel("not checked")
        self.runner_label.setWordWrap(True)
        runner_form.addRow("Executable", self.runner_label)
        row = QHBoxLayout()
        check = QPushButton("Check")
        check.clicked.connect(self.check_runner)
        row.addWidget(check)
        settings = QPushButton("Settings...")
        settings.clicked.connect(self.ctx.open_settings)
        row.addWidget(settings)
        row.addStretch(1)
        runner_form.addRow("", _wrap(row))
        layout.addWidget(runner_box)

        # -- the project ----------------------------------------------------------
        project_box = QGroupBox("Project")
        project_layout = QVBoxLayout(project_box)
        buttons = QHBoxLayout()
        for label, slot in (("Open project...", self.open_project),
                            ("Save project", self.save_project),
                            ("Save as...", self.save_project_as),
                            ("Add case...", self.add_case)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        project_layout.addLayout(buttons)

        self.project_label = QLabel("no project")
        self.project_label.setWordWrap(True)
        project_layout.addWidget(self.project_label)

        self.case_list = QListWidget()
        self.case_list.currentTextChanged.connect(self._case_selected)
        project_layout.addWidget(self.case_list)
        layout.addWidget(project_box)

        # -- how jobs are run -----------------------------------------------------
        run_box = QGroupBox("How jobs run on this machine")
        run_form = QFormLayout(run_box)
        self.profile_combo = QComboBox()
        self.profile_combo.currentTextChanged.connect(self._profile_changed)
        run_form.addRow("Solver profile", self.profile_combo)

        self.launcher_combo = QComboBox()
        self.launcher_combo.addItems(["auto", "systemd", "posix", "windows", "wsl"])
        self.launcher_combo.currentTextChanged.connect(self._launcher_changed)
        run_form.addRow("Launcher", self.launcher_combo)

        root_row = QHBoxLayout()
        self.job_root_edit = QLineEdit()
        self.job_root_edit.setPlaceholderText(
            "axqua's default (a large scratch volume is a good idea)")
        self.job_root_edit.editingFinished.connect(self._job_root_changed)
        root_row.addWidget(self.job_root_edit, 1)
        browse = QPushButton("...")
        browse.setMaximumWidth(36)
        browse.clicked.connect(self._browse_job_root)
        root_row.addWidget(browse)
        run_form.addRow("Job root", _wrap(root_row))
        layout.addWidget(run_box)

        self.capability_label = QLabel("")
        self.capability_label.setWordWrap(True)
        layout.addWidget(self.capability_label)
        layout.addStretch(1)

    # -- axqua ----------------------------------------------------------------
    def check_runner(self) -> None:
        try:
            info = self.ctx.client.validate()
        except RunnerError as exc:
            self.runner_label.setText(exc.user_text())
            self.ctx.warn(exc.user_text())
            self.ctx.set_runner_ok(False)
            return
        self.runner_label.setText(info.describe())
        self.ctx.set_runner_ok(True)
        self.load_profiles()

    def load_profiles(self) -> None:
        run_async("aXqua: reading solver profiles",
                  self.ctx.client.profiles,
                  on_success=self._profiles_arrived,
                  on_error=lambda exc: self.ctx.warn(str(exc)))

    def _profiles_arrived(self, profiles) -> None:
        current = self.ctx.project.profile
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        # An empty first entry, because a case that carries its own telemac.pysource
        # needs no profile at all - the profile system is an addition, not a new
        # prerequisite.
        self.profile_combo.addItem("(from the case config)")
        for name in sorted(profiles or {}):
            self.profile_combo.addItem(name)
        if current:
            index = self.profile_combo.findText(current)
            if index >= 0:
                self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)

    # -- the project --------------------------------------------------------------
    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open an aXqua project", "",
            # The pre-rename suffix is offered too: a project written before the rename
            # would otherwise simply not appear in the dialog.
            f"aXqua project (*{project_io.SUFFIX} *{project_io.LEGACY_SUFFIX})")
        if path:
            self.ctx.open_project(Path(path))

    def save_project(self) -> None:
        if self.ctx.project.path is None:
            self.save_project_as()
            return
        self.ctx.save_project()

    def save_project_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save the aXqua project", "",
            f"aXqua project (*{project_io.SUFFIX})")
        if path:
            self.ctx.save_project(Path(path))

    def add_case(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Add a case configuration", "", "aXqua case (*.yml *.yaml)")
        if path:
            self.ctx.add_case(Path(path))

    def _case_selected(self, text: str) -> None:
        if text:
            self.ctx.set_active_case(text)

    # -- run settings -------------------------------------------------------------
    def _profile_changed(self, text: str) -> None:
        self.ctx.project.profile = "" if text.startswith("(") else text

    def _launcher_changed(self, text: str) -> None:
        self.ctx.project.launcher = text

    def _job_root_changed(self) -> None:
        self.ctx.project.job_root = self.job_root_edit.text().strip()

    def _browse_job_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Where should jobs be written?")
        if path:
            self.job_root_edit.setText(path)
            self._job_root_changed()

    # -- rendering ----------------------------------------------------------------
    def refresh(self) -> None:
        project = self.ctx.project
        self.project_label.setText(
            str(project.path) if project.path else
            "no project file - open or save one to keep your cases and job root together")
        self.case_list.blockSignals(True)
        self.case_list.clear()
        for case in project.cases:
            self.case_list.addItem(case)
        if project.active_case:
            items = self.case_list.findItems(project.active_case, MATCH_EXACTLY)
            if items:
                self.case_list.setCurrentItem(items[0])
        self.case_list.blockSignals(False)
        self.job_root_edit.setText(project.job_root)
        index = self.launcher_combo.findText(project.launcher or "auto")
        if index >= 0:
            self.launcher_combo.setCurrentIndex(index)

    def show_capabilities(self, case_view) -> None:
        """Summarise what the chosen case can do, in one line per solver."""
        if case_view is None:
            self.capability_label.setText("")
            return
        lines = []
        for solver in case_view.solvers:
            if not solver.enabled:
                continue
            env = ("environment ok" if solver.env_ok
                   else "environment UNAVAILABLE" if solver.env_ok is False
                   else "environment not checked")
            available = [c.title for c in solver.visible_capabilities if c.can_submit]
            lines.append(f"<b>{solver.name}</b> - {env}. "
                         + (", ".join(available) if available
                            else "nothing configured yet"))
        self.capability_label.setText("<br>".join(lines))


def _wrap(layout) -> QWidget:
    widget = QWidget()
    widget.setLayout(layout)
    return widget
