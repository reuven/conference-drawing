#!/usr/bin/env bash
# Server half of deploy/backup.sh. Runs as root from
# /tmp/conference-drawing-<commit>-backup-<random>/remote/. Prints the tarball's path as its
# last line; the laptop downloads it and then deletes the directory.
#
# Root never opens a file in the app user's directory: the app user makes the
# sqlite3 .backup copy (in its own snapshots/ directory), checks it, and tars
# it with media/ to stdout; root only writes that stream into its own 0700
# temp directory. (tar stores links inside media/ as links.)
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root
no_symlink "$DATA_DIR"
[ -e "$DATA_DIR/db.sqlite3" ] || die "no database at $DATA_DIR/db.sqlite3"
umask 077
out=$(mktemp -d "$ROOT/tmp/conference-drawing-backup.XXXXXXXX")
copy="$DATA_DIR/snapshots/backup"

take_lock 300
as_app rm -rf -- "$copy"
as_app mkdir -m 0700 "$copy"
as_app sqlite3 "$DATA_DIR/db.sqlite3" ".backup '$copy/db.sqlite3'"
result=$(as_app sqlite3 "$copy/db.sqlite3" "PRAGMA integrity_check")
[ "$result" = ok ] || die "the backup copy failed integrity_check: $result"
media=()
[ ! -d "$DATA_DIR/media" ] || [ -L "$DATA_DIR/media" ] || media=(-C "$DATA_DIR" media)
as_app tar -czf - -C "$copy" db.sqlite3 "${media[@]+"${media[@]}"}" >"$out/backup.tar.gz"
as_app rm -rf -- "$copy"
printf '%s\n' "$out/backup.tar.gz"
