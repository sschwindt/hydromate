"""Submit a axqua job and return its id.

**It must not run the simulation** (plan §21). It validates, creates the job, hands it to
the runner and finishes - typically in about a second. The solver then runs detached and
is watched from the dock or from ``Check job status``.

That is what makes this usable in the Graphical Modeler: an algorithm that blocked for
hours could not be part of a model, and would die with QGIS anyway.
"""

from __future__ import annotations

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterEnum,
                       QgsProcessingParameterFile, QgsProcessingParameterNumber,
                       QgsProcessingParameterString)

from ...core.runner_client import RunnerClient, RunnerError
from ...gui.settings_dialog import read_settings

#: Offered in the enum. Kept short - the full list is in ``axqua submit --help-kinds``
#: and in the dock, which generates its tabs from the case itself.
KINDS = ["preprocessing", "steady", "mesh-convergence", "build-3d", "steady-3d",
         "vertical-convergence", "unsteady", "openfoam-build", "openfoam-run",
         "calibration"]


class SubmitJobAlgorithm(QgsProcessingAlgorithm):
    CONFIG = "CONFIG"
    KIND = "KIND"
    PROFILE = "PROFILE"
    JOB_ROOT = "JOB_ROOT"
    PROCESSES = "PROCESSES"
    JOB_ID = "JOB_ID"

    def createInstance(self):        # noqa: N802 - QGIS naming
        return SubmitJobAlgorithm()

    def name(self) -> str:
        return "submitjob"

    def displayName(self) -> str:    # noqa: N802 - QGIS naming
        return "Submit a simulation job"

    def group(self) -> str:
        return "Jobs"

    def groupId(self) -> str:        # noqa: N802 - QGIS naming
        return "jobs"

    def shortHelpString(self) -> str:  # noqa: N802 - QGIS naming
        return (
            "Creates a persistent axqua job and starts it detached, then returns "
            "the job id.\n\n"
            "The simulation does <b>not</b> run inside this algorithm: it is launched "
            "as a system job that keeps running when QGIS is closed. Watch it in the "
            "aXqua panel, or with the 'Check job status' algorithm.")

    def initAlgorithm(self, config=None):    # noqa: N802 - QGIS naming
        self.addParameter(QgsProcessingParameterFile(
            self.CONFIG, "Case configuration (case-config.yml)",
            extension="yml"))
        self.addParameter(QgsProcessingParameterEnum(
            self.KIND, "What to run", options=KINDS, defaultValue=1))
        self.addParameter(QgsProcessingParameterString(
            self.PROFILE, "Solver profile (blank: use the case config)",
            defaultValue="", optional=True))
        self.addParameter(QgsProcessingParameterFile(
            self.JOB_ROOT, "Job root (blank: axqua's default)",
            behavior=QgsProcessingParameterFile.Behavior.Folder, optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.PROCESSES, "MPI processes (0: from the profile or config)",
            type=QgsProcessingParameterNumber.Type.Integer, defaultValue=0, minValue=0))
        self.addOutput(_string_output(self.JOB_ID, "Job id"))

    def processAlgorithm(self, parameters, context, feedback):   # noqa: N802
        config = self.parameterAsFile(parameters, self.CONFIG, context)
        kind = KINDS[self.parameterAsEnum(parameters, self.KIND, context)]
        profile = self.parameterAsString(parameters, self.PROFILE, context).strip()
        job_root = self.parameterAsFile(parameters, self.JOB_ROOT, context)
        processes = self.parameterAsInt(parameters, self.PROCESSES, context)

        client = RunnerClient(read_settings()["executable"] or None)
        try:
            info = client.validate()
        except RunnerError as exc:
            raise _fail(exc.user_text()) from exc
        feedback.pushInfo(info.describe())

        feedback.pushInfo(f"Submitting a {kind} job for {config} ...")
        try:
            data = client.submit(config, kind, profile=profile or None,
                                 job_root=job_root or None,
                                 processes=processes or None)
        except RunnerError as exc:
            raise _fail(exc.user_text()) from exc

        job_id = str((data or {}).get("job_id") or "")
        feedback.pushInfo(f"Submitted {job_id}.")
        feedback.pushInfo("The solver is now running independently of QGIS; you can "
                          "close QGIS and it will keep going.")
        return {self.JOB_ID: job_id}


def _string_output(name: str, description: str):
    from qgis.core import QgsProcessingOutputString
    return QgsProcessingOutputString(name, description)


def _fail(message: str) -> Exception:
    from qgis.core import QgsProcessingException
    return QgsProcessingException(message)
