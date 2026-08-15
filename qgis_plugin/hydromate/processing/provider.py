"""The Processing provider.

Three algorithms, all of which **validate, create a job, submit and return** (plan §21).
None of them runs a simulation. That distinction is easy to lose: a Processing algorithm
that blocked for six hours would be owned by QGIS's task manager, would die with QGIS, and
would undo the entire architecture - so ``Submit`` returns a job id in about a second and
the job outlives everything.

Being in Processing at all is worth it for one reason: it makes hydromate scriptable from
the Graphical Modeler and from ``processing.run``, so a user can batch-submit a discharge
sweep without writing a plugin.
"""

from __future__ import annotations

from qgis.core import QgsProcessingProvider

from ..compat import icon
from .algorithms.check_status import CheckStatusAlgorithm
from .algorithms.import_results import ImportResultsAlgorithm
from .algorithms.submit import SubmitJobAlgorithm


class HydromateProvider(QgsProcessingProvider):
    def loadAlgorithms(self) -> None:       # noqa: N802 - QGIS naming
        for algorithm in (SubmitJobAlgorithm(), CheckStatusAlgorithm(),
                          ImportResultsAlgorithm()):
            self.addAlgorithm(algorithm)

    def id(self) -> str:                    # noqa: A003 - QGIS naming
        return "hydromate"

    def name(self) -> str:
        return "HydroMate"

    def longName(self) -> str:              # noqa: N802 - QGIS naming
        return "HydroMate (TELEMAC / OpenFOAM)"

    def icon(self):
        return icon("hydromate.svg")
