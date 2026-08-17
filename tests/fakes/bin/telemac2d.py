#!/usr/bin/env python3
"""A stand-in for TELEMAC's ``telemac2d.py`` / ``telemac3d.py`` launcher.

Python, not shell, because axqua runs the launcher **through an interpreter**
(``python "$(command -v telemac2d.py)" case.cas``) - so a shell fake would be handed to
python and fail with a syntax error, testing nothing.

The listing is real-shaped: ``axqua.progress.SolverProgress`` parses the
``ITERATION n TIME: ... ( n S)`` header and ``axqua.sortie`` reads the volume-balance
block, so a fake printing anything else would leave both parsers untested.

    FAKE_STEPS  iterations to emit  (default 5)
    FAKE_SLEEP  seconds per step    (default 0)
    FAKE_RC     exit code           (default 0)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

DT = 25.0


def main() -> int:
    cas = next((a for a in sys.argv[1:] if a.endswith(".cas")), None)
    ncsize = next((a.split("=", 1)[1] for a in sys.argv[1:]
                   if a.startswith("--ncsize=")), "1")
    steps = int(os.environ.get("FAKE_STEPS", "5"))
    nap = float(os.environ.get("FAKE_SLEEP", "0"))
    rc = int(os.environ.get("FAKE_RC", "0"))

    print(f"FAKE TELEMAC: {cas or '<no steering file>'} on {ncsize} process(es)",
          flush=True)
    for i in range(1, steps + 1):
        t = i * DT
        print(f"     ITERATION   {i * 100:6d}    TIME:      0 H "
              f"{int(t // 60):2d} MIN  {t % 60:.4f} S   ( {t:15.4f} S)", flush=True)
        print("         BALANCE OF WATER VOLUME", flush=True)
        print(f"         RELATIVE ERROR IN VOLUME AT T = {t:12.4f} S :  0.1000E-08",
              flush=True)
        if nap:
            time.sleep(nap)

    if cas:
        case = Path(cas)
        # The artifacts a real run leaves behind, so the postprocessing path has
        # something to find rather than being skipped silently.
        (case.parent / "r2d.slf").touch()
        (case.parent / f"{case.stem}_fake.sortie").write_text(
            "FAKE LISTING\n", encoding="utf-8")
    print(f"FAKE TELEMAC: done (rc={rc})", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
