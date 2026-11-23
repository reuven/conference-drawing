#!/usr/bin/env bash
# Reset the owner's admin password: owner_password.sh <username>
# Django's changepassword prompts for the new password on the terminal ssh -t
# gave us; it never appears in an argument, the environment or a log.
set -euo pipefail
# shellcheck source=deploy/remote/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

require_root
[ $# -eq 1 ] || die "usage: owner_password.sh <username>"
valid_username "$1" || die "a username is letters, digits and @.+-_ only"
# "--" so a name can never be read as an option.
run_app changepassword -- "$1"
