#!/usr/bin/env bash
# Server half of deploy/backup.sh. Runs as root from
# /tmp/conference-drawing-<commit>-backup-<random>/remote/. Prints the tarball's path as its
# last line; the laptop downloads it and then deletes the directory.
#
# Root reads the app user's directory here, so it refuses a database or media
# directory that is a symbolic link (tar stores links inside media/ as links).
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root
no_symlink "$DATA_DIR"
no_symlink "$DATA_DIR/db.sqlite3"
no_symlink "$DATA_DIR/media"
[ -f "$DATA_DIR/db.sqlite3" ] || die "no database at $DATA_DIR/db.sqlite3"
umask 077
out=$(mktemp -d /tmp/conference-drawing-backup.XXXXXXXX)

take_lock 300
sqlite3 "$DATA_DIR/db.sqlite3" ".backup '$out/db.sqlite3'"
result=$(sqlite3 "$out/db.sqlite3" "PRAGMA integrity_check")
[ "$result" = ok ] || die "the backup copy failed integrity_check: $result"
# db.sqlite3 comes from $out, media/ from the data directory.
if [ -d "$DATA_DIR/media" ]; then
	tar -czf "$out/backup.tar.gz" -C "$out" db.sqlite3 -C "$DATA_DIR" media
else
	tar -czf "$out/backup.tar.gz" -C "$out" db.sqlite3
fi
rm -f "$out/db.sqlite3"
printf '%s\n' "$out/backup.tar.gz"
