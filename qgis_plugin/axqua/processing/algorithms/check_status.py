"""Report one job's state, machine-readably.

The point of having this as an algorithm rather than only in the dock is the Modeler: a
batch that submits a sweep of discharges wants to poll them, and polling through
``processing.run`` needs no plugin code at all.

It reports and returns; it never waits. A blocking "wait until done" algorithm would be a
six-hour Processing task, which is the thing plan §21 forbids.
"""

from __future__ import annotations

import json

from qgis.core import (QgsProcessingAlgorithm, QgsProcessingParameterFile,
                       QgsProcessingParameterString)

from ...core.runner_client import RunnerClient, RunnerError
from ...gui.settings_dialog import read_settings


class CheckStatusAlgorithm(QgsProcessingAlgorithm):
    JOB_ID = "JOB_ID"
    JOB_ROOT = "JOB_ROOT"
    STATE = "STATE"
    REPORT = "REPORT"

    def createInstance(self):        # noqa: N802 - QGIS naming
        return CheckStatusAlgorithm()

    def name(self) -> str:
        return "checkjobstatus"

    def displayName(self) -> str:    # noqa: N802 - QGIS naming
        return "Check job status"

    def group(self) -> str:
        return "Jobs"

    def groupId(self) -> str:        # noqa: N802 - QGIS naming
        return "jobs"

    def shortHelpString(self) -> str:  # noqa: N802 - QGIS naming
        return ("Reports the current state of a axqua job (QUEUED, RUNNING, "
                "COMPLETED, FAILED, CANCELLED ...) together with its progress.\n\n"
                "Returns immediately - it does not wait for the job to finish.")

    def initAlgorithm(self, config=None):    # noqa: N802 - QGIS naming
        self.addParameter(QgsProcessingParameterString(self.JOB_ID, "Job id"))
        self.addParameter(QgsProcessingParameterFile(
            self.JOB_ROOT, "Job root (blank: axqua's default)",
            behavior=QgsProcessingParameterFile.Behavior.Folder, optional=True))
        from qgis.core import QgsProcessingOutputString
        self.addOutput(QgsProcessingOutputString(self.STATE, "State"))
        self.addOutput(QgsProcessingOutputString(self.REPORT, "Status (JSON)"))

    def processAlgorithm(self, parameters, context, feedback):   # noqa: N802
        from qgis.core import QgsProcessingException

        job_id = self.parameterAsString(parameters, self.JOB_ID, context).strip()
        job_root = self.parameterAsFile(parameters, self.JOB_ROOT, context)
        client = RunnerClient(read_settings()["executable"] or None)
        try:
            data = client.job_status(job_id, job_root=job_root or None)
        except RunnerError as exc:
            raise QgsProcessingException(exc.user_text()) from exc

        state = str((data or {}).get("state") or "")
        feedback.pushInfo(f"{job_id}: {state}")
        error = (data or {}).get("error")
        if error:
            feedback.reportError(f"{error.get('code')}: {error.get('message')}")
            if error.get("remedy"):
                feedback.pushInfo(str(error["remedy"]))
        return {self.STATE: state, self.REPORT: json.dumps(data or {})}
