#!/usr/bin/env bash
# Hourly snapshot of the database (conference-drawing-snapshot.timer). Runs as
# the app user, which reaches its own database by peer auth. pg_dump -Fc reads
# one consistent MVCC snapshot while the app writes; pg_restore -f /dev/null
# then reads every block of the dump back (pg_restore --list would read only
# its table of contents). Keeps the newest 48 in data/snapshots/, and none
# older than 48 hours: the age rotation runs before the dump, so a dump that
# keeps failing cannot keep old (possibly deleted) entrants alive.
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

# Age rotation first. -P never follows a symlink, -maxdepth 1 stays in this
# directory, -type f touches only regular files.
find -P "$snapshots" -maxdepth 1 -type f -name 'db-*.dump' -mmin +2880 -delete

name="db-$(date -u +%Y%m%dT%H%M%SZ).dump"
partial="$snapshots/$name.partial"
trap 'rm -f "$partial"' EXIT
rm -f "$partial"
pg_dump -Fc --file "$partial" --dbname="$DB_NAME"
if ! pg_restore -f /dev/null "$partial"; then
	rm -f "$partial"
	die "the snapshot failed verification (pg_restore could not read it back)"
fi
mv "$partial" "$snapshots/$name"

# Names sort by time, and globs sort by name: drop all but the last $KEEP.
files=("$snapshots"/db-*.dump)
count=${#files[@]}
if [ "$count" -gt "$KEEP" ]; then
	rm -f -- "${files[@]:0:count-KEEP}"
fi
