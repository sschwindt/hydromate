"""The dock: Setup, the capability tabs, and the job dashboard.

The tab set is rebuilt whenever the case changes, from ``axqua case-status --json``.
That is the design point (plan §17): the plugin does not know what TELEMAC or OpenFOAM can
do, it *asks*, so a capability added to axqua appears here without a plugin release.

The dock also carries the :class:`PluginContext` - the small object every widget uses to
reach the runner client, the project and the message bar. Passing one context beats
threading four constructor arguments through every widget, and it keeps the widgets free
of solver knowledge (plan §31).
"""

from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtWidgets import (QDockWidget, QLabel, QTabWidget, QVBoxLayout, QWidget)

from ..compat import CRITICAL, INFO, WARNING, exec_dialog
from ..core import project as project_io
from ..core.runner_client import RunnerClient, RunnerError
from .capability_tab_widget import CapabilityTab
from .capability_tabs import CaseView
from .jobs_widget import JobsWidget
from .settings_dialog import SettingsDialog, read_settings
from .setup_tab import SetupTab


class PluginContext:
    """What every widget needs, in one place."""

    def __init__(self, iface, dock) -> None:
        self.iface = iface
        self.dock = dock
        settings = read_settings()
        self.client = RunnerClient(settings["executable"] or None)
        self.min_depth = settings["min_depth"]
        self.velocity_cap = settings["velocity_cap"]
        self.project = project_io.AxquaProject()
        self.case_view: CaseView | None = None
        self.runner_ok = False

    # -- messaging ----------------------------------------------------------------
    def _message(self, text: str, level) -> None:
        self.iface.messageBar().pushMessage("aXqua", text, level=level, duration=8)

    def info(self, text: str) -> None:
        self._message(text, INFO)

    def warn(self, text: str) -> None:
        self._message(text, WARNING)

    def error(self, text: str) -> None:
        self._message(text, CRITICAL)

    # -- the runner ---------------------------------------------------------------
    def set_runner_ok(self, ok: bool) -> None:
        self.runner_ok = ok

    def client_or_warn(self) -> RunnerClient | None:
        """The client, or a message saying exactly what to fix.

        Every action goes through this, so "axqua is not installed" is reported once,
        clearly, rather than as a different traceback per button.
        """
        try:
            self.client.info
        except RunnerError as exc:
            self.error(exc.user_text())
            return None
        return self.client

    def active_case_or_warn(self) -> Path | None:
        case = self.project.active_case_path()
        if case is None:
            self.warn("Add a case configuration on the Setup tab first.")
            return None
        if not case.exists():
            self.warn(f"{case} is listed in the project but is not on disk.")
            return None
        return case

    # -- delegated to the dock ----------------------------------------------------
    def open_settings(self) -> None:
        self.dock.open_settings()

    def open_project(self, path: Path) -> None:
        self.dock.open_project(path)

    def save_project(self, path: Path | None = None) -> None:
        self.dock.save_project(path)

    def add_case(self, path: Path) -> None:
        self.dock.add_case(path)

    def set_active_case(self, stored: str) -> None:
        self.dock.set_active_case(stored)

    def job_submitted(self, job_id: str) -> None:
        self.dock.job_submitted(job_id)

    def load_latest_results(self, kind: str) -> None:
        self.dock.load_latest_results(kind)


class AxquaDock(QDockWidget):
    """The plugin's main window."""

    def __init__(self, iface, parent=None) -> None:
        super().__init__("aXqua", parent)
        self.iface = iface
        self.setObjectName("AxquaDock")
        self.ctx = PluginContext(iface, self)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(2, 2, 2, 2)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.setWidget(container)

        self.setup_tab = SetupTab(self.ctx)
        self.tabs.addTab(self.setup_tab, "Setup")

        self.jobs_tab = JobsWidget(self.ctx)
        self.tabs.addTab(self.jobs_tab, "Jobs")

        self._capability_tabs: dict[str, CapabilityTab] = {}
        self._placeholder = QLabel(
            "Choose a case on the Setup tab. The tabs available here are generated from "
            "what axqua reports this case can actually do.")
        self._placeholder.setWordWrap(True)

        self.setup_tab.check_runner()

    # -- project ------------------------------------------------------------------
    def open_project(self, path: Path) -> None:
        try:
            self.ctx.project = project_io.load(path)
        except (OSError, ValueError) as exc:
            self.ctx.error(f"Could not open {path.name}: {exc}")
            return
        self.setup_tab.refresh()
        self.ctx.info(f"Opened {path.name} with {len(self.ctx.project.cases)} case(s).")
        # Rediscovery (plan §19): the project names the job root, and the jobs under it
        # are found with whatever state they have reached while QGIS was closed.
        self.refresh_case()
        self.jobs_tab.refresh()

    def save_project(self, path: Path | None = None) -> None:
        try:
            written = project_io.save(self.ctx.project, path)
        except (OSError, ValueError) as exc:
            self.ctx.error(f"Could not save the project: {exc}")
            return
        self.setup_tab.refresh()
        self.ctx.info(f"Saved {written.name}.")

    def add_case(self, path: Path) -> None:
        self.ctx.project.add_case(path)
        self.setup_tab.refresh()
        self.refresh_case()

    def set_active_case(self, stored: str) -> None:
        self.ctx.project.active_case = stored
        self.refresh_case()

    # -- capabilities -------------------------------------------------------------
    def refresh_case(self) -> None:
        """Re-read the capability matrix and rebuild the tabs from it."""
        case = self.ctx.project.active_case_path()
        client = self.ctx.client_or_warn()
        if case is None or client is None:
            self._apply_case_view(None)
            return
        from ..core.tasks import run_async
        run_async("aXqua: reading what this case can do",
                  lambda: client.case_status(case),
                  on_success=lambda payload: self._apply_case_view(
                      CaseView.from_payload(payload or {})),
                  on_error=lambda exc: self.ctx.error(str(exc)))

    def _apply_case_view(self, view: CaseView | None) -> None:
        self.ctx.case_view = view
        self.setup_tab.show_capabilities(view)
        self._rebuild_tabs(view)

    def _rebuild_tabs(self, view: CaseView | None) -> None:
        # Keep Setup and Jobs; everything between them is generated.
        while self.tabs.count() > 1:
            if self.tabs.widget(self.tabs.count() - 1) is self.jobs_tab:
                break
            self.tabs.removeTab(self.tabs.count() - 1)
        for index in reversed(range(self.tabs.count())):
            widget = self.tabs.widget(index)
            if widget not in (self.setup_tab, self.jobs_tab):
                self.tabs.removeTab(index)
        self._capability_tabs = {}

        if view is None:
            return
        for solver in view.enabled_solvers:
            for capability in solver.visible_capabilities:
                key = f"{solver.name}:{capability.name}"
                tab = CapabilityTab(capability, self.ctx)
                # Shown but disabled where axqua has not implemented it - a gap in
                # axqua is worth seeing, and is different from a category error,
                # which is hidden entirely.
                tab.setEnabled(capability.enabled)
                label = capability.title
                if len(view.enabled_solvers) > 1:
                    label = f"{capability.title} ({solver.name})"
                self.tabs.insertTab(self.tabs.count() - 1, tab, label)
                if not capability.enabled:
                    self.tabs.setTabToolTip(self.tabs.count() - 2, capability.reason)
                self._capability_tabs[key] = tab

    # -- actions ------------------------------------------------------------------
    def open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if exec_dialog(dialog):
            values = dialog.values()
            self.ctx.client = RunnerClient(values["executable"] or None)
            self.ctx.min_depth = values["min_depth"]
            self.ctx.velocity_cap = values["velocity_cap"]
            self.setup_tab.check_runner()

    def job_submitted(self, job_id: str) -> None:
        self.jobs_tab.refresh()
        self.tabs.setCurrentWidget(self.jobs_tab)

    def load_latest_results(self, kind: str) -> None:
        """Load the newest completed job of *kind* for the active case."""
        jobs = [j for j in self.jobs_tab.table_model
                if j.state == "COMPLETED" and (not kind or j.kind == kind)]
        if not jobs:
            self.ctx.warn("No completed job of that kind yet. Refresh the Jobs tab if "
                          "one finished while this was open.")
            return
        newest = max(jobs, key=lambda j: j.finished or j.updated or "")
        self.jobs_tab.select(newest.job_id)
        self.jobs_tab._load_results()
