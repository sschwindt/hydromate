#!/usr/bin/env bash
# A TELEMAC setup script that exports the real sentinel (SENTINELS["telemac"]) and puts
# the fake launcher on PATH, so SolverEnvironment.capture()/validate() succeed in CI -
# a path that has never had end-to-end coverage.
export HOMETEL="${HOMETEL_FAKE:-/opt/fake-telemac}"
export SYSTELCFG="$HOMETEL/configs/systel.cfg"
_FAKE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$_FAKE_ROOT/bin:$PATH"
# TelemacRuntime.check_available proves the environment by importing TELEMAC's own
# SELAFIN reader in it, so the stand-in package has to be importable too.
export PYTHONPATH="$_FAKE_ROOT/pylib${PYTHONPATH:+:$PYTHONPATH}"
echo "FAKE TELEMAC environment set: $HOMETEL"
