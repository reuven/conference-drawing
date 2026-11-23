#!/usr/bin/env bash
# Reset the owner's admin password on the server:
#
#   deploy/owner-password.sh <host> <username>
#
# Stages remote/owner_password.sh like backup.sh does, then runs it over
# ssh -t so Django's changepassword asks for the new password at the server's
# own prompt. The password never passes through this script or any argument.
set -euo pipefail
# shellcheck source=deploy/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

[ $# -eq 2 ] || {
	echo "usage: deploy/owner-password.sh <host> <username>" >&2
	exit 2
}
set_target "$1"
valid_username "$2" || die "a username is letters, digits and @.+-_ only"
user=$2

local_commit
RDIR=""
trap '[ -z "$RDIR" ] || unstage' EXIT
stage owner-password "$DEPLOY/remote"
say "resetting the password for $user on $HOST"
remote_tty "bash $RDIR/remote/owner_password.sh $user"
