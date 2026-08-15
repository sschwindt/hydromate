#!/usr/bin/env bash
# The OpenFOAM counterpart: all three sentinels, or validate() rejects it.
export WM_PROJECT_DIR="${WM_PROJECT_DIR_FAKE:-/opt/fake-openfoam}"
export WM_PROJECT_VERSION="9"
export FOAM_APPBIN="$WM_PROJECT_DIR/platforms/linux64/bin"
export PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bin:$PATH"
