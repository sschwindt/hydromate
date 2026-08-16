"""Tabs generated from hydromate's capability matrix, not from a hardcoded list.

``hydromate case-status --json`` reports, per solver, every capability on three axes -
*implemented*, *configured*, *built/run*. Those **are** the tabs, and driving the UI from
them buys three things a hardcoded tab set cannot:

* a capability that is ``n/a`` for the chosen solver is **hidden**. OpenFOAM's free
  surface is inherently three-dimensional and transient, so a "Steady 2D" tab under
  OpenFOAM is not a missing feature but a category error, and showing it disabled would
  invite the user to go looking for the switch that turns it on;
* a capability that is ``no`` - not implemented *yet* - is **shown, disabled, with the
  reason**. That distinction is the entire reason the marker files report three values
  rather than two, and it is the difference between "hydromate cannot do this" and
  "hydromate cannot do this *here*";
* adding a capability or a whole solver to hydromate surfaces in the plugin **with no
  plugin change**, which is the point of the registry.

Actions enable from ``configured``/``built``/``run`` rather than from the plugin's own
guess about what a case has done - so the button that submits a 3D run is live exactly
when a 2D result exists for it to hotstart from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Display order and titles. A capability absent from here still gets a tab (titled from
#: its own name), because the whole point is that hydromate may grow one we have never
#: heard of.
TITLES = {
    "steady2d": "Steady 2D",
    "unsteady2d": "Unsteady 2D",
    "steady3d": "Steady 3D",
    "unsteady3d": "Unsteady 3D",
    "free_surface_3d": "Free surface (VOF)",
    "morphodynamics": "Morphodynamics",
    "mesh_convergence": "Mesh convergence",
    "vertical_convergence": "Vertical convergence",
    "calibration": "Calibration (BAL)",
    "gain_lose": "Gain-lose reach",
}

ORDER = list(TITLES)

#: Which job kind a tab submits. Kept here rather than derived, because it is a UI
#: decision: several kinds may share a capability, and the tab picks the usual one.
PRIMARY_KIND = {
    "steady2d": "steady",
    "unsteady2d": "unsteady",
    "steady3d": "steady-3d",
    "free_surface_3d": "openfoam-run",
    "mesh_convergence": "mesh-convergence",
    "vertical_convergence": "vertical-convergence",
    "calibration": "calibration",
}

#: The kind that *builds* what the tab then runs, where the two differ.
BUILD_KIND = {
    "steady2d": "preprocessing",
    "unsteady2d": "unsteady",
    "steady3d": "build-3d",
    "free_surface_3d": "openfoam-build",
}

NOT_IMPLEMENTED_REASON = (
    "hydromate does not implement this for {solver} yet. It is a gap in hydromate, not "
    "a limitation of your case.")
NOT_CONFIGURED_REASON = (
    "This case does not ask for {title} yet. Add the relevant block to case-config.yml "
    "(see the annotated template) and refresh.")


@dataclass
class CapabilityView:
    """One capability, as the UI needs to see it."""

    name: str
    solver: str
    implemented: str            # yes | no | n/a
    configured: bool | None = None
    built: bool | None = None
    run: bool | None = None

    @property
    def title(self) -> str:
        return TITLES.get(self.name, self.name.replace("_", " ").capitalize())

    @property
    def visible(self) -> bool:
        """Hidden only for a category error."""
        return self.implemented != "n/a"

    @property
    def enabled(self) -> bool:
        return self.implemented == "yes"

    @property
    def can_submit(self) -> bool:
        """A run needs the case to ask for it. Whether the *build* exists is a separate
        question, answered by :attr:`needs_build`."""
        return self.enabled and bool(self.configured)

    @property
    def needs_build(self) -> bool:
        return self.can_submit and self.built is False

    @property
    def has_results(self) -> bool:
        return bool(self.run)

    @property
    def reason(self) -> str:
        """Why an action is unavailable - shown, never left to be guessed."""
        if self.implemented == "no":
            return NOT_IMPLEMENTED_REASON.format(solver=self.solver)
        if not self.configured:
            return NOT_CONFIGURED_REASON.format(title=self.title)
        return ""

    @property
    def state_text(self) -> str:
        if self.implemented != "yes":
            return "not available"
        marks = [name for name, flag in (("configured", self.configured),
                                         ("built", self.built), ("run", self.run))
                 if flag]
        return ", ".join(marks) if marks else "not set up"

    @property
    def submit_kind(self) -> str | None:
        return PRIMARY_KIND.get(self.name)

    @property
    def build_kind(self) -> str | None:
        return BUILD_KIND.get(self.name)


@dataclass
class SolverView:
    name: str
    enabled: bool
    env_ok: bool | None
    env_detail: str = ""
    capabilities: list[CapabilityView] = field(default_factory=list)

    def capability(self, name: str) -> CapabilityView | None:
        return next((c for c in self.capabilities if c.name == name), None)

    @property
    def visible_capabilities(self) -> list[CapabilityView]:
        by_name = {c.name: c for c in self.capabilities}
        ordered = [by_name.pop(n) for n in ORDER if n in by_name]
        # Anything hydromate knows about that this plugin does not goes on the end,
        # rather than being dropped - that is what "no plugin change" means in practice.
        return [c for c in ordered + list(by_name.values()) if c.visible]


@dataclass
class CaseView:
    """The whole ``case-status --json`` document, as views."""

    case_dir: str = ""
    solvers: list[SolverView] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CaseView":
        solvers = []
        for entry in payload.get("solvers") or []:
            name = str(entry.get("solver") or "")
            solvers.append(SolverView(
                name=name,
                enabled=bool(entry.get("enabled")),
                env_ok=entry.get("env_ok"),
                env_detail=str(entry.get("env_detail") or ""),
                capabilities=[
                    CapabilityView(
                        name=str(c.get("capability") or ""),
                        solver=name,
                        implemented=str(c.get("implemented") or "no"),
                        configured=c.get("configured"),
                        built=c.get("built"),
                        run=c.get("run"),
                    )
                    for c in (entry.get("capabilities") or [])
                ],
            ))
        return cls(case_dir=str(payload.get("case_dir") or ""), solvers=solvers)

    def solver(self, name: str) -> SolverView | None:
        return next((s for s in self.solvers if s.name == name), None)

    @property
    def enabled_solvers(self) -> list[SolverView]:
        """Only the solvers this case declares.

        A case with no ``openfoam:`` block should not grow an OpenFOAM tab; the marker
        filename already reflects the case rather than the machine, and the UI follows
        the same rule.
        """
        return [s for s in self.solvers if s.enabled]
