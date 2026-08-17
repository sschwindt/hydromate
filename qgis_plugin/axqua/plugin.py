"""The plugin object QGIS loads, unloads and hands the interface to.

Deliberately thin. It creates a dock, a menu, a toolbar button and a Processing provider,
and removes all four again on unload. Everything else lives in :mod:`.gui` and
:mod:`.core`, so this file stays the kind of thing that can be read in one sitting when a
load failure has to be diagnosed.

**Unloading has to be complete.** A plugin reload leaves the previous instance's actions
and dock behind if they are not removed, and the second instance then races the first over
the same timer - which presents as a dashboard that refreshes twice, then four times, then
stops working. So every registration here has a matching removal.
"""

from __future__ import annotations

from qgis.PyQt.QtWidgets import QAction

from .compat import DOCK_RIGHT, icon

MENU = "&aXqua"


class AxquaPlugin:
    """The QGIS plugin interface: ``initGui`` and ``unload``, and nothing clever."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self.dock = None
        self.provider = None
        self._actions: list[QAction] = []

    # -- lifecycle ----------------------------------------------------------------
    def initGui(self) -> None:      # noqa: N802 - QGIS calls this name
        from .gui.dock import AxquaDock

        self.dock = AxquaDock(self.iface, self.iface.mainWindow())
        self.iface.addDockWidget(DOCK_RIGHT, self.dock)
        self.dock.hide()

        show = QAction(icon("axqua.svg"), "aXqua panel",
                       self.iface.mainWindow())
        show.setCheckable(True)
        show.triggered.connect(self._toggle)
        self.dock.visibilityChanged.connect(show.setChecked)
        self._register(show, toolbar=True)

        settings = QAction("Settings...", self.iface.mainWindow())
        settings.triggered.connect(lambda: self.dock.open_settings())
        self._register(settings)

        layout = QAction("Add the default A3 print layout", self.iface.mainWindow())
        layout.triggered.connect(self._add_layout)
        self._register(layout)

        self._add_provider()

    def unload(self) -> None:
        for action in self._actions:
            self.iface.removePluginMenu(MENU, action)
            self.iface.removeToolBarIcon(action)
        self._actions.clear()
        if self.dock is not None:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        self._remove_provider()

    # -- registration helpers -----------------------------------------------------
    def _register(self, action: QAction, *, toolbar: bool = False) -> None:
        self.iface.addPluginToMenu(MENU, action)
        if toolbar:
            self.iface.addToolBarIcon(action)
        self._actions.append(action)

    def _toggle(self, checked: bool) -> None:
        if self.dock is None:
            return
        self.dock.setVisible(checked)
        if checked:
            # Refreshing on show rather than on a timer means a hidden dock costs
            # nothing, which is half of the polling policy.
            self.dock.jobs_tab.refresh()

    # -- processing ---------------------------------------------------------------
    def _add_provider(self) -> None:
        try:
            from qgis.core import QgsApplication

            from .processing.provider import AxquaProvider
            self.provider = AxquaProvider()
            QgsApplication.processingRegistry().addProvider(self.provider)
        except Exception as exc:  # noqa: BLE001
            # Processing is a bonus, not the feature. A QGIS build without it, or a
            # provider that fails to construct, must not stop the dock from loading.
            self.provider = None
            self.iface.messageBar().pushMessage(
                "aXqua", f"Processing algorithms unavailable: {exc}", duration=5)

    def _remove_provider(self) -> None:
        if self.provider is None:
            return
        try:
            from qgis.core import QgsApplication
            QgsApplication.processingRegistry().removeProvider(self.provider)
        except Exception:  # noqa: BLE001 - pragma: no cover
            pass
        self.provider = None

    # -- the print layout ---------------------------------------------------------
    def _add_layout(self) -> None:
        from .gui.layouts import add_default_layout
        try:
            name = add_default_layout(self.iface)
        except Exception as exc:  # noqa: BLE001
            self.iface.messageBar().pushMessage(
                "aXqua", f"Could not create the layout: {exc}", duration=8)
            return
        self.iface.messageBar().pushMessage(
            "aXqua", f"Added the print layout {name!r}. Open it from "
            "Project > Layouts.", duration=8)
