"""
Extract boundary flux / discharge convergence from a TELEMAC .sortie file and
plot inflow and outflow magnitudes against a target steady discharge.

Assumed TELEMAC sign convention for common listing lines:
    FLUX BOUNDARY n: +Q  -> entering domain
    FLUX BOUNDARY n: -Q  -> exiting domain

Default plotting:
    x-axis: listing printout time [s]
    y-axis: 0.5 * Qtar to 1.5 * Qtar
    Qtar: purple dashed horizontal line
    inflow magnitude: red
    outflow magnitude: blue

Example:
    python convergence_plotter.py t3d_case.sortie --qtar 2.0

Example with CSV export:
    python convergence_plotter.py t3d_case.sortie --qtar 2.0 --csv convergence.csv

Author: Sebastian Schwindt (2026-Jun-30)
"""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import matplotlib.pyplot as plt
from matplotlib.ticker import AutoMinorLocator, MaxNLocator, MultipleLocator


NUM = r"[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[EeDd][+-]?\d+)?"

# Common TELEMAC listing form, e.g.:
#   FLUX BOUNDARY 2: 178.2000 M3/S ( >0 : ENTERING <0 : EXITING )
FLUX_PATTERNS = [
    re.compile(rf"\bFLUX\s+BOUNDARY\s+(\d+)\s*:?\s*({NUM})", re.IGNORECASE),
    re.compile(rf"\bBOUNDARY\s+(\d+)\b.*?\bFLUX\b\s*:?\s*({NUM})", re.IGNORECASE),
    re.compile(rf"\bLIQUID\s+BOUNDARY\s+(\d+)\b.*?\b(?:FLUX|FLOW(?:RATE)?)\b\s*:?\s*({NUM})", re.IGNORECASE),
]

# Common TELEMAC iteration/time line, e.g.:
#   ITERATION        100    TIME:   50.0000 S
ITER_TIME_RE = re.compile(
    rf"\bITERATION\s+(\d+)\b.*?\bTIME\s*:?\s*({NUM})\s*S?\b",
    re.IGNORECASE,
)

# Fallback for lines containing only time information.
TIME_RE = re.compile(rf"\bTIME\s*:?\s*({NUM})\s*S?\b", re.IGNORECASE)


@dataclass
class ListingRecord:
    """Boundary-flux data belonging to one TELEMAC listing printout."""
    listing_index: int
    iteration: Optional[int] = None
    time_s: Optional[float] = None
    boundary_fluxes: Dict[int, float] = field(default_factory=dict)

    @property
    def has_fluxes(self) -> bool:
        return bool(self.boundary_fluxes)

    @property
    def q_in(self) -> float:
        """Sum of positive boundary fluxes: entering-domain discharge magnitude."""
        return sum(q for q in self.boundary_fluxes.values() if q > 0.0)

    @property
    def q_out(self) -> float:
        """Sum of negative boundary fluxes converted to positive exiting magnitude."""
        return sum(-q for q in self.boundary_fluxes.values() if q < 0.0)

    @property
    def q_net(self) -> float:
        """Signed net flux; near zero means global inflow/outflow balance."""
        return sum(self.boundary_fluxes.values())


def parse_float(value: str) -> float:
    """Parse TELEMAC-style floats, including Fortran D exponents."""
    return float(value.replace("D", "E").replace("d", "e"))


def parse_sortie(sortie_path: Path) -> List[ListingRecord]:
    """
    Parse TELEMAC .sortie boundary fluxes.

    The parser is intentionally tolerant: it starts a new listing record when it
    sees an ITERATION/TIME line. Boundary-flux lines following that line are
    attached to the current record.
    """
    records: List[ListingRecord] = []
    current: Optional[ListingRecord] = None
    listing_index = -1

    def new_record(iteration: Optional[int], time_s: Optional[float]) -> ListingRecord:
        nonlocal listing_index
        listing_index += 1
        rec = ListingRecord(
            listing_index=listing_index,
            iteration=iteration,
            time_s=time_s,
        )
        records.append(rec)
        return rec

    with sortie_path.open("r", encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.strip()

            m_iter = ITER_TIME_RE.search(line)
            if m_iter:
                iteration = int(m_iter.group(1))
                time_s = parse_float(m_iter.group(2))
                current = new_record(iteration=iteration, time_s=time_s)
                continue

            # If time appears before fluxes, keep it on the current record;
            # if there is no current record, start one.
            m_time = TIME_RE.search(line)
            if m_time and "ITERATION" not in line.upper():
                time_s = parse_float(m_time.group(1))
                if current is None or current.has_fluxes:
                    current = new_record(iteration=None, time_s=time_s)
                else:
                    current.time_s = time_s

            for pat in FLUX_PATTERNS:
                m_flux = pat.search(line)
                if not m_flux:
                    continue

                boundary_id = int(m_flux.group(1))
                flux = parse_float(m_flux.group(2))

                if current is None:
                    current = new_record(iteration=None, time_s=None)

                current.boundary_fluxes[boundary_id] = flux
                break

    parsed = [r for r in records if r.has_fluxes]

    # Fallback x coordinate: listing index if no time was found.
    # For plotting with a time axis, replace missing times by their sequence number.
    for i, rec in enumerate(parsed):
        if rec.time_s is None:
            rec.time_s = float(i)

    return parsed


def infer_listing_dt(times: List[float]) -> Optional[float]:
    """Infer representative listing printout interval from parsed time coordinates."""
    if len(times) < 2:
        return None

    diffs = []
    for a, b in zip(times[:-1], times[1:]):
        d = b - a
        if d > 0.0 and math.isfinite(d):
            diffs.append(d)

    if not diffs:
        return None

    diffs.sort()
    mid = len(diffs) // 2
    if len(diffs) % 2:
        return diffs[mid]
    return 0.5 * (diffs[mid - 1] + diffs[mid])


def first_converged_index(
    values: List[float],
    qtar: float,
    tol: float,
    consecutive: int,
) -> Optional[int]:
    """
    Return the first index where abs(Q - Qtar) / Qtar <= tol
    for `consecutive` consecutive values.
    """
    if qtar == 0.0 or consecutive <= 0:
        return None

    run = 0
    for i, q in enumerate(values):
        relerr = abs(q - qtar) / abs(qtar)
        if relerr <= tol:
            run += 1
            if run >= consecutive:
                return i - consecutive + 1
        else:
            run = 0

    return None



def percent_deviation(q: float, qtar: float) -> float:
    """Return percentage deviation from Qtar."""
    return 100.0 * (q - qtar) / qtar


def nice_percentage_step(span: float) -> float:
    """
    Return a readable major tick spacing for a percentage axis.

    This avoids unreadable tick marks such as 0.1375 %, while keeping enough
    resolution for small convergence deviations.
    """
    if span <= 0.0 or not math.isfinite(span):
        return 1.0

    raw = span / 8.0
    exponent = math.floor(math.log10(raw))
    base = raw / (10.0 ** exponent)

    if base <= 1.0:
        nice = 1.0
    elif base <= 2.0:
        nice = 2.0
    elif base <= 2.5:
        nice = 2.5
    elif base <= 5.0:
        nice = 5.0
    else:
        nice = 10.0

    return nice * (10.0 ** exponent)


def write_csv(records: List[ListingRecord], csv_path: Path, qtar: float) -> None:
    """Write parsed convergence data to CSV."""
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "listing_index",
            "iteration",
            "time_s",
            "q_in_m3s",
            "q_out_m3s",
            "q_net_m3s",
            "relerr_in",
            "relerr_out",
            "boundary_fluxes_signed_m3s",
        ])

        for rec in records:
            relerr_in = abs(rec.q_in - qtar) / abs(qtar) if qtar != 0.0 else float("nan")
            relerr_out = abs(rec.q_out - qtar) / abs(qtar) if qtar != 0.0 else float("nan")
            bflux = ";".join(
                f"{bid}:{q:.12g}" for bid, q in sorted(rec.boundary_fluxes.items())
            )
            writer.writerow([
                rec.listing_index,
                "" if rec.iteration is None else rec.iteration,
                rec.time_s,
                rec.q_in,
                rec.q_out,
                rec.q_net,
                relerr_in,
                relerr_out,
                bflux,
            ])


def plot_convergence(
    records: List[ListingRecord],
    qtar: float,
    output: Path,
    title: Optional[str] = None,
    tol: float = 5.0e-4,
    consecutive: int = 5,
    mark_convergence: bool = False,
    show: bool = False,
    x_major: Optional[float] = None,
    x_minor: Optional[float] = None,
    xmax: Optional[float] = None,
) -> None:
    """Create the discharge convergence plot as percentage deviation from Qtar."""
    times = [float(r.time_s) for r in records]
    q_in = [r.q_in for r in records]
    q_out = [r.q_out for r in records]

    dev_in = [percent_deviation(q, qtar) for q in q_in]
    dev_out = [percent_deviation(q, qtar) for q in q_out]
    all_dev = dev_in + dev_out

    # Requested y scaling:
    # y_min = largest negative deviation from Qtar across inflow/outflow.
    # y_max = largest positive deviation from Qtar across inflow/outflow.
    y_min = min(all_dev)
    y_max = max(all_dev)

    # Keep the Qtar reference line visible even if all deviations have one sign.
    y_min = min(y_min, 0.0)
    y_max = max(y_max, 0.0)

    # Avoid a collapsed y-axis if deviations are exactly zero.
    if math.isclose(y_min, y_max, rel_tol=0.0, abs_tol=1.0e-12):
        y_min -= 1.0
        y_max += 1.0

    fig, ax = plt.subplots(figsize=(10.5, 5.8))

    ax.plot(times, dev_in, color="red", linewidth=1.6, label="Inflow deviation")
    ax.plot(times, dev_out, color="blue", linewidth=1.6, label="Outflow deviation")
    ax.axhline(0.0, color="purple", linestyle="--", linewidth=1.4, label=f"Qtar = {qtar:g} m³/s")

    if mark_convergence:
        i_in = first_converged_index(q_in, qtar, tol=tol, consecutive=consecutive)
        i_out = first_converged_index(q_out, qtar, tol=tol, consecutive=consecutive)
        candidates = [i for i in [i_in, i_out] if i is not None]
        if len(candidates) == 2:
            conv_i = max(candidates)
            ax.axvline(times[conv_i], color="black", linestyle=":", linewidth=1.1)
            ax.text(
                times[conv_i],
                y_max,
                f"converged @ {times[conv_i]:g} s",
                rotation=90,
                va="top",
                ha="right",
            )

    # Requested: fix xmin to 0 and leave xmax automatic unless explicitly provided.
    if xmax is None:
        ax.set_xlim(left=0.0)
    else:
        ax.set_xlim(left=0.0, right=xmax)

    ax.set_ylim(y_min, y_max)

    ax.set_xlabel("Listing printout time [s]")
    ax.set_ylabel("Deviation from Qtar [%]")

    if title:
        ax.set_title(title)
    else:
        ax.set_title("TELEMAC discharge convergence from .sortie boundary fluxes")

    # Ticks inside, including top/right ticks.
    ax.tick_params(axis="both", which="both", direction="in", top=True, right=True)

    # X-axis tick control:
    # Use --x-major / --x-minor if provided. Otherwise, let Matplotlib choose
    # readable ticks and avoid the old hard-coded xmax = total_time behaviour.
    if x_major is not None:
        ax.xaxis.set_major_locator(MultipleLocator(x_major))
    else:
        ax.xaxis.set_major_locator(MaxNLocator(nbins="auto", min_n_ticks=5, steps=[1, 2, 2.5, 5, 10]))

    if x_minor is not None:
        ax.xaxis.set_minor_locator(MultipleLocator(x_minor))
    else:
        ax.xaxis.set_minor_locator(AutoMinorLocator())

    # Readable percentage-axis ticks.
    y_span = y_max - y_min
    y_major = nice_percentage_step(y_span)
    ax.yaxis.set_major_locator(MultipleLocator(y_major))
    ax.yaxis.set_minor_locator(MultipleLocator(y_major / 2.0))

    ax.grid(True, which="major", axis="both", linewidth=0.65, alpha=0.55)
    ax.grid(True, which="minor", axis="y", linewidth=0.45, alpha=0.35)

    ax.legend(loc="best", frameon=True)
    fig.tight_layout()

    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=300)

    if show:
        plt.show()

    plt.close(fig)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Extract TELEMAC .sortie boundary fluxes and plot discharge "
            "convergence as percentage deviation from target Q."
        )
    )
    p.add_argument(
        "sortie",
        type=Path,
        help="Path to TELEMAC .sortie file.",
    )
    p.add_argument(
        "--qtar",
        type=float,
        default=2.0,
        help="Target steady discharge Qtar in m3/s. Default: 2.0",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("discharge_convergence_relative.png"),
        help="Output plot file. Default: discharge_convergence_relative.png",
    )
    p.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Optional CSV file with parsed time, inflow, outflow, and boundary fluxes.",
    )
    p.add_argument(
        "--tol",
        type=float,
        default=5.0e-4,
        help="Relative convergence tolerance used for printed convergence report. Default: 5e-4",
    )
    p.add_argument(
        "--consecutive",
        type=int,
        default=5,
        help="Required number of consecutive printouts below tolerance. Default: 5",
    )
    p.add_argument(
        "--mark-convergence",
        action="store_true",
        help="Draw a vertical marker if both inflow and outflow satisfy the tolerance.",
    )
    p.add_argument(
        "--x-major",
        type=float,
        default=None,
        help="Manual x-axis major tick spacing in seconds, e.g. --x-major 100.",
    )
    p.add_argument(
        "--x-minor",
        type=float,
        default=None,
        help="Manual x-axis minor tick spacing in seconds, e.g. --x-minor 20.",
    )
    p.add_argument(
        "--xmax",
        type=float,
        default=None,
        help="Manual x-axis maximum in seconds. Default: automatic.",
    )
    p.add_argument(
        "--title",
        type=str,
        default=None,
        help="Optional plot title.",
    )
    p.add_argument(
        "--show",
        action="store_true",
        help="Open an interactive Matplotlib window in addition to saving the plot.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.qtar <= 0.0:
        print("ERROR: --qtar must be positive.", file=sys.stderr)
        return 2

    if args.x_major is not None and args.x_major <= 0.0:
        print("ERROR: --x-major must be positive.", file=sys.stderr)
        return 2

    if args.x_minor is not None and args.x_minor <= 0.0:
        print("ERROR: --x-minor must be positive.", file=sys.stderr)
        return 2

    if args.xmax is not None and args.xmax <= 0.0:
        print("ERROR: --xmax must be positive.", file=sys.stderr)
        return 2

    if not args.sortie.exists():
        print(f"ERROR: sortie file not found: {args.sortie}", file=sys.stderr)
        return 2

    records = parse_sortie(args.sortie)

    if not records:
        print(
            "ERROR: no boundary flux records found in the .sortie file.\n"
            "Expected lines similar to: FLUX BOUNDARY 2: 178.2000 M3/S",
            file=sys.stderr,
        )
        return 1

    if args.csv is not None:
        write_csv(records, args.csv, args.qtar)

    plot_convergence(
        records=records,
        qtar=args.qtar,
        output=args.output,
        title=args.title,
        tol=args.tol,
        consecutive=args.consecutive,
        mark_convergence=args.mark_convergence,
        show=args.show,
        x_major=args.x_major,
        x_minor=args.x_minor,
        xmax=args.xmax,
    )

    q_in = [r.q_in for r in records]
    q_out = [r.q_out for r in records]
    i_in = first_converged_index(q_in, args.qtar, args.tol, args.consecutive)
    i_out = first_converged_index(q_out, args.qtar, args.tol, args.consecutive)

    print(f"Parsed listing printouts with boundary fluxes: {len(records)}")
    print(f"Saved plot: {args.output}")

    if args.csv is not None:
        print(f"Saved CSV: {args.csv}")

    if i_in is not None:
        print(
            f"Inflow reaches tolerance {args.tol:g} for {args.consecutive} "
            f"consecutive printouts at t = {records[i_in].time_s:g} s "
            f"(listing index {records[i_in].listing_index})."
        )
    else:
        print(
            f"Inflow does not reach tolerance {args.tol:g} for "
            f"{args.consecutive} consecutive printouts."
        )

    if i_out is not None:
        print(
            f"Outflow reaches tolerance {args.tol:g} for {args.consecutive} "
            f"consecutive printouts at t = {records[i_out].time_s:g} s "
            f"(listing index {records[i_out].listing_index})."
        )
    else:
        print(
            f"Outflow does not reach tolerance {args.tol:g} for "
            f"{args.consecutive} consecutive printouts."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())



