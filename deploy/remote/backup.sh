#!/usr/bin/env bash
# Server half of deploy/backup.sh. Runs as root from
# /tmp/conference-drawing-<commit>/remote/. Prints the tarball's path as its
# last line; the laptop downloads it and then deletes the directory.
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root
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
# A fingerprint of what was backed up. (PRAGMA data_version only counts
# commits seen by one open connection, so it cannot compare two runs.)
printf 'backed_up_at=%s\nsha256=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
	"$(sha256sum "$out/db.sqlite3" | cut -d' ' -f1)" >"$DATA_DIR/last_backup"
chown "$APP_USER:" "$DATA_DIR/last_backup"
rm -f "$out/db.sqlite3"
printf '%s\n' "$out/backup.tar.gz"
