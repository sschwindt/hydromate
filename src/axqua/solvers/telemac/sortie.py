"""Reader for the TELEMAC ``.sortie`` listing (the solver's printed output).

A steady TELEMAC-2D/3D run prints a *water-volume balance* block at every listing
printout::

                           BALANCE OF WATER VOLUME
         VOLUME IN THE DOMAIN :    6317.532     M3
         FLUX BOUNDARY    1:    0.8000116     M3/S  ( >0 : ENTERING  <0 : EXITING )
         FLUX BOUNDARY    2:   -7.3518840     M3/S  ( >0 : ENTERING  <0 : EXITING )
         RELATIVE ERROR IN VOLUME AT T =        28.57     S :   -0.1008281E-14

which is everything needed to judge whether a steady run has converged: at steady
state the boundary fluxes balance (total inflow = total outflow) and the domain
volume stops changing. This module turns that listing into arrays.

**Why axqua parses this itself.** The obvious alternative is TELEMAC's own
``postel.parser_output`` (and the ``pythomac`` package that adapts it), but both are
GPL-3 while axqua is BSD-3, so neither can be vendored here. The file *format*
is not the licensed part, so this is an independent reader written against the
listing itself - which also lets it be stricter and simpler than a general-purpose
one, because axqua needs exactly one thing from the listing:

* **Each balance block is parsed as a unit**, and its simulation time is taken from
  the ``RELATIVE ERROR IN VOLUME AT T = ...`` line *inside the block*. The general
  parser instead collects times from the separate ``ITERATION ... TIME:`` lines and
  zips the two sequences afterwards, which needs a synthetic zero-flux sample
  prepended to make the lengths match - so the first sample is fabricated and every
  later sample can slip by one if the listing has an extra ``ITERATION`` line
  (``PRINTING CUMULATED FLOWRATES`` and the listing printout period do not have to
  agree). Reading the time from inside the block makes misalignment impossible.
* Nothing is fabricated: what the run printed is what is returned.
* Blocks are only accepted when complete (volume + every boundary flux + the time),
  so a listing truncated mid-block by a crash or a still-running job yields the
  blocks written so far instead of raising.

Parallel runs write one main listing plus a ``*_p0000N.sortie`` per processor; only
the main one carries the merged balance. :func:`latest_sortie` returns it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# A number as TELEMAC prints it: 1234, 12.34, -0.1008281E-14, 1.2D+3
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[dDeE][-+]?\d+)?"

_RE_VOLUME = re.compile(rf"\s*VOLUME IN THE DOMAIN\s*:\s*(?P<value>{_NUM})", re.IGNORECASE)
_RE_FLUX = re.compile(
    rf"\s*FLUX BOUNDARY\s+(?P<index>\d+)\s*:\s*(?P<value>{_NUM})", re.IGNORECASE)
_RE_ERROR = re.compile(
    rf"\s*RELATIVE ERROR IN VOLUME AT T\s*=\s*(?P<time>{_NUM})\s*S\s*:\s*"
    rf"(?P<value>{_NUM})", re.IGNORECASE)
_RE_ITERATION = re.compile(r"\s*ITERATION\s+(?P<iteration>\d+)\s+TIME", re.IGNORECASE)
_RE_STUDY = re.compile(r"\s*.*NAME OF THE STUDY\s*:?\s*$", re.IGNORECASE)
_RE_EXEC = re.compile(
    r"\A\s*(?:(?P<days>\d+)\s*DAYS|(?P<hours>\d+)\s*HOURS"
    r"|(?P<minutes>\d+)\s*MINUTES|(?P<seconds>\d+)\s*SECONDS)\s*\Z", re.IGNORECASE)


def _to_float(text: str) -> float:
    return float(text.replace("d", "e").replace("D", "E"))


@dataclass
class Sortie:
    """The water-volume balance history of one TELEMAC listing.

    All arrays are one entry per listing printout and share the same ordering, so
    ``time[i]``, ``volume[i]`` and ``fluxes[i]`` describe the same instant.
    """

    path: Path
    study: str
    exec_seconds: float
    iteration: np.ndarray          # solver time-step number at each printout
    time: np.ndarray               # simulated time [s]
    volume: np.ndarray             # water volume in the domain [m3]
    fluxes: np.ndarray             # (n_printouts, n_boundaries) signed [m3/s]
    volume_error: np.ndarray       # relative volume error reported by TELEMAC

    @property
    def n_boundaries(self) -> int:
        return int(self.fluxes.shape[1]) if self.fluxes.size else 0

    @property
    def gross_in(self) -> np.ndarray:
        """Total inflow [m3/s]: the sum of the positive boundary fluxes.

        Summing by sign rather than by boundary index means any number of inflow /
        outflow boundaries works, and a boundary that momentarily reverses is
        counted on the side it is actually flowing.
        """
        return np.where(self.fluxes > 0, self.fluxes, 0.0).sum(axis=1)

    @property
    def gross_out(self) -> np.ndarray:
        """Total outflow magnitude [m3/s]: the sum of the negative fluxes, positive."""
        return -np.where(self.fluxes < 0, self.fluxes, 0.0).sum(axis=1)

    @property
    def printout_interval(self) -> float:
        """Median simulated time between listing printouts [s].

        Taken from the times actually printed, because a CFL-adaptive run has no
        fixed time step: ``LISTING PRINTOUT PERIOD x TIME STEP`` is meaningless there.
        """
        if self.time.size < 2:
            return float("nan")
        return float(np.median(np.diff(self.time)))

    def to_frame(self):
        """The balance history as a pandas DataFrame indexed by simulated time."""
        import pandas as pd

        data = {"iteration": self.iteration, "volume (m3)": self.volume,
                "volume error (-)": self.volume_error}
        for i in range(self.n_boundaries):
            data[f"flux boundary {i + 1} (m3/s)"] = self.fluxes[:, i]
        data["gross inflow (m3/s)"] = self.gross_in
        data["gross outflow (m3/s)"] = self.gross_out
        frame = pd.DataFrame(data, index=np.asarray(self.time, dtype=float))
        frame.index.name = "time (s)"
        return frame


def read_sortie(path: str | Path) -> Sortie:
    """Parse the water-volume balance history out of a ``.sortie`` listing.

    Raises :class:`ValueError` when the listing holds no complete balance block -
    normally because the run was launched without ``-s`` or without
    ``PRINTING CUMULATED FLOWRATES : YES``.
    """
    path = Path(path)
    study = "UNKNOWN"
    exec_seconds = 0.0
    iteration = 0
    want_study = False

    iterations: list[int] = []
    times: list[float] = []
    volumes: list[float] = []
    errors: list[float] = []
    flux_rows: list[dict[int, float]] = []

    volume: float | None = None
    pending: dict[int, float] = {}

    with open(path, errors="replace") as fh:
        for line in fh:
            if want_study:
                stripped = line.strip()
                study = stripped or "NO NAME OF STUDY"
                want_study = False
                continue
            if "NAME OF THE STUDY" in line.upper() and _RE_STUDY.match(line):
                want_study = True
                continue

            match = _RE_ITERATION.match(line)
            if match:
                iteration = int(match.group("iteration"))
                continue

            match = _RE_VOLUME.match(line)
            if match:
                # a new block starts here; anything half-collected is incomplete
                volume = _to_float(match.group("value"))
                pending = {}
                continue

            match = _RE_FLUX.match(line)
            if match and volume is not None:
                pending[int(match.group("index"))] = _to_float(match.group("value"))
                continue

            match = _RE_ERROR.match(line)
            if match and volume is not None:
                iterations.append(iteration)
                times.append(_to_float(match.group("time")))
                volumes.append(volume)
                errors.append(_to_float(match.group("value")))
                flux_rows.append(pending)
                volume, pending = None, {}
                continue

            match = _RE_EXEC.match(line)
            if match:
                for unit, factor in (("days", 86400), ("hours", 3600),
                                     ("minutes", 60), ("seconds", 1)):
                    if match.group(unit) is not None:
                        exec_seconds += factor * int(match.group(unit))

    if not flux_rows:
        raise ValueError(
            f"{path.name}: no complete 'BALANCE OF WATER VOLUME' block found. Run the "
            "solver with the -s flag and set 'PRINTING CUMULATED FLOWRATES : YES' in "
            "the steering file."
        )

    n_bnd = max((max(row) for row in flux_rows if row), default=0)
    fluxes = np.full((len(flux_rows), n_bnd), np.nan)
    for i, row in enumerate(flux_rows):
        for index, value in row.items():
            fluxes[i, index - 1] = value
    # drop any block whose fluxes were not all printed (a truncated tail)
    complete = ~np.isnan(fluxes).any(axis=1) if n_bnd else np.zeros(len(flux_rows), bool)
    if not complete.any():
        raise ValueError(f"{path.name}: balance blocks carry no boundary fluxes; "
                         "set 'PRINTING CUMULATED FLOWRATES : YES'.")
    keep = np.flatnonzero(complete)
    return Sortie(
        path=path, study=study, exec_seconds=exec_seconds,
        iteration=np.asarray(iterations, dtype=int)[keep],
        time=np.asarray(times, dtype=float)[keep],
        volume=np.asarray(volumes, dtype=float)[keep],
        fluxes=fluxes[keep],
        volume_error=np.asarray(errors, dtype=float)[keep],
    )


def latest_sortie(model_dir: str | Path, cas_name: str) -> Path | None:
    """Newest **main** listing for *cas_name* in *model_dir*.

    TELEMAC names it ``<cas>_<YYYY-MM-DD-HHhMMminSSs>.sortie``; a parallel run adds a
    ``_p0000N`` copy per processor, which carries only that subdomain's balance and
    is skipped here.
    """
    model_dir = Path(model_dir)
    candidates = [p for p in model_dir.glob(f"{cas_name}_*.sortie")
                  if not re.search(r"_p\d+\.sortie$", p.name)]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.stat().st_mtime, p.name))


def processor_sorties(model_dir: str | Path, cas_name: str) -> list[Path]:
    """The per-processor ``*_p0000N.sortie`` copies of a parallel run."""
    return sorted(p for p in Path(model_dir).glob(f"{cas_name}_*.sortie")
                  if re.search(r"_p\d+\.sortie$", p.name))


# --------------------------------------------------------------------------- #
# GAIA / tracer mass balances
#
# Adopted from the `pythomac` package (Sebastian Schwindt, hydro-informatics.com),
# whose flux/convergence post-processing axqua used to call out to. The field
# lists below were re-derived from TELEMAC v9.1.1's own FORTRAN FORMAT statements
# (gaia/mass_balance.f, telemac3d/bil3d.f) rather than transcribed, which is why
# they carry more than the four sediment quantities pythomac exposes - the erosion /
# deposition fluxes and the per-boundary bedload fluxes are what a morphodynamic run
# actually needs to be checked against.
# --------------------------------------------------------------------------- #

#: GAIA per-class mass-balance quantities, as printed by ``gaia/mass_balance.f``
_SEDIMENT_FIELDS = {
    "total_mass": "TOTAL MASS",
    "initial_mass": "INITIAL MASS",
    "initial_mass_active_layer": "INITIAL MASS ACTIVE LAYER",
    "lost_mass": "LOST MASS",
    "cumulated_lost_mass": "CUMULATED LOST MASS",
    "total_bed_evolutions": "TOTAL BED EVOLUTIONS",
    "cumulated_bed_evolutions": "CUMULATED BED EVOLUTIONS",
    "erosion_flux": "EROSION FLUX",
    "deposition_flux": "DEPOSITION FLUX",
    "cumulated_erosion": "CUMULATED EROSION",
    "cumulated_deposition": "CUMULATED DEPOSITION",
    "relative_error_active_layer": "RELATIVE ERROR TO INITIAL ACT LAYER MASS",
    "relative_error_total": "RELATIVE ERROR TO TOTAL INITIAL MASS",
    "boundaries_bedload_flux": "BOUNDARIES BEDLOAD FLUX",
    "cumulated_boundaries_bedload_mass": "CUMULATED BOUNDARIES BEDLOAD MASS",
}

_RE_SED_CLASS = re.compile(r"\s*SEDIMENT CLASS NUMBER\s*=\s*(?P<klass>\d+)", re.IGNORECASE)
_RE_SED_ALL = re.compile(r"\s*GAIA MASS-BALANCE OF SEDIMENTS OVER ALL CLASSES", re.IGNORECASE)
_RE_SED_PER = re.compile(r"\s*GAIA MASS-BALANCE OF SEDIMENTS PER CLASS", re.IGNORECASE)
_RE_BEDLOAD_BND = re.compile(
    rf"\s*(?P<cum>CUMULATED )?BEDLOAD (?:FLUX )?BOUNDARY\s+(?P<index>\d+)\s*=\s*"
    rf"(?P<value>{_NUM})", re.IGNORECASE)


def _field_pattern(label: str) -> re.Pattern:
    # GAIA pads the label out to a fixed column before the '='
    return re.compile(rf"\s*{re.escape(label)}\s*=\s*(?P<value>{_NUM})", re.IGNORECASE)


_SEDIMENT_PATTERNS = {key: _field_pattern(label)
                      for key, label in _SEDIMENT_FIELDS.items()}
# longest label first so 'CUMULATED LOST MASS' is never eaten by 'LOST MASS'
_SEDIMENT_ORDER = sorted(_SEDIMENT_FIELDS, key=lambda k: -len(_SEDIMENT_FIELDS[k]))


def sediment_mass_profile(path: str | Path) -> dict[int, dict[str, np.ndarray]]:
    """GAIA sediment mass balance per class over the run.

    Returns ``{class_number: {quantity: array}}`` with one entry per listing
    printout; class ``0`` holds the "OVER ALL CLASSES" summary block. Quantities are
    the keys of :data:`_SEDIMENT_FIELDS` (total/lost/cumulated mass, erosion and
    deposition fluxes, bed evolutions, the relative mass errors, ...). An empty dict
    means the run was not coupled to GAIA.

    The mass balance is the morphodynamic counterpart of the water-volume balance:
    ``relative_error_active_layer`` and ``cumulated_lost_mass`` are what tell you a
    GAIA run stayed mass-conservative.
    """
    per_class: dict[int, dict[str, list[float]]] = {}
    current: int | None = None
    with open(Path(path), errors="replace") as fh:
        for line in fh:
            if _RE_SED_ALL.match(line):
                current = 0
                per_class.setdefault(0, {})
                continue
            if _RE_SED_PER.match(line):
                current = None           # the class number follows on the next line
                continue
            match = _RE_SED_CLASS.match(line)
            if match:
                current = int(match.group("klass"))
                per_class.setdefault(current, {})
                continue
            if current is None:
                continue
            for key in _SEDIMENT_ORDER:
                match = _SEDIMENT_PATTERNS[key].match(line)
                if match:
                    per_class[current].setdefault(key, []).append(
                        _to_float(match.group("value")))
                    break
            else:
                match = _RE_BEDLOAD_BND.match(line)
                if match:
                    prefix = "cumulated_bedload_boundary" if match.group("cum") \
                        else "bedload_flux_boundary"
                    key = f"{prefix}_{int(match.group('index'))}"
                    per_class[current].setdefault(key, []).append(
                        _to_float(match.group("value")))
    return {klass: {k: np.asarray(v, dtype=float) for k, v in fields.items()}
            for klass, fields in per_class.items() if fields}


_RE_TRACER = re.compile(r"\s*TRACER\s+(?P<index>\d+)\s*:", re.IGNORECASE)
_RE_TRACER_NOW = re.compile(
    rf"\s*QUANTITY AT THE PRESENT TIME STEP\s*:\s*(?P<value>{_NUM})", re.IGNORECASE)
_RE_TRACER_ERR = re.compile(
    rf"\s*ERROR ON THE QUANTITY DURING THIS TIME STEP\s*:\s*(?P<value>{_NUM})",
    re.IGNORECASE)
_RE_WATER_BLOCK = re.compile(r"\s*WATER\s*:?\s*$", re.IGNORECASE)


def tracer_mass_profile(path: str | Path) -> dict[int, dict[str, np.ndarray]]:
    """Tracer mass balance per tracer over the run.

    Returns ``{tracer_number: {"total": array, "error": array}}``, one entry per
    listing printout. GAIA carries suspended sediment as TELEMAC tracers, so this is
    how a suspended-load run is mass-checked. The listing's ``WATER`` balance block
    uses the same wording and is skipped.
    """
    per_tracer: dict[int, dict[str, list[float]]] = {}
    current: int | None = None
    with open(Path(path), errors="replace") as fh:
        for line in fh:
            if _RE_WATER_BLOCK.match(line):
                current = None
                continue
            match = _RE_TRACER.match(line)
            if match:
                current = int(match.group("index"))
                per_tracer.setdefault(current, {})
                continue
            if current is None:
                continue
            match = _RE_TRACER_NOW.match(line)
            if match:
                per_tracer[current].setdefault("total", []).append(
                    _to_float(match.group("value")))
                continue
            match = _RE_TRACER_ERR.match(line)
            if match:
                per_tracer[current].setdefault("error", []).append(
                    _to_float(match.group("value")))
    return {idx: {k: np.asarray(v, dtype=float) for k, v in fields.items()}
            for idx, fields in per_tracer.items() if fields}


def find_lines(path: str | Path, pattern: str, *, with_line_numbers: bool = False):
    """Every listing line matching *pattern* (a regular expression).

    The escape hatch for anything this module does not model - a user-Fortran
    printout, a solver warning, a keyword echo. ``USER_RAIN`` percolation diagnostics
    in the isar-2025 case are read this way, for instance.
    """
    regex = re.compile(pattern, re.IGNORECASE)
    hits, numbers = [], []
    with open(Path(path), errors="replace") as fh:
        for number, line in enumerate(fh, start=1):
            if regex.search(line):
                hits.append(line.strip())
                numbers.append(number)
    return (hits, numbers) if with_line_numbers else hits
