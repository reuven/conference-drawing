#!/usr/bin/env bash
# Hourly snapshot of the database (conference-drawing-snapshot.timer). Runs as
# the app user. Copies db.sqlite3 with sqlite3 .backup (consistent while the
# app writes), checks it, and keeps the newest 48 in data/snapshots/.
#
# So a deleted entrant can live on in a snapshot for up to 48 hours after the
# owner deletes entrants; the privacy page says so.
#
# install.sh also runs it just before migrate, while holding the maintenance
# lock itself (CD_LOCK_HELD=1).
#
# Installed to /srv/conference-drawing/bin/snapshot.sh, next to common.sh.
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

KEEP=48
snapshots="$DATA_DIR/snapshots"
umask 077
mkdir -p "$snapshots"
[ "${CD_LOCK_HELD:-}" = 1 ] || take_lock 600

name="db-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
partial="$snapshots/$name.partial"
trap 'rm -f "$partial"' EXIT
rm -f "$partial"
sqlite3 "$DATA_DIR/db.sqlite3" ".backup '$partial'"
result=$(sqlite3 "$partial" "PRAGMA integrity_check")
if [ "$result" != ok ]; then
	rm -f "$partial"
	die "snapshot failed integrity_check"
fi
mv "$partial" "$snapshots/$name"

# Names sort by time, and globs sort by name: drop all but the last $KEEP.
files=("$snapshots"/db-*.sqlite3)
count=${#files[@]}
if [ "$count" -gt "$KEEP" ]; then
	rm -f -- "${files[@]:0:count-KEEP}"
fi
