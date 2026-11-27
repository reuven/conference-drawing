# Setting up a server

From nothing to a running drawing site on the internet. Once it is up, the
[admin guide](admin-guide.md) covers running conferences on it.

One install serves one owner (you) for as many conferences as you like: you
wrap up one and add the next in the same admin. You never need to take it
down between conferences.

## What you need

- **A server** running Ubuntu with systemd and [Caddy](https://caddyserver.com/)
  serving the web. If you don't have one, step 1 sets one up. A server that
  already hosts other sites behind Caddy is fine: the install adds one site
  file and touches nothing else.
- **A domain name** for the site, such as `drawing.example.com`, whose DNS you
  can change.
- **Your laptop**, with `git`, `ssh` access as root to the server, and
  [uv](https://docs.astral.sh/uv/). Every command below runs on the laptop
  unless it says otherwise. The laptop needs no database: the app is built
  there and runs on the server.

## 1. Get a server (skip if you have one)

A small DigitalOcean Droplet is enough: the $6 plan works. Choose Ubuntu 24.04
and add your ssh key. Then ssh in as root and install Caddy from its official
apt repository:

```sh
apt install --yes debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
chmod o+r /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install --yes caddy
```

## 2. Let the app add its own Caddy site

On the server, as root, add this line to Caddy's main file (once), check it,
and reload:

```sh
echo 'import /etc/caddy/sites/*.caddy' >> /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl reload caddy
```

The server also needs a `caddy` group, which Caddy's package creates.

## 3. Point the domain at the server

Create a DNS **A** record for your domain with the server's address (and an
**AAAA** record if the server has IPv6). The address may be one on the
server's network interfaces, or one that the server's own host name resolves
to, so a DigitalOcean Reserved IP or another provider's floating IP works.

Watch out for a **wildcard** record (`*.example.com`) at your DNS provider: it
can answer for your new name before your own record exists, pointing it at
the wrong machine. The install checks the DNS and refuses until every A and
AAAA record points at the server.

## 4. Get the code on your laptop

```sh
git clone https://github.com/reuven/conference-drawing.git
cd conference-drawing
uv sync
```

The install builds whatever this checkout holds, so it must be clean (no
uncommitted changes). To install a particular release, check out its tag,
such as `git checkout v0.3.0`.

## 5. Optional: connect Drip

The app hands entrants to [Drip](https://www.drip.com/), which sends the
emails. To use it, put your Drip account id (digits only) and API key in a
file named `.env` in the checkout:

```sh
DRIP_ACCOUNT_ID=1234567
DRIP_API_KEY=your-key-here
```

The install copies both to the server. Set both or neither. The workflows you
build in Drip, and exactly what the app sends there, are in
[Setting up Drip](drip-setup.md).

Without Drip the app still works: digital prizes show their code and URL on
your drawing screen and in the CSV export for you to hand over, and wrap-up
only locks the conference.

## 6. Install

```sh
deploy/up.sh <server> <domain>
```

For example `deploy/up.sh 203.0.113.7 drawing.example.com`.

It checks the server first and refuses, changing nothing, if a systemd unit
has failed, Caddy's configuration is invalid, or the DNS doesn't point at the
server. Then it:

1. builds the app from your checkout;
2. asks for the **owner's** username, email address, and password (the
   password isn't shown as you type, and never appears on a command line);
3. installs it under `/srv/conference-drawing/`, with PostgreSQL if the server
   has none (see [PostgreSQL on the server](#postgresql-on-the-server));
4. writes one Caddy site file, which also gets the site its HTTPS
   certificate;
5. checks `/health`, `/version` and `/privacy/` over HTTPS.

Stay connected until it finishes. If the connection drops, the install puts
the running services back and records what happened in
`/var/log/conference-drawing-install.log` on the server; read that, then run
`up.sh` again.

## 7. Check it and sign in

- `https://<domain>/version` shows the version and commit you installed.
- `https://<domain>/` shows a short page with your name (once you set it) and
  how to enter.
- `https://<domain>/admin/` is the admin. Sign in with the owner's username or
  email address.

In the admin, open **Install settings** first: your name, the privacy text,
and the after-conference page. Then go on to the
[admin guide](admin-guide.md).

## Upgrading

To install a newer version, update the checkout and run the same command:

```sh
git checkout main && git pull     # or: git checkout v0.3.0
deploy/up.sh <server> <domain>
```

An upgrade asks for no password. It builds the new version beside the
running one, so the site keeps serving while it builds; then it stops the
app for a moment, takes a database snapshot, updates the database, switches
to the new version, and starts the app again. Some releases sign everyone
out of the admin once (0.2 did, because the login method changed).

**Rolling back.** The previous version stays on the server for one release.
Database changes in this app only ever add, so the previous version runs on
the new database.

From 0.3 back to 0.2.x, first deal with the Helpers lists. They stay in the
database and 0.2 doesn't know about them, so after a plain rollback, deleting
a conference, or a user who is on a Helpers list, fails. While 0.3 is still
running, do one of these:

- In the admin, empty the **Helpers** list of every conference. 0.2's admin
  has no Helpers field, so this can't be done after the rollback.
- Or undo 0.3's database changes. This deletes every Helpers list, and
  upgrading to 0.3 again starts with them empty, so note who was on them
  first. As root, while `venv` still points at 0.3:

  ```sh
  systemctl stop conference-drawing-snapshot.timer conference-drawing-web.socket conference-drawing-web conference-drawing-drip
  cd / && runuser -u conference-drawing -- env \
    CD_ENV_FILE=/srv/conference-drawing/data/.env \
    CD_DATA_DIR=/srv/conference-drawing/data CD_DB_HOST=/var/run/postgresql \
    /srv/conference-drawing/venv/bin/conference-drawing migrate raffle 0008
  ```

Then, on the server, as root:

```sh
ls -d /srv/conference-drawing/venv-*        # the newest is current
ln -sfn venv-<previous> /srv/conference-drawing/venv
systemctl start conference-drawing-web.socket conference-drawing-web conference-drawing-drip conference-drawing-snapshot.timer
systemctl restart conference-drawing-web conference-drawing-drip
```

### Upgrading to 0.4

0.4 adds database columns: an `email_key` on each entrant (the lowercased
mailbox with any `+tag` removed, filled in for existing entrants) and the
per-network entry limit on each conference. Both are add-only, so 0.3 still
runs on the new database.

The units are also tighter (see
[Hardening the services](#hardening-the-services)), and on a PostgreSQL the
installer set up, PostgreSQL restarts once (see
[PostgreSQL on the server](#postgresql-on-the-server)). The app restarts with
it. After the first 0.4 install, check these, in order:

1. `systemctl status conference-drawing-web conference-drawing-drip` shows
   both active, and `systemctl --failed` lists nothing.
2. This prints nothing:

   ```sh
   journalctl -u conference-drawing-web -u conference-drawing-drip -u conference-drawing-snapshot --since "10 min ago" | grep -iE "seccomp|SIGSYS|bad system call|Operation not permitted|Address family"
   ```

3. Load the site, run a drawing and the wrap-up, and confirm the Drip outbox
   drains (`outbox_status.sh`) and Drip sends.
4. Start a snapshot once and check that it exited 0 and left a new file in
   `data/snapshots/`:

   ```sh
   systemctl start conference-drawing-snapshot.service
   ```

5. Look at the units' exposure scores:

   ```sh
   systemd-analyze verify /etc/systemd/system/conference-drawing-*.service
   systemd-analyze security conference-drawing-web.service
   ```

   Run the second command for the Drip and snapshot units too.
6. If a unit fails on a system call, `journalctl` (or
   `dmesg | grep -i audit`) names it. Roll back with the steps above.
7. On a server where the installer put PostgreSQL, this prints 1:

   ```sh
   grep -c log_min_error_statement /etc/postgresql/*/main/conf.d/conference-drawing.conf
   ```

   On a PostgreSQL you already had, set it by hand (see below).
8. After you rotate the Drip key, `.env` on the server has the key once, mode
   0600, owned by `conference-drawing`, and no `.env.XXXXXX` files beside it.

**Rolling back from 0.4 to 0.3.** Entrants created under 0.3, then carried
through a rollback and forward again, have no mailbox key. Exclusions still
match them by exact email, but `+` aliases of them are not caught.

## Changing a setting on the server

An upgrade only **adds** settings the server's `.env` lacks; it never changes
one that is already there. So a new Drip key, or a move to another domain,
has to be edited on the server and the app restarted:

```sh
ssh you@server
sudo -u conference-drawing "${EDITOR:-nano}" /srv/conference-drawing/data/.env
#   change DRIP_API_KEY / DRIP_ACCOUNT_ID, or DJANGO_ALLOWED_HOSTS for a new domain
sudo systemctl restart conference-drawing-web conference-drawing-drip
```

A new domain also needs a new Caddy site file, so after editing `.env`, run
`up.sh` with the new domain.

## Passwords and logins

The admin login accepts the username, or the email address in any case
(usernames are case-sensitive). If two accounts share an email address, that
address can't be used to sign in, but each can still sign in by username. The
admin only lets in staff accounts. Booth helpers are ordinary accounts that
sign in at `/booth/login/` instead (see the
[admin guide](admin-guide.md#helpers)).

To reset a password, for the owner or any helper:

```sh
deploy/owner-password.sh <server> <username>
```

It runs over `ssh -t`, so it needs a terminal: you type the new password at
the server's own prompt, and it never passes through the script. A username
may not start with `-`.

## Backups

The server takes a database snapshot every hour, and one before each upgrade,
and keeps them in `/srv/conference-drawing/data/snapshots/`. Snapshots rotate
by age and by count: dumps older than 48 hours are deleted before each new
one, and so are stale partial dumps from a run that died. With an hourly timer
the oldest snapshot can be up to about 49 hours old. They still exist for that
long after you delete entrants.

To keep a copy on your laptop:

```sh
deploy/backup.sh <server>
```

It saves a tarball (database and uploaded images) in `backups/` in the
checkout, which Git ignores, with mode 0600. Backups are **not** encrypted:
keep that folder on an encrypted disk. They hold every entrant who hadn't been
deleted when the backup was taken (see the
[privacy notes](admin-guide.md#privacy-and-personal-data)), and nothing ever
removes old ones for you.

**Starting a new server from a backup.** `--restore` loads a backup into a
**fresh** install (run `down.sh` first if the app is already there), so the
new site starts with the old one's books, prizes, and codes:

```sh
deploy/up.sh <server> <domain> --restore backups/<file>.tar.gz
```

A backup is a full database dump, so restoring one also brings back **every
entrant who wasn't deleted before it was taken**, with their names, emails,
answers, and consent records. Delete old conferences' entrants first, or
restore a backup taken after you did.

## PostgreSQL on the server

The app needs PostgreSQL 15 or later and talks to it only over the local
socket, as a role named `conference-drawing` with no password.

- If the server has **no** PostgreSQL, the install adds Ubuntu's package and
  gives it a small configuration (socket only, little memory).
- If the server **already has** PostgreSQL, the install never installs,
  reconfigures, or restarts it. It only creates its own role and database
  (refusing a `conference_drawing` database that someone else owns), and
  warns you if that PostgreSQL listens on the network.

The app's error log can quote the statement that failed, and a statement can
hold an entrant's name or email. So the app wants `log_min_error_statement =
panic`. On a PostgreSQL the installer set up, its drop-in file has the
setting, and PostgreSQL restarts once on the 0.4 upgrade, when the drop-in
gains it. On a PostgreSQL you already had, the installer prints a hint and
changes nothing: add `log_min_error_statement = panic` yourself.

## Hardening the services

The systemd units run with a small set of powers:

- **Web:** unix sockets only (`AF_UNIX`). It talks to PostgreSQL and to
  Gunicorn's socket, and nothing else.
- **Drip worker:** unix sockets, plus IP (`AF_INET`, `AF_INET6`) to reach
  Drip's API.
- **Snapshot:** unix sockets only.
- **All three:** `CapabilityBoundingSet=` (no capabilities),
  `SystemCallFilter=@system-service`, and `RestrictSUIDSGID=yes`. The web and
  Drip units also have `ProtectProc=invisible`. The snapshot unit does not.

## Logs

- The app's own logs (`journalctl -u conference-drawing-web`, and `-drip` for
  the Drip worker) carry ids and counts only: never names, emails, answers, or
  codes. Display addresses are replaced by `REDACTED`, and an error's
  traceback names each exception's type without its message.
- Caddy keeps an access log for this site in
  `/var/log/caddy/conference-drawing.access.log`. It records each visitor's IP
  address and the page they asked for, with display tokens replaced by
  `REDACTED`, query strings dropped, and no headers. It is rotated and kept
  for up to 14 days, also after you delete entrants.
- Django's security errors are logged at WARNING. If you ever set `ADMINS`,
  they are still not emailed.

## Retiring the install

Only if you're done with conferences for good:

```sh
deploy/down.sh <server>
```

It refuses while any Drip job is still pending. Then it stops the app, takes a
final backup to your laptop, and removes the services, the Caddy site file
(and its access log), the database and its role, and
`/srv/conference-drawing`. It removes PostgreSQL itself only if the install
put it there **and** it holds nothing but the stock databases and roles;
otherwise it leaves PostgreSQL alone and prints the command to remove it by
hand. Delete entrants **before** running it if you don't want them in that
final backup.

A server still running a build from before the switch to PostgreSQL needs
`deploy/down.sh` and then a fresh `deploy/up.sh`.

## Configuration reference

Settings come from real environment variables first, then from `.env` in the
project directory (or the file named by `CD_ENV_FILE`). The app never reads
`~/.env`. On a server, the install writes `/srv/conference-drawing/data/.env`
with mode 0600.

| Variable | Default | What it does |
|---|---|---|
| `DJANGO_SECRET_KEY` | none | Required unless `DJANGO_DEBUG` is on. `init` generates it. Debug mode never uses a fixed key: it makes a random one for each process. |
| `CD_TRUSTED_PROXIES` | empty | Comma-separated IP addresses of proxies you trust. The app honors `X-Forwarded-For` only from the unix socket (Caddy) or from these addresses. An invalid value stops startup. |
| `DJANGO_ALLOWED_HOSTS` | empty | Comma-separated hostnames the site answers to. `init --domain` writes it. In debug, `localhost`, `127.0.0.1` and `[::1]` are always accepted too. |
| `DJANGO_DEBUG` | off | `1` for local development only. `up.sh` refuses to install if the server's `.env` turns it on (1, true, yes, or on). |
| `CD_DB_NAME` | `conference_drawing` | PostgreSQL database name. |
| `CD_DB_USER` | `conference-drawing` | PostgreSQL role. |
| `CD_DB_HOST` | empty (the local socket) | Host or socket directory. Homebrew: `/tmp`. |
| `CD_DB_PORT` | empty | Port, if not the default. |
| `CD_DB_PASSWORD` | empty | Password. Leave empty for socket login; for a TCP setup only. |
| `CD_DATA_DIR` | `var/` in the project | Where static files and uploaded covers and logos live. |
| `CD_ENV_FILE` | `.env` in the project | The file to read settings from. |
| `CD_VERSION_FILE` | none | A JSON file the deploy writes; the footer and `/version` show it. |
| `DRIP_ACCOUNT_ID` | none | Your Drip account id (digits). Set both Drip values or neither. |
| `DRIP_API_KEY` | none | Your Drip API key. Without both, the app runs without Drip. |
| `DJANGO_SUPERUSER_USERNAME`, `_EMAIL`, `_PASSWORD` | none | Only for `init --non-interactive`; the deploy passes them to the server over ssh. |

`init` takes `--env-file`, `--domain HOST...`, and `--non-interactive`.
