#!/usr/bin/env bash
#
# Mirror cases/*/user-sources/ and cases/*/hydromate-case/ to Google Drive via rclone.
#
# One-time setup (not done by this script):
#   sudo apt install rclone
#   rclone config          # create a remote named "gdrive" (OAuth in browser)
#   rclone lsd gdrive:     # sanity check
#
# Usage:
#   ./scripts/backup_cases_to_drive.sh          # dry run: prints what would change
#   ./scripts/backup_cases_to_drive.sh --run    # actually syncs (uploads AND deletes
#                                                #   remote-only files, mirroring local)
#
# Remote layout: gdrive:HydroMate-Cases/<case-name>/user-sources/
#                gdrive:HydroMate-Cases/<case-name>/hydromate-case/
# Restore a case with: rclone copy gdrive:HydroMate-Cases/<case-name> cases/<case-name>

set -euo pipefail

REMOTE="gdrive"
REMOTE_ROOT="HydroMate-Cases"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CASES_DIR="${REPO_ROOT}/cases"
LOG_DIR="${REPO_ROOT}/scripts/backup-logs"

DRY_RUN=1
if [[ "${1:-}" == "--run" ]]; then
    DRY_RUN=0
fi

if ! command -v rclone >/dev/null 2>&1; then
    echo "rclone is not installed. Run: sudo apt install rclone" >&2
    exit 1
fi

if ! rclone listremotes | grep -qx "${REMOTE}:"; then
    echo "rclone remote '${REMOTE}' is not configured. Run: rclone config" >&2
    exit 1
fi

mkdir -p "${LOG_DIR}"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
LOG_FILE="${LOG_DIR}/backup-${TIMESTAMP}.log"

RCLONE_FLAGS=(
    --exclude "__pycache__/**"
    --exclude "*.pyc"
    --exclude ".DS_Store"
    --transfers=8
    --checkers=16
    --drive-chunk-size=64M
    --fast-list
    --progress
    --log-file "${LOG_FILE}"
)

if [[ "${DRY_RUN}" -eq 1 ]]; then
    RCLONE_FLAGS+=(--dry-run)
    echo "DRY RUN (no changes will be made). Pass --run to actually sync."
fi

echo "Log: ${LOG_FILE}"

for case_dir in "${CASES_DIR}"/*/; do
    case_name="$(basename "${case_dir}")"

    # Skip the data-free template and non-case entries (e.g. gauge_data.py, __pycache__).
    if [[ "${case_name}" == "case-template" || "${case_name}" == "__pycache__" ]]; then
        continue
    fi
    if [[ ! -d "${case_dir}user-sources" && ! -d "${case_dir}hydromate-case" ]]; then
        continue
    fi

    for sub in user-sources hydromate-case; do
        local_path="${case_dir}${sub}"
        if [[ -d "${local_path}" ]]; then
            echo ""
            echo "==> Syncing ${case_name}/${sub}"
            rclone sync "${local_path}" "${REMOTE}:${REMOTE_ROOT}/${case_name}/${sub}" "${RCLONE_FLAGS[@]}"
        fi
    done
done

echo ""
if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "Dry run complete. Review the plan above, then rerun with --run to apply it."
else
    echo "Backup complete."
fi
