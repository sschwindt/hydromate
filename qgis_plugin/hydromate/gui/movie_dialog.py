"""Exporting an unsteady result as a movie.

Renders the *currently visualised* mesh variable, one frame per timestep, then encodes.
Rendering what is on screen rather than offering a variable picker is the point: the user
has already chosen the variable, the colour scale and the extent, and a second set of
controls here would be a second place for those to be wrong.

**Format.** WebM/VP9 - the most storage-efficient of the formats a browser and every
modern player can open without a codec hunt. A 300-frame depth animation lands in a few
megabytes where the equivalent animated GIF is tens.

**ffmpeg is optional.** If it is not on ``PATH`` the frames are kept and the dialog says
where they are, with the exact command to run later. Deleting a user's frames because the
encoder is missing would throw away the expensive half of the work.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from qgis.core import (QgsMapRendererParallelJob, QgsMapSettings, QgsMeshLayer,
                       QgsProject)
from qgis.PyQt.QtCore import QEventLoop, QSize
from qgis.PyQt.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                                 QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                                 QProgressBar, QPushButton, QSpinBox, QVBoxLayout,
                                 QWidget)

DEFAULT_FPS = 8
DEFAULT_WIDTH = 1920


class MovieDialog(QDialog):
    """Frame-by-frame export of a mesh layer's dataset over time."""

    def __init__(self, layer: QgsMeshLayer, iface, parent=None) -> None:
        super().__init__(parent)
        self.layer = layer
        self.iface = iface
        self.setWindowTitle("HydroMate - export a movie")
        self.resize(560, 300)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.dataset = QComboBox()
        for index in layer.datasetGroupsIndexes():
            meta = layer.datasetGroupMetadata(index)
            self.dataset.addItem((meta.name() or f"dataset {index}").strip(), index)
        active = layer.rendererSettings().activeScalarDatasetGroup()
        position = self.dataset.findData(active)
        if position >= 0:
            self.dataset.setCurrentIndex(position)
        form.addRow("Variable", self.dataset)

        self.fps = QSpinBox()
        self.fps.setRange(1, 60)
        self.fps.setValue(DEFAULT_FPS)
        self.fps.setSuffix(" frames/s")
        form.addRow("Frame rate", self.fps)

        self.width = QSpinBox()
        self.width.setRange(320, 7680)
        self.width.setSingleStep(160)
        self.width.setValue(DEFAULT_WIDTH)
        self.width.setSuffix(" px")
        form.addRow("Width", self.width)

        row = QHBoxLayout()
        self.output = QLineEdit(str(Path.home() / "hydromate-animation.webm"))
        row.addWidget(self.output, 1)
        browse = QPushButton("...")
        browse.setMaximumWidth(36)
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        form.addRow("Output", _wrap(row))
        layout.addLayout(form)

        self.note = QLabel(self._encoder_note())
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._export)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # -- helpers ------------------------------------------------------------------
    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save the animation", self.output.text(),
                                              "WebM video (*.webm)")
        if path:
            self.output.setText(path)

    @staticmethod
    def _encoder_note() -> str:
        if shutil.which("ffmpeg"):
            return "ffmpeg found: the frames will be encoded to WebM/VP9 and removed."
        return ("ffmpeg was not found on PATH. The PNG frames will be written and kept, "
                "with the command to encode them shown when the export finishes.")

    def _timesteps(self, index: int) -> int:
        return self.layer.datasetCount(index) if hasattr(self.layer, "datasetCount") \
            else self.layer.datasetGroupMetadata(index).datasetCount()

    # -- the work -----------------------------------------------------------------
    def _export(self) -> None:
        index = self.dataset.currentData()
        if index is None:
            return
        try:
            count = int(self._timesteps(index))
        except Exception:  # noqa: BLE001
            count = 0
        if count <= 1:
            self.note.setText("This dataset has a single timestep, so there is nothing "
                              "to animate.")
            return

        output = Path(self.output.text()).expanduser()
        frames_dir = output.with_suffix("") .parent / f"{output.stem}-frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        self.progress.setVisible(True)
        self.progress.setRange(0, count)
        settings = self._map_settings()
        renderer = self.layer.rendererSettings()

        for step in range(count):
            from qgis.core import QgsMeshDatasetIndex
            renderer.setActiveScalarDatasetGroup(index)
            self.layer.setRendererSettings(renderer)
            self.layer.setStaticScalarDatasetIndex(QgsMeshDatasetIndex(index, step))
            self._render(settings, frames_dir / f"frame-{step:05d}.png")
            self.progress.setValue(step + 1)

        self._encode(frames_dir, output, count)

    def _map_settings(self) -> QgsMapSettings:
        canvas = self.iface.mapCanvas()
        settings = QgsMapSettings()
        settings.setLayers(canvas.layers())
        settings.setExtent(canvas.extent())
        settings.setDestinationCrs(QgsProject.instance().crs())
        width = int(self.width.value())
        aspect = canvas.extent().height() / max(canvas.extent().width(), 1e-9)
        # An even height, because VP9 refuses odd dimensions - a failure that would
        # otherwise appear only at the very end, after every frame had been rendered.
        height = max(2, int(round(width * aspect)) // 2 * 2)
        settings.setOutputSize(QSize(width, height))
        settings.setBackgroundColor(canvas.canvasColor())
        return settings

    @staticmethod
    def _render(settings: QgsMapSettings, path: Path) -> None:
        job = QgsMapRendererParallelJob(settings)
        loop = QEventLoop()
        job.finished.connect(loop.quit)
        job.start()
        loop.exec() if hasattr(loop, "exec") else loop.exec_()
        job.renderedImage().save(str(path))

    def _encode(self, frames_dir: Path, output: Path, count: int) -> None:
        pattern = str(frames_dir / "frame-%05d.png")
        command = ["ffmpeg", "-y", "-framerate", str(self.fps.value()), "-i", pattern,
                   "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32",
                   "-pix_fmt", "yuv420p", str(output)]
        if not shutil.which("ffmpeg"):
            self.note.setText(
                f"{count} frames written to {frames_dir}.\n\n"
                "ffmpeg is not installed, so nothing was encoded. To make the video "
                "later:\n\n" + " ".join(command))
            return
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.SubprocessError) as exc:
            self.note.setText(f"Encoding failed ({exc}). The frames are kept in "
                              f"{frames_dir}.")
            return
        if proc.returncode != 0:
            self.note.setText(
                f"ffmpeg exited with {proc.returncode}; the frames are kept in "
                f"{frames_dir}.\n\n{(proc.stderr or '')[-800:]}")
            return
        shutil.rmtree(frames_dir, ignore_errors=True)
        self.note.setText(f"Wrote {output} ({count} frames).")


def _wrap(layout) -> QWidget:
    widget = QWidget()
    widget.setLayout(layout)
    return widget
