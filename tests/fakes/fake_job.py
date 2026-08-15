"""A long-lived job that spawns a grandchild - the tree-kill test subject.

The grandchild is the point: killing only the process you launched leaves it running,
which is the exact failure mode process-group cancellation exists to prevent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

if __name__ == "__main__":
    marker = sys.argv[1] if len(sys.argv) > 1 else None
    child = subprocess.Popen([sys.executable, "-c",
                              "import time\nwhile True: time.sleep(0.2)"])
    if marker:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"{os.getpid()}\n{child.pid}\n")
    while True:
        time.sleep(0.2)
