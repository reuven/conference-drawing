# conference-drawing

A self-hosted Django app for running book raffles at a conference booth.
Visitors scan a QR code and enter on their phones; you run the drawings from
your own phone at the booth; afterward the entrants go to
[Drip](https://www.drip.com/) with tags, and Drip sends the follow-up email.

One install has one owner. Each exhibitor deploys their own copy, runs it for
one conference, and takes it down again.

What it does:

- Sets up a conference: dates, time zone, booth location, an exclusion list
  (you and your staff), optional entry questions, and a schedule of drawings
  with prize lines (a book, a quantity, physical or digital).
- Prints a flyer with a QR code, the schedule, and the "you must be present"
  line.
- Takes entries on a phone-friendly form, one entry per email address, with a
  consent record for each.
- Runs each drawing from a phone: fair random picks, Claimed / Not here for
  physical books with an immediate redraw, and digital prizes sent through
  Drip. An optional full-screen display view works for a booth monitor.
- After the conference, wraps up: every entrant goes to Drip with tags, and
  you can export a CSV and delete the entrants.

The Drip side is a pair of workflows you build in Drip itself; see
[`docs/drip-setup.md`](docs/drip-setup.md). The design is in
[`docs/superpowers/specs/`](docs/superpowers/specs/).

## Quick start (on your own computer)

You need [uv](https://docs.astral.sh/uv/), which installs Python 3.14 for you,
and a local PostgreSQL. The app uses PostgreSQL only.

1. Install PostgreSQL, version 15 or later, and start it. On a Mac with
   Homebrew:

   ```sh
   brew install postgresql@18
   brew services start postgresql@18
   ```

   Any other way of running PostgreSQL 15 or later works too.

2. Create an empty database for the app, owned by your own login:

   ```sh
   createdb conference_drawing
   ```

3. Get the code and install it:

   ```sh
   git clone https://github.com/reuven/conference-drawing.git
   cd conference-drawing
   uv sync
   ```

4. Tell the app how to reach the database. Homebrew's PostgreSQL listens on a
   socket in `/tmp` and lets your login in without a password. Put this in a
   file named `.env` in the project directory (replace `yourlogin` with the
   output of `whoami`):

   ```sh
   CD_DB_USER=yourlogin
   CD_DB_HOST=/tmp
   DJANGO_DEBUG=1
   ```

   `DJANGO_DEBUG=1` is for your own computer only: it lets the login cookie
   work over plain `http://localhost`. Never set it on a server.

5. Run the one-time setup, then start the server:

   ```sh
   uv run conference-drawing init --domain localhost
   uv run conference-drawing runserver
   ```

   `init` writes a secret key and the allowed host into `.env`, checks that it
   can reach the database, creates the tables, and asks you for the owner's
   username, email, and password. It is safe to run again; it only fills in
   what is missing.

6. Open <http://localhost:8000/admin/> to set things up (books, conference,
   drawings), and <http://localhost:8000/booth/> to run them.

Everything is configured by environment variables or that `.env` file; see
[Configuration](#configuration).

## Running a conference

1. **Books.** In the admin, add each book you will raffle: title, an optional
   cover image, an optional link. Books are reused across conferences.
2. **Conference.** Add a conference: name, a slug (it becomes the entry URL,
   `/c/<slug>/`, and the Drip tags, such as `pycon-us-2027`), start and end
   dates, the IANA time zone (for example `America/Denver`), the booth
   location, and the exclusion list: the email addresses of you and your
   staff, one per line. Excluded people can enter (to test the form) but can
   never win, and are never sent to Drip. The default entry questions are
   created for you; edit them in place.
3. **Drawings.** Add each drawing at a date and time in the conference's time
   zone. Give each one or more prize lines: a book, a quantity, and a kind.
   A digital line has a prize URL (it may contain `{code}`) and either a pool
   of unique codes, which you paste one per line, or one shared code. The app
   will not start a drawing whose pool has fewer unused codes than the
   quantity.
4. **Flyer.** From the conference page at `/booth/<slug>/`, open "Printable
   flyer" and print it or save it as a PDF from the browser. Optionally open
   "Booth display link" on a monitor. If that link leaks, select the
   conference in the admin and run "Rotate display token": the old link stops
   working, and the conference page shows the new one.
5. **At the booth.** Open `/booth/` on your phone. When a drawing's time
   comes, start it. Digital winners are announced at once and emailed through
   Drip. For each physical pick, tap Claimed if the person is there or Not
   here to draw a replacement. A mis-tap can be undone while the drawing is
   open, or after the claim that auto-finished it (that reopens the drawing),
   but never after wrap-up or after you close the drawing yourself. Close the
   drawing when every copy is claimed, or when you decide to stop.
6. **Wrap up.** After the last drawing, tap "Wrap up". It shows the counts,
   closes entries, and, if Drip is configured, queues every entrant for
   Drip (see Privacy notes below for what is sent). The Drip worker sends the
   queue in the background; the conference page shows how many jobs are
   pending or failed, with a "Retry failed" button.
7. **Export.** Download the CSV from the conference page. It holds names,
   emails, answers, entry times, what each person won, and the consent
   timestamps. It is personal data: keep it somewhere private.
8. **Delete.** Once Drip has everything (no jobs pending), "Delete entrants"
   removes the conference's personal data and keeps the drawings, prizes, and
   counts.
9. **Take the server down.** `deploy/down.sh <host>` backs the server up to
   your laptop and removes the app from the server (see below).

## Deploying to a server

The deploy scripts run from your laptop, in a clean Git checkout, and need
only ssh access as root to the server.

The server must be Ubuntu with systemd and [Caddy](https://caddyserver.com/)
already installed and serving, a `caddy` group, and a line in
`/etc/caddy/Caddyfile` that reads `import /etc/caddy/sites/*.caddy`. The
domain's DNS A (and AAAA) records must already point at the server.

```sh
deploy/up.sh <host> <domain> [--restore <backup.tar.gz>]
```

`up.sh` checks the server first and refuses if a systemd unit has failed,
Caddy's configuration is invalid, or the DNS does not point at the host. It
then builds this checkout, installs it under `/srv/conference-drawing/`, runs
`init`, writes one Caddy site file, and checks `/health`, `/version`, and
`/privacy/` over HTTPS. On the first install it asks for the owner's username,
email, and password. If your checkout's `.env` has `DRIP_API_KEY` and
`DRIP_ACCOUNT_ID`, they are copied to the server. Run it again to upgrade; a
snapshot of the database is taken before each migration.

An upgrade only **adds** keys the server's `.env` lacks; it never changes one
that is already there. So rotating the Drip key, or moving to another domain,
through `up.sh` does nothing to those settings. To change an existing key,
edit the server's file by hand and restart the app:

```sh
ssh you@server
sudo -u conference-drawing "${EDITOR:-nano}" /srv/conference-drawing/data/.env
#   change DRIP_API_KEY / DRIP_ACCOUNT_ID, or DJANGO_ALLOWED_HOSTS for a new domain
sudo systemctl restart conference-drawing-web conference-drawing-drip
```

A new domain also needs a new Caddy site file, so for that, run `up.sh` with
the new domain after editing `.env`.

`--restore` loads a backup into a fresh install, so a new conference starts
with the last one's books, prizes, and codes. It needs a fresh install, so run
`down.sh` first if one is there. A backup is a full database dump: restoring
one brings back **every entrant who was not deleted before `down.sh` took
it**, with their names, emails, answers, and consent records. Delete the old
conference's entrants first (see "Backups still hold what you deleted" below),
or restore from a backup taken after you did.

The other operator commands:

```sh
deploy/backup.sh <host>   # save a backup to backups/ on this laptop
deploy/down.sh <host>     # back up, then remove everything from the server
```

`down.sh` refuses while any Drip job is still pending. It stops the app,
takes a final backup, then removes the services, the Caddy site file, the
database and its role, and `/srv/conference-drawing`. Backups are tarballs in
`backups/` (git-ignored, mode 0600). They are **not** encrypted; keep that
directory on an encrypted disk.

If a server is still running an older build from before the PostgreSQL
switch, run `deploy/down.sh <host>` and then a fresh `deploy/up.sh`.

### PostgreSQL on the server

The app needs PostgreSQL 15 or later on the server and talks to it only over
the local socket, as a role named `conference-drawing` with no password.

- If the server has **no** PostgreSQL at all, `up.sh` installs Ubuntu's
  package and gives it a small configuration (socket only, little memory).
- If the server **already has** PostgreSQL, the install never installs,
  reconfigures, or restarts it. It only creates its own role and database
  (refusing a `conference_drawing` database that someone else owns), and
  warns you if that PostgreSQL listens on the network.
- `down.sh` always drops the app's own database and role. It removes
  PostgreSQL itself only if the install put it there **and** it holds nothing
  but the stock databases and roles. Otherwise it leaves PostgreSQL in place
  and prints the command to remove it by hand.

### No server?

A small DigitalOcean Droplet is enough (the $6 plan works; choose Ubuntu 24.04
and add your ssh key).

1. Point your domain's A record at the Droplet's address.
2. ssh in as root and install Caddy from its official apt repository:

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

3. Add the line that lets the app add its own site, then check and reload:

   ```sh
   echo 'import /etc/caddy/sites/*.caddy' >> /etc/caddy/Caddyfile
   caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   systemctl reload caddy
   ```

4. From your laptop, run `deploy/up.sh <droplet-address> <your-domain>`.

Delete the Droplet when the conference is over, or leave it for the next one.

## Configuration

Settings come from real environment variables first, then from `.env` in the
project directory (or the file named by `CD_ENV_FILE`). The app never reads
`~/.env`. On a server, `init` writes `/srv/conference-drawing/data/.env`
with mode 0600.

| Variable | Default | What it does |
|---|---|---|
| `DJANGO_SECRET_KEY` | none | Required unless `DJANGO_DEBUG` is on. `init` generates it. |
| `DJANGO_ALLOWED_HOSTS` | empty (`localhost` and friends in debug) | Comma-separated hostnames the site answers to. `init --domain` writes it. |
| `DJANGO_DEBUG` | off | `1` for local development only. `up.sh` refuses to install if the server's `.env` turns it on (1, true, yes, or on), so a server cannot run with it. |
| `CD_DB_NAME` | `conference_drawing` | PostgreSQL database name. |
| `CD_DB_USER` | `conference-drawing` | PostgreSQL role. |
| `CD_DB_HOST` | empty (the local socket) | Host or socket directory. Homebrew: `/tmp`. |
| `CD_DB_PORT` | empty | Port, if not the default. |
| `CD_DB_PASSWORD` | empty | Password. Leave empty for socket login; for a TCP setup only. |
| `CD_DATA_DIR` | `var/` in the project | Where static files and uploaded covers live. |
| `CD_ENV_FILE` | `.env` in the project | The file to read settings from. |
| `CD_VERSION_FILE` | none | A JSON file the deploy writes; the footer and `/version` show it. |
| `DRIP_ACCOUNT_ID` | none | Your Drip account id (digits). Set both Drip values or neither. |
| `DRIP_API_KEY` | none | Your Drip API key. Without both, the app runs without Drip (see below). |
| `DJANGO_SUPERUSER_USERNAME`, `_EMAIL`, `_PASSWORD` | none | Only for `init --non-interactive`; the deploy passes them to the server over ssh. |

`init` takes `--env-file`, `--domain HOST...`, and `--non-interactive`.

Without Drip credentials the app still works: digital prizes show the code and
URL on your drawing screen and in the CSV for you to hand over, and wrap-up
only locks the conference.

## Privacy notes

- **What the entry form collects.** Name, email address, answers to the
  optional questions, the follow-up acknowledgement (the wording shown and
  when it was accepted), and, if ticked, the Better Developers opt-in (the
  wording shown and when). The box is unchecked by default. Entering the same
  email again changes nothing, and shows the same thanks page, so the form
  cannot be used to find out who has entered.
- **Who sees names.** Winner names appear on your phone and on the booth
  display, whose address contains a secret token. The flyer and entry pages
  never show them. Every page tells search engines not to index it. The
  app's own logs carry ids and counts only, never names, emails, answers, or
  codes. Every log line, Django's own error lines included, has display
  tokens replaced by `REDACTED`, and an error's traceback names each
  exception's type without its message (which could quote a name or email).
- **Server access log.** Caddy, the web server in front of the app, keeps its
  own access log for this site (`/var/log/caddy/conference-drawing.access.log`).
  It records each visitor's IP address and the page they asked for. Display
  addresses have their token replaced by `REDACTED`, query strings are dropped,
  and no request or response headers are logged. The log is rotated and kept
  for up to 14 days, and `down.sh` deletes it with the site.
- **What goes to Drip, and when.** Nothing goes to Drip while people enter.
  When a digital prize is drawn, the winner's email gets a `Won raffle prize`
  event with the book title, code, prize URL, and conference name. At wrap-up
  every entrant, except those on your exclusion list, becomes a Drip
  subscriber (email and first name, never a subscription status) with the
  conference's tags, plus an `Entered raffle` event holding the conference
  name and their answers. Only people who ticked the Better Developers box
  carry an EU consent grant, with the exact wording they saw. Physical-prize
  winners get no email from the app.
- **Deleting.** "Delete entrants" removes every entrant of the conference, and
  with them their picks, the admin-history rows about them, and any failed
  Drip jobs; the remaining Drip jobs keep a row but lose their contents. It
  also empties the rate-limit cache, whose keys are hashes of visitors' IP
  addresses (easy to reverse for IPv4); otherwise each key expires after
  about a minute but its row can stay in the table until overwritten. It
  then tries to rewrite the affected database tables so the deleted rows leave
  the files. That rewrite is best effort: it gives up (and only logs it) if a
  table stays locked for more than 5 seconds, and even when it runs, the
  database's write-ahead log files can still hold the rows until they are
  recycled, and older snapshots hold them until they age out (next point). It
  does not touch Drip: what is in Drip is managed in Drip. The drawings,
  prizes, and counts stay.
- **Backups still hold what you deleted.** Anything saved **before** a
  deletion still contains the deleted people until it ages out or you delete
  it. On the server, hourly database snapshots and the one taken before each
  upgrade keep the newest 48, so deleted entrants can live on there for up to
  48 hours. Backups on your laptop (`backups/`) are never aged out and are not
  encrypted: delete entrants **before** running `down.sh`, and delete old
  backups yourself.
- **Your own privacy page.** `/privacy/` shows text you write in the admin
  (Install settings); the entry form links to it. Say who you are, what
  happens to the data, and the points above.

## Development

```sh
uv sync
uv run pytest --cov=conference_drawing -m "not e2e"
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the checks and the test setup.

Licensed under the MIT License.
