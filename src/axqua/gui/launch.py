"""Console entry point that launches the Streamlit config editor.

``axqua-gui`` simply runs ``streamlit run`` on the bundled ``app.py`` so the
user does not need to know the file's path. Extra arguments are forwarded to
Streamlit (e.g. ``axqua-gui --server.port 8600``).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    try:
        from streamlit.web import cli as stcli
    except ModuleNotFoundError:
        sys.stderr.write(
            "Streamlit is not installed. Install the GUI extra with:\n"
            "    pip install -e \".[gui]\"\n"
        )
        return 1

    app = str(Path(__file__).with_name("app.py"))
    sys.argv = ["streamlit", "run", app, *sys.argv[1:]]
    return stcli.main()


if __name__ == "__main__":
    raise SystemExit(main())
