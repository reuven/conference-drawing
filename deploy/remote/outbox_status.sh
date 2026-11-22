#!/usr/bin/env bash
# Print the Drip outbox counts; exit 1 while any job is pending (down.sh).
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root
run_app outbox_status
