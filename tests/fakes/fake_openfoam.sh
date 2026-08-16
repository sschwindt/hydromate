#!/usr/bin/env bash
# Stand-in for the OpenFOAM utilities, dispatching on the tool name so one script
# covers checkMesh / decomposePar / interFoam / reconstructPar / foamToVTK.
set -u
TOOL="$(basename "${0}")"
[ "$TOOL" = "fake_openfoam.sh" ] && TOOL="${1:-interFoam}"
RC="${FAKE_RC:-0}"; STEPS="${FAKE_STEPS:-5}"; SLEEP="${FAKE_SLEEP:-0}"
case "$TOOL" in
  checkMesh)
    echo "Checking geometry..."; echo "    Mesh OK."; echo "End" ;;
  decomposePar) echo "Decomposing mesh"; echo "End" ;;
  reconstructPar) echo "Reconstructing fields"; echo "End" ;;
  foamToVTK) mkdir -p VTK; : > VTK/case.pvd; echo "End" ;;
  *)
    for i in $(seq 1 "$STEPS"); do
      echo "Courant Number mean: 0.05 max: 0.42"
      echo "Interface Courant Number mean: 0.01 max: 0.31"
      echo "deltaT = 0.0031"
      echo "Time = $(awk "BEGIN{print $i*0.5}")"
      [ "$SLEEP" != "0" ] && sleep "$SLEEP"
    done
    echo "End" ;;
esac
exit "$RC"
