"""Which settings describe the *river*, and which name a *solver keyword*.

A case config mixes two kinds of statement that look identical in YAML:

* **shared intent** - facts about the reach and the modelling goal. The discharge,
  the roughness, the channel resolution, how converged a run has to be. Change the
  solver and these stay true.
* **solver detail** - the knobs of one particular code. ``IMPLICITATION FOR DEPTH``
  is a Telemac2d keyword; ``MULESCorr`` is an interFoam one. Neither means anything
  to the other solver, and neither is a statement about the river.

Nothing enforced that distinction, so a reader had to know the solvers to tell which
was which. This module states it **as data**, for two consumers that cannot guess:

* a form-based editor (the planned QGIS plugin) which should present "the reach" and
  "TELEMAC numerics" as different pages rather than one flat wall of fields;
* :func:`axqua.config.dump_config` and ``axqua migrate``, which need to know
  what may be renamed without changing meaning.

Why the fields have not been physically moved
----------------------------------------------
The original plan folded ``openfoam.cell_size`` into ``mesh.channel_size`` and
``openfoam.n_layers`` into a shared ``mesh.vertical_layers``. That has since become
the wrong move for the one case that motivated it: ``openfoam.cell_size_factor``
already expresses the OpenFOAM lattice **relative to** the 2D channel size, which is
the shared-intent link the fold was after - and it does so without pretending the two
numbers are the same quantity, which they are not. TELEMAC's is a per-zone
anisotropic target edge length; OpenFOAM's is a uniform Cartesian spacing. Collapsing
them would have replaced a real relationship with a false identity.

The remaining moves (twenty-odd Telemac2d keywords out of ``hydrodynamics:`` into a
``telemac.numerics`` block) are pure renaming: they change no behaviour, touch every
case config, and would invalidate the artifact regression baseline for nothing. So the
classification lives here, where a plugin can read it, and the field layout stays put.
:data:`LEGACY_KEYS` is the mechanism for doing the move later without breaking a
single existing config.
"""

from __future__ import annotations

import logging
from enum import Enum

log = logging.getLogger("axqua")


class Layer(str, Enum):
    """Who a setting belongs to."""

    SHARED = "shared"        # a fact about the reach or the goal
    TELEMAC = "telemac"      # a Telemac2d/3d or GAIA keyword
    OPENFOAM = "openfoam"    # an interFoam dictionary entry
    PROJECT = "project"      # paths and bookkeeping, not physics


# Blocks that are wholly one layer. Checked before the per-field table, so only the
# genuinely mixed blocks (`mesh`, `hydrodynamics`) need spelling out field by field.
BLOCK_LAYER: dict[str, Layer] = {
    "project": Layer.PROJECT,
    "outputs": Layer.PROJECT,
    "geodata": Layer.SHARED,        # the reach itself: DEMs, zones, centerline
    "boundaries": Layer.SHARED,     # what the river does at its ends
    "friction": Layer.SHARED,       # the bed, not a keyword
    "ground_truth": Layer.SHARED,   # measurements are measurements
    "calibration": Layer.SHARED,
    "morphodynamics": Layer.SHARED,
    "dem_of_difference": Layer.SHARED,
    "gain_lose": Layer.SHARED,      # a property of the reach; TELEMAC-only *today*
    "structures": Layer.SHARED,     # dams and walls are terrain, and both codes mesh them
    "drying": Layer.TELEMAC,
    "initialization": Layer.TELEMAC,
    "telemac": Layer.TELEMAC,
    "openfoam": Layer.OPENFOAM,
}

# The mixed blocks, field by field. Only the SHARED entries are listed: anything in a
# mixed block that is not named here is that block's own solver detail, which keeps
# this table short and makes "is this shared?" the question that has to be answered
# deliberately.
SHARED_FIELDS: dict[str, tuple[str, ...]] = {
    # Resolution is a modelling decision about the reach, not a solver keyword. The
    # gmsh/BAMG specifics beside them (growth_ratio, max_aspect_ratio, size_scale,
    # the quality thresholds) are TELEMAC's mesher and stay TELEMAC's.
    "mesh": ("channel_size", "floodplain_size", "refinement_size",
             "channel_anisotropy"),
    # Physics and goals, as opposed to the ~24 Telemac2d numerics keywords beside
    # them (implicitation, advection schemes, solver accuracies, sub-iterations).
    "hydrodynamics": ("duration", "turbulence_model", "wet_depth", "flux_tolerance",
                      "initial_velocity_guess", "turbulence_length_scale",
                      "desired_courant"),
}

# Legacy key -> canonical key, applied at load. Dotted, so a block rename and a field
# rename use one mechanism. This is what makes a future field move safe: add the
# mapping here, and every existing config keeps loading.
LEGACY_KEYS: dict[str, str] = {
    "project.work_dir": "project.preprocessing_dir",
    # results_dir became calibration_dir, NOT model_dir - the old single "results"
    # folder is where the HydroBayesCal artifacts land, and the phase split gave the
    # solver case its own model_dir. Reading it as model_dir would point a migrated
    # config's solver output at the calibration folder.
    "project.results_dir": "project.calibration_dir",
    # `percolation:` -> `gain_lose:` is handled in config._load_gain_lose, which also
    # remaps two field *values*; it is listed here only so a reader of this table sees
    # the whole set of renames in one place.
}


def classify_setting(key: str) -> Layer:
    """Which layer a dotted config key belongs to.

    Unknown keys inside a known block inherit that block's layer, and an unknown block
    is SHARED - the reading that fails safe for a form, since showing a setting on the
    reach page is a smaller mistake than hiding it behind a solver the user has not
    enabled.
    """
    block, _, fieldname = key.partition(".")
    if fieldname and block in SHARED_FIELDS and fieldname in SHARED_FIELDS[block]:
        return Layer.SHARED
    if block in BLOCK_LAYER:
        return BLOCK_LAYER[block]
    if block in SHARED_FIELDS:          # a mixed block's non-shared field
        return Layer.TELEMAC
    return Layer.SHARED


def apply_legacy(raw: dict) -> list[str]:
    """Rewrite deprecated keys in *raw* in place; return what was renamed.

    Returns the list so the caller can emit **one** aggregated warning naming
    everything that moved. A per-key warning turns a config written three years ago
    into a wall of text that hides whichever line actually matters.
    """
    renamed: list[str] = []
    for old, new in LEGACY_KEYS.items():
        old_block, _, old_field = old.partition(".")
        new_block, _, new_field = new.partition(".")
        section = raw.get(old_block)
        if not isinstance(section, dict) or old_field not in section:
            continue
        target = raw.setdefault(new_block, {}) if new_block != old_block else section
        if not isinstance(target, dict) or new_field in target:
            continue                     # the canonical key wins; it was set explicitly
        target[new_field] = section.pop(old_field)
        renamed.append(f"{old} -> {new}")
    return renamed


def warn_legacy(renamed: list[str], source: str = "") -> None:
    """Log one aggregated deprecation notice for *renamed*."""
    if not renamed:
        return
    where = f" in {source}" if source else ""
    log.warning("deprecated config keys%s, still loaded but rename them: %s",
                where, "; ".join(sorted(renamed)))


# `classify` reads fine inside this module; at package level it says nothing about
# what is being classified, so the exported name is the explicit one.
classify = classify_setting
