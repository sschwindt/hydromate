"""Load a finished job's results into the current project.

Reads the manifest the runner wrote and adds what QGIS can open, styled and grouped -
the same path the dock's "Load results" button takes, exposed so a model can end with it.

Nothing is copied. The layers point at the files where the solver left them, which for a
multi-gigabyte OpenFOAM case is the difference between working and not (plan §26).
"""

from __future__ import annotations

from pathlib import Path

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile,
                       QgsProcessingParameterNumber)

from ...core.result_loader import ResultLoader, discover
from ...gui.settings_dialog import read_settings


class ImportResultsAlgorithm(QgsProcessingAlgorithm):
    JOB_DIR = "JOB_DIR"
    MIN_DEPTH = "MIN_DEPTH"
    VELOCITY_CAP = "VELOCITY_CAP"
    COUNT = "COUNT"

    def createInstance(self):        # noqa: N802 - QGIS naming
        return ImportResultsAlgorithm()

    def name(self) -> str:
        return "importresults"

    def displayName(self) -> str:    # noqa: N802 - QGIS naming
        return "Import job results"

    def group(self) -> str:
        return "Jobs"

    def groupId(self) -> str:        # noqa: N802 - QGIS naming
        return "jobs"

    def shortHelpString(self) -> str:  # noqa: N802 - QGIS naming
        return ("Adds a completed job's results to the project as styled layers, "
                "grouped under axqua/&lt;job id&gt;.\n\n"
                "Water depth gets a white-to-blue ramp with everything below the "
                "minimum depth transparent; velocity gets arrows on a reversed plasma "
                "ramp capped at the given maximum. Files are referenced where they are, "
                "never copied.")

    def initAlgorithm(self, config=None):    # noqa: N802 - QGIS naming
        settings = read_settings()
        self.addParameter(QgsProcessingParameterFile(
            self.JOB_DIR, "Job directory",
            behavior=QgsProcessingParameterFile.Behavior.Folder))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_DEPTH, "Minimum water depth (m), below which depth is transparent",
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=settings["min_depth"], minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.VELOCITY_CAP, "Velocity scale maximum (m/s)",
            type=QgsProcessingParameterNumber.Type.Double,
            defaultValue=settings["velocity_cap"], minValue=0.1))
        from qgis.core import QgsProcessingOutputNumber
        self.addOutput(QgsProcessingOutputNumber(self.COUNT, "Layers added"))

    def processAlgorithm(self, parameters, context, feedback):   # noqa: N802
        from qgis.core import QgsProcessingException

        job_dir = Path(self.parameterAsFile(parameters, self.JOB_DIR, context))
        min_depth = self.parameterAsDouble(parameters, self.MIN_DEPTH, context)
        cap = self.parameterAsDouble(parameters, self.VELOCITY_CAP, context)

        results = discover(job_dir)
        if not results.layers:
            raise QgsProcessingException(
                f"{job_dir} holds nothing QGIS can open."
                + ("" if results.from_manifest
                   else " There is no result manifest either, so this was a plain scan "
                        "of the folder."))
        loader = ResultLoader(context.project(), min_depth=min_depth, velocity_cap=cap)
        added = loader.load(results)
        for warning in loader.warnings:
            feedback.pushWarning(warning)
        for item in results.documents:
            feedback.pushInfo(f"also produced (open outside QGIS): {item.path}")
        feedback.pushInfo(f"Added {len(added)} layer(s).")
        return {self.COUNT: len(added)}
