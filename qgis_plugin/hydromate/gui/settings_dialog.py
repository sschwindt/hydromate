"""Plugin settings: where hydromate is, and the two styling defaults.

Deliberately small. Everything about the *model* belongs in ``case-config.yml`` and
everything about the *machine* belongs in hydromate's own ``profiles.yml``; duplicating
either here would create a second place to look when something disagrees.

What is left is genuinely the plugin's own: which executable to talk to, and the two
rendering thresholds a user may reasonably want to override per install.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QPushButton, QVBoxLayout, QWidget)

from ..core import runner_client
from ..core.runner_client import RunnerClient, RunnerError

KEY_EXECUTABLE = runner_client.SETTINGS_KEY
KEY_MIN_DEPTH = "hydromate/min_depth"
KEY_VELOCITY_CAP = "hydromate/velocity_cap"


def read_settings() -> dict:
    settings = QSettings()
    return {
        "executable": str(settings.value(KEY_EXECUTABLE, "") or ""),
        "min_depth": float(settings.value(KEY_MIN_DEPTH, 0.01) or 0.01),
        "velocity_cap": float(settings.value(KEY_VELOCITY_CAP, 5.0) or 5.0),
    }


def write_settings(values: dict) -> None:
    settings = QSettings()
    settings.setValue(KEY_EXECUTABLE, values.get("executable", ""))
    settings.setValue(KEY_MIN_DEPTH, values.get("min_depth", 0.01))
    settings.setValue(KEY_VELOCITY_CAP, values.get("velocity_cap", 5.0))


class SettingsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("HydroMate settings")
        self.resize(620, 260)
        values = read_settings()

        layout = QVBoxLayout(self)
        form = QFormLayout()

        row = QHBoxLayout()
        self.executable = QLineEdit(values["executable"])
        self.executable.setPlaceholderText(
            "leave empty to use $HYDROMATE_EXE, then PATH")
        row.addWidget(self.executable, 1)
        browse = QPushButton("...")
        browse.setMaximumWidth(36)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        test = QPushButton("Test")
        test.clicked.connect(self._test)
        row.addWidget(test)
        form.addRow("hydromate executable", _wrap(row))

        self.result = QLabel("")
        self.result.setWordWrap(True)
        form.addRow("", self.result)

        self.min_depth = QDoubleSpinBox()
        self.min_depth.setDecimals(3)
        self.min_depth.setRange(0.0, 10.0)
        self.min_depth.setSingleStep(0.005)
        self.min_depth.setSuffix(" m")
        self.min_depth.setValue(values["min_depth"])
        self.min_depth.setToolTip(
            "Water shallower than this renders transparent. On a bed with a Nikuradse "
            "roughness of 0.05-0.5 m, water 5 mm deep stands inside the grain roughness "
            "rather than flowing over it, so drawing it as river overstates the wetted "
            "extent. hydromate's own reports use the same threshold.")
        form.addRow("Minimum water depth", self.min_depth)

        self.velocity_cap = QDoubleSpinBox()
        self.velocity_cap.setDecimals(1)
        self.velocity_cap.setRange(0.5, 100.0)
        self.velocity_cap.setSuffix(" m/s")
        self.velocity_cap.setValue(values["velocity_cap"])
        self.velocity_cap.setToolTip(
            "Upper end of the velocity colour scale. Above about 5 m/s a depth-averaged "
            "river result is usually a wetting/drying artefact in a nearly-dry cell, and "
            "letting one such node set the scale flattens the whole map.")
        form.addRow("Velocity scale maximum", self.velocity_cap)

        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Where is hydromate?")
        if path:
            self.executable.setText(path)

    def _test(self) -> None:
        """Validate now, so a wrong executable is never discovered at submit time."""
        try:
            info = RunnerClient(self.executable.text().strip() or None).validate()
        except RunnerError as exc:
            self.result.setText(exc.user_text())
            return
        self.result.setText(info.describe())

    def values(self) -> dict:
        return {
            "executable": self.executable.text().strip(),
            "min_depth": self.min_depth.value(),
            "velocity_cap": self.velocity_cap.value(),
        }

    def accept(self) -> None:      # noqa: D102 - Qt override
        write_settings(self.values())
        super().accept()


def _wrap(layout) -> QWidget:
    widget = QWidget()
    widget.setLayout(layout)
    return widget
