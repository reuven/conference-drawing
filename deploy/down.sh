#!/usr/bin/env bash
# Tear conference-drawing down after a conference, keeping a backup:
#
#   deploy/down.sh <host>
#
# In order, stopping at the first failure:
#   1. refuse while any Drip outbox job is pending (outbox_status exits 1);
#   2. stop the web app and the snapshot timer, check the outbox once more
#      (nothing new can be queued now), then stop the Drip worker;
#   3. run backup.sh and check the tarball arrived here and is not empty;
#   4. run remote/uninstall.sh: units, the Caddy site file (validate, then
#      reload), /srv/conference-drawing, and the system user.
#
# Because the app is stopped before the backup, nothing can be written
# between the backup and the removal.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ $# -eq 1 ] || {
	echo "usage: deploy/down.sh <host>" >&2
	exit 2
}
set_target "$1"
local_commit

stage "$DEPLOY/remote"
say "checking the Drip outbox on $HOST"
remote "bash $RDIR/remote/outbox_status.sh" >&2 ||
	die "Drip jobs are still pending on $HOST; wait for the worker to send them, then run down.sh again"
say "stopping conference-drawing on $HOST"
remote "bash $RDIR/remote/stop.sh" >&2

tarball=$("$DEPLOY/backup.sh" "$HOST" | tail -n 1)
[ -s "$tarball" ] || die "backup.sh did not leave a backup here; nothing was removed"
say "backup kept at $tarball"

# backup.sh removed its staging directory, so stage again for uninstall.
stage "$DEPLOY/remote"
say "uninstalling from $HOST"
remote "bash $RDIR/remote/uninstall.sh" >&2
unstage
say "conference-drawing is gone from $HOST; the backup is $tarball"
