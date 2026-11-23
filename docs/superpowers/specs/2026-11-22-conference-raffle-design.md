# Conference Book Raffle — Design

Date: 2026-11-22
Status: approved 2026-11-22

## Intent and why

Reuven runs a booth at conferences (PyCon US, PyCon Taiwan, …) and raffles
signed copies of his books. Today this is stitched together by hand:
a SurveyMonkey form, a printed flyer with a QR code, a CSV downloaded before
each drawing, manual random picking that skips himself and booth staff,
names read aloud, and follow-up emails.

This project replaces all of that with one **self-hosted, open-source web
app**. Each install has a single owner (Reuven runs his; other exhibitors can
deploy their own copy from the repo). The app handles setup, the flyer,
entry, the drawings at the booth, winner notification, and the
post-conference hand-off to Drip.

Success: at the next conference, Reuven sets up the raffle in the app,
prints the flyer, and runs every drawing from his phone at the booth —
with no SurveyMonkey, no CSV juggling, and no manual exclusion of staff.

## Goals

1. Set up a conference: name, dates, IANA time zone, booth location,
   exclusion list, optional entry questions, and a schedule of drawings, each
   with one or more prize lines.
2. Generate a printable flyer with a QR code to the entry page.
3. Public, phone-friendly entry form with validation and consent capture.
4. Run drawings at the booth from a phone: fair random picks, physical-prize
   claim/redraw flow, digital-prize delivery.
5. Optional full-screen display view for a booth monitor.
6. Notify digital winners via Drip; after the conference, push all entrants
   to Drip with tags so Drip workflows send the follow-up (including the
   "you didn't win, but…" consolation code).
7. CSV export of entrants, and one-click deletion of a conference's entrant
   data.
8. **Super easy to self-deploy** for someone who knows Python but isn't an
   ops person (Reuven's requirement). Open source, repo `conference-drawing`.

## Non-goals

- Multi-tenant hosted service. One owner per install; no sign-up, no tenant
  isolation.
- Sending email directly (SMTP, transactional providers). All email goes
  through Drip.
- Email providers other than Drip. (The Drip integration is isolated in one
  module so someone could add another later, but we don't build that.)
- Separate staff accounts or roles. Booth staff use the owner login.
- Shipping physical books, payments, internationalization (UI is English).
- Weighting the drawing by entrant preferences.

## Anti-goals (failure even if it "works")

- An unfair drawing: an excluded person (owner/staff) wins, someone wins
  twice at one conference, one person gets two chances via duplicate entries,
  or the randomness is predictable.
- The booth drawing stalls because Drip or the network to Drip is slow or
  down. Drawing must never wait on Drip.
- Attendee data mishandled: newsletter subscription without explicit opt-in,
  winner names exposed publicly on the internet, row-level data in logs.
- A drawing screen too fiddly to operate one-handed in a crowd.

## Constraints

- Python 3.14 (pin `.python-version` and `requires-python = ">=3.14,<3.15"`;
  Django 6.1 does not officially list 3.15). `uv` project.
- Shipping-code rigor (owner's standing rules): TDD with pytest, `mypy
  --strict` (with `django-stubs`), 100% coverage enforced via
  `--cov-fail-under=100`, `pytest-playwright` integration tests, Hypothesis
  for invariants, mutmut on changed modules, ruff. 100% coverage also applies
  to any first-party JavaScript (the aim is to have none or almost none).
- Pydantic for non-ORM data models (Drip payloads, CSV rows, config) with
  validators.
- Secrets (Django `SECRET_KEY`, `DRIP_API_KEY`, `DRIP_ACCOUNT_ID`) in the
  project's own `.env`, loaded with `python-dotenv`. Never committed, and
  never read from `~/.env`, so a deployed server holds only what the app
  needs.
- At most three concurrent test runs on the dev machine; no pytest-xdist.
- Logs carry ids and counts only — never names, emails, or answers.

## Approaches considered

| Approach | Trade-off | Verdict |
|---|---|---|
| **A. Django + HTMX + PostgreSQL** (SQLite until 2026-11-22) | Auth, sessions, CSRF, forms, migrations, and an admin come built in; HTMX gives live updates without custom JS. Heaviest framework, own conventions. | **Chosen** |
| B. FastAPI + Jinja + SQLAlchemy/Alembic + HTMX | Lighter, typed; but login, sessions, CSRF, forms, and setup screens are hand-built — more security surface on a public app. | Rejected |
| C. React SPA + Python API | Doubles the test burden (100% coverage both sides) for no user-visible gain. | Rejected |

Reuven picked A, on Python 3.14.

### Storage (revised 2026-11-22: PostgreSQL only)
- **PostgreSQL, and nothing else.** Reuven trusts it more than SQLite: real
  row locks, concurrent writers, mature backup tooling. SQLite support was
  removed entirely; there are no dual code paths.
- Settings come from the environment (or the project `.env`): `CD_DB_NAME`
  (default `conference_drawing`), `CD_DB_USER` (default `conference-drawing`),
  `CD_DB_HOST` (default empty: the local unix socket), `CD_DB_PORT`, and
  `CD_DB_PASSWORD` (optional; empty means peer auth over the socket).
  `CONN_MAX_AGE=60` with `CONN_HEALTH_CHECKS`; `ATOMIC_REQUESTS` off. Driver:
  psycopg 3 (`psycopg[binary]`: wheels bundle libpq, so the server needs no
  libpq-dev or compiler, and `uv export` carries their hashes).
- **Concurrency.** `select_for_update()` takes real row locks: two phones
  tapping the same pick serialize on the drawing's lock, and the second sees
  the first's result. The Drip worker claims jobs with `skip_locked`, so
  concurrent workers never wait on each other.
- **Versions.** The laptop and CI run PostgreSQL 18 (Homebrew
  `postgresql@18`; a `postgres:18` service container), the server Ubuntu
  24.04's PostgreSQL 16. Django 6.1 (PostgreSQL 15+) and psycopg 3 support
  both, and the app uses nothing newer than 16, so the drift is harmless.
- **Tests** run against a local PostgreSQL; pytest-django creates
  `test_conference_drawing`. The conftest guard refuses any host but
  `localhost`, `127.0.0.1` or a socket directory, and any `OPTIONS` or libpq
  variable (`PGHOST`, `PGHOSTADDR`, `PGSERVICE`, `PGSERVICEFILE`) that could
  point elsewhere.

## Decisions (made with Reuven)

### Who runs it
Self-hosted, one owner per install.

### Conference and drawings
- A **conference** has many **drawings**, each at a date and time in the
  conference's time zone. Several per day are normal (e.g. 11:00 and 14:00).
- A drawing has one or more **prize lines**: a book × a quantity × a kind
  (**physical** or **digital**). E.g. "3 × Python Workout (physical) +
  2 × Pandas Workout (digital)" = 5 winners.
- Winners are drawn **per prize line, in line order**, so results read as
  "Python Workout: Alice, Bob, Carol".

### Eligibility
- Pool = everyone who has entered *this conference* (not just since the last
  drawing), minus the exclusion list (owner + booth staff, matched by
  normalized email, set per conference), minus anyone who has **already won**
  at this conference.
- "Already won" = has a **Claimed** physical pick or **any** digital pick at
  this conference. A digital pick counts the moment it is drawn, whether or not
  Drip has delivered the email yet (Reuven: "they get it, end of story").
  Anyone already picked in the current drawing (any line, any status) is also
  out of the pool for the rest of that drawing.
- Duplicate entries are impossible: email (normalized) is unique per
  conference. *(My call, changed after review: a re-submission with an
  existing email does **not** modify the entry. Otherwise anyone could
  overwrite a stranger's name or consent by typing their email.)* *(Further
  call, recorded in Task 1: a repeat entry shows the same thanks page as a new
  one, "Thanks — you're in the drawing.", not "You're already entered", so the
  form cannot be used to test whether an email has entered.)*
- Entry cutoff: an entry is in a drawing only if it was created before the
  drawing's cutoff. *(Builder's precise rule, my call: cutoff = the earlier of
  the scheduled time and the moment the owner presses Start, so someone who
  enters at 14:01 is in the next drawing, not the 14:00 one.)*

### Physical vs. digital prizes
- **Digital** (ebook / coupon): the pick is final at once. The winner is
  announced at the booth and emailed the prize via Drip. Presence not needed.
- **Physical**: each pick shows **Claimed** and **Not here** on the drawing
  screen. **Not here** immediately draws a replacement for that copy; the
  absent person is **not** a winner, is excluded from the rest of *this*
  drawing, and stays eligible for later drawings. If the pool is exhausted,
  the app says so and the copy goes unawarded.
- A drawing is **finished** when every physical copy is Claimed, or the owner
  closes it with copies unawarded. Until a physical pick is Claimed, that
  person doesn't count as a past winner.
- Physical winners get **no email**. The entry confirmation page and flyer
  state: "Drawings at [times] at [booth]. You must be present to win a printed
  book."

### Digital prize codes
- A digital prize line has a **URL**, optionally containing `{code}` (e.g.
  `https://store.example.com/checkout?coupon={code}`), and a **code source**:
  - **Pool** (normal case): the owner pastes unique codes, one per line; each
    winner consumes one code.
  - **Shared**: one code reused for every winner of that line (for a
    limited-use code configured elsewhere).
- The app refuses to start a drawing whose pool-mode lines have fewer unused
  codes than their quantity, and says which line is short.

### Entry form
Fixed fields:
1. Name — required.
2. Email — required, validated.
3. Required acknowledgement: "Entering means one follow-up email from me after
   the conference," linking to a privacy note.
4. ☐ "Also subscribe me to Better Developers" — optional, **unchecked by
   default**.

Per-conference **optional questions**, configurable (free text or pick-one
choice, ordered), prefilled for a new conference with:
- "What do you most want to learn?" (free text)
- "How do you code these days?" (pick one):
  - I'm just starting to code
  - I write my own code, and use AI a little or not at all
  - I work alongside AI: chat, autocomplete, asking it to explain
  - AI agents write most of my code; I direct and review
- "Anything else to tell me?" (free text)

The app records, per entrant, the consent text shown and the timestamp of the
follow-up acknowledgement and of the Better Developers opt-in (if any).

### Booth experience
- **Drawing screen is phone-first.** Start drawing, Claimed / Not here,
  redraw, close drawing — all one-handed. Setup screens may assume a laptop.
- **Optional display view**: a full-screen page for a booth monitor showing
  the next drawing time and entry count; within ~2 seconds of a drawing on the
  phone it shows winners by book in large type. Works through HTMX polling.
  Not required — everything works without it.
- **Flyer**: a printable page per conference — books with covers, drawing
  schedule, large QR code, short typeable URL, and the "must be present"
  line. Printed or saved as PDF from the browser.

### Drip integration (Drip API v2)
- **Digital win**: record a Drip **event** (`Won raffle prize`) for the
  winner's email, with properties `book_title`, `coupon_code`, `prize_url`,
  `conference`. A Drip workflow triggered by that event sends the email.
  Event properties (not subscriber custom fields) carry the code, so codes
  can't be overwritten across wins.
- **Tag scheme** (agreed with Reuven). Each conference has a slug that
  defaults to the pattern of his recent tags, `pycon-us-YYYY` /
  `pycon-tw-YYYY`, with the slug first so a conference's tags sort together:

  | Tag | Applied to |
  |---|---|
  | `<slug>-raffle` | every entrant |
  | `<slug>-raffle-winner` | winners (physical Claimed or any digital pick) |
  | `<slug>-raffle-nonwinner` | everyone else |
  | `<slug>-bd-optin` | entrants who ticked the Better Developers box |

  The app only adds tags in this scheme and never removes or renames
  existing tags in the account.
- **Wrap up** (button, at conference end): create/update every entrant as a
  Drip subscriber (email and name only; no `status`) and apply the tags
  above. Drip workflows send the follow-up and consolation message; the app
  never knows the consolation code. Entrants whose normalized email is on the
  conference's exclusion list (the owner's and staff's own test entries) are
  **not** sent: no subscriber, tags or event. The wrap-up page and result count
  them separately as "excluded".
- **Better Developers** (Reuven chose option A). Better Developers in Drip has
  no double opt-in, and Reuven's habit is to notify people before adding them.
  So:
  - Entrants who tick the box get `<slug>-bd-optin` and are added to Better
    Developers directly (by a Drip workflow keyed on that tag).
  - Everyone else gets Reuven's existing practice, built entirely in Drip: the
    follow-up email tells them he'll add them to Better Developers and how to
    remove themselves, then after a wait they are added unless they opted
    out. Reuven may restrict that workflow to non-EU entrants (opt-out notice
    isn't GDPR consent).
  - The app's only job is to tag correctly. It does not add anyone to Better
    Developers itself. Drip workflow setup is documented in the README, not
    automated.
- **Entrant answers travel as event properties, not custom fields.** On
  wrap-up, each entrant also gets an `Entered raffle` event whose properties
  are `conference` plus one key per optional question (each question has a
  stable `key`, e.g. `want_to_learn`, `coding_style`). Custom fields are
  global per subscriber and would be overwritten by the next conference.
- **Consent mapping** *(my call)*: `eu_consent: "granted"` with the Better
  Developers checkbox text as `eu_consent_message` is sent **only** for
  entrants who ticked that box; for everyone else `eu_consent` is omitted.
  The app never sends a subscriber `status`, so it can't resubscribe someone
  who previously unsubscribed.
- **Without Drip** (no credentials): digital picks show the code and URL on the
  owner's drawing screen and in the CSV for manual hand-off; the Drip buttons
  explain how to configure it.
- **Drip never blocks the booth.** Calls go through a persistent outbox with
  retries; failures are visible to the owner with a retry action.

### Data export and deletion
- CSV export per conference: name, email, answers, entry time, winner /
  non-winner (and what they won), consent flags and timestamps.
- "Delete this conference's entrants" removes the entrant rows and, by
  cascade, their picks; the coupon and audit links to those picks become NULL
  (a used coupon code stays `used`, so it is never handed out twice). It also
  deletes the admin-history (`LogEntry`) rows about those entrants, deletes
  the conference's FAILED outbox jobs (a redacted one could be retried and
  fail as poison), and replaces every remaining job's payload with
  `{"redacted": true}`. It is refused unless the conference is wrapped up and
  not already deleted, and while Drip jobs for it are still pending. It does
  **not** touch Drip: what lives in Drip is managed in Drip. *(My call.)*
  *(My call: keep the conference, drawings, and aggregate counts — pool size,
  number of winners — so history survives without personal data.)*
- PostgreSQL's `DELETE` only marks tuples dead; their bytes stay in the table
  files. So once the deleting transaction commits, the app runs
  `VACUUM (FULL, ANALYZE)` on the entrant, pick, outbox-job, coupon-code and
  admin-log tables, outside any transaction (from `on_commit`), which writes
  each table to a new file. The app role owns these tables (it ran migrate),
  as VACUUM FULL requires. It waits at most `lock_timeout = 5s` for each
  table (a running pg_dump holds a read lock), so it never stalls the request;
  a failure, a lock timeout included, is logged by exception class only.
  **What VACUUM FULL cannot guarantee:** WAL segments keep the deleted rows
  until PostgreSQL recycles them, an old snapshot (a long transaction) can
  keep them alive until it ends, and the hourly pg_dump snapshots hold them
  until they age out, 48 hours later. The owner's privacy note should say so.
- **Backup retention.** On the server, `data/snapshots/` keeps the newest 48
  `pg_dump` files: the hourly ones and the one taken just before each
  migrate share that count. Anything taken **before** a deletion still holds
  the deleted entrants until it ages out of those 48. Backups pulled to the
  laptop by `deploy/backup.sh` (and the one `down.sh` takes) are never aged
  out by the app: they hold personal data until the operator deletes them.
  Deleting entrants before `down.sh` keeps personal data out of that last
  backup.

### Randomness and audit
- Picks use Python's `secrets` module (`secrets.SystemRandom`).
- Each drawing records when it ran, its cutoff, the pool size at each pick,
  and every pick (including Not here and replacements), so fairness can be
  shown if anyone asks.

## My calls (Reuven can override)

- **Admin**: one Django superuser; the Django admin handles the books catalog,
  conference fields, and the exclusion list. The drawing screen, flyer,
  display, export, and wrap-up are custom views.
- **Books catalog**: title, cover image, optional link; reused across
  conferences.
- **One drawing in progress at a time** per conference.
- **Undo a mis-tap**: while a drawing is open, the most recent Claimed or Not
  here on a pick can be undone. Undoing a Not here voids its replacement
  (allowed only if the replacement hasn't been marked yet). Undoing the claim
  that auto-finished a drawing reopens it, unless another drawing is open or
  a later one has started. Undo never works after wrap-up (Drip already has
  the winners) or after the owner closed the drawing. Undo is recorded in the
  audit trail as a reversal, never by deleting history.
- **Display view privacy**: the display URL contains an unguessable
  per-conference token, so winner names aren't on a public, guessable page.
- **Entry form abuse**: per-IP rate limit and a honeypot field; no CAPTCHA
  (friction on phones). Entry closes at wrap-up.
- **Admin login abuse**: login POSTs are rate limited per IP (10 a minute),
  so password guessing can't tie up the workers the booth needs.
- **Time handling**: all times stored in UTC, displayed and entered in the
  conference's IANA zone.
- **QR code**: generated server-side as SVG (e.g. `segno`).
- **Live updates**: HTMX (vendored, pinned) with polling; no WebSockets.
- **Deployment** (serving goal 8; revised 2026-11-22 after Reuven chose
  DigitalOcean and pointed at his existing server). The app is **permanent;
  teardown is optional** (revised 2026-11-23, v0.2): one install serves one
  conference after another, and `down.sh` is for retiring it.
  Target is any Ubuntu server with **systemd + Caddy** that the operator can
  ssh to as root. Reuven's is `wp2.lerner.co.il` (Ubuntu 24.04, 1 vCPU,
  2 GB RAM + 2 GB swap, native Caddy 2.11 importing `/etc/caddy/sites/*.caddy`,
  no Docker, and it also serves his WordPress sites and store — so the install
  must be small and must never break Caddy).
  - **Native, no Docker**: `uv` installs Python 3.14 and the locked
    dependencies under `/srv/conference-drawing/`, owned by a dedicated
    `conference-drawing` system user. Two systemd services: `conference-drawing-web`
    (gunicorn, 2 workers, on a unix socket that the `conference-drawing-web.socket`
    unit creates as the app user, group `caddy`, mode 0660, and hands to gunicorn;
    every script that stops the app stops that socket too, or a request would
    start the web service again) and `conference-drawing-drip`
    (the outbox worker), both `Requires=postgresql.service`. An hourly
    systemd timer runs `pg_dump -Fc` as the app user, reads every block back
    with `pg_restore -f /dev/null` (`--list` reads only the table of
    contents), and keeps the newest 48, under the same maintenance lock as
    install, backup and teardown.
  - **PostgreSQL on the server** (wp2 has none yet; MySQL is active for
    WordPress; about 860 MB RAM free): `install.sh` installs Ubuntu's
    `postgresql` package (16) if it is missing and records that it did in
    `/srv/conference-drawing/.installed-postgresql`; only then does it write a
    `conf.d` drop-in (`listen_addresses = ''` — socket only —
    `shared_buffers = 64MB`, `max_connections = 20`), restarting PostgreSQL
    when the drop-in changes (the units' `Requires=` then restarts web and
    Drip for a moment). "Missing" means no PostgreSQL of any kind: no cluster
    under `/etc/postgresql` and no `postgresql-<major>` package (Ubuntu's or
    PGDG's, with or without the metapackage). An existing PostgreSQL may serve
    others: no apt, no marker, no drop-in, no restart, only a warning with its
    `listen_addresses`. Any server must be version 15 or later (Django 6.1),
    and a `conference_drawing` database owned by another role is refused,
    never taken over. The app is pointed at `/var/run/postgresql` explicitly
    (`CD_DB_HOST` in the units and in install.sh), not at the binary wheel's
    compiled-in socket default. It creates role
    `conference-drawing` (LOGIN, no password: the app's OS user maps to it by
    peer auth, so no database password exists anywhere) and database
    `conference_drawing` owned by it, idempotently, via
    `runuser -u postgres -- psql -v ON_ERROR_STOP=1` with the names passed
    only as psql variables (`:"role"`, `:"db"`). `PUBLIC` loses all access to
    the database.
  - **Caddy**: the install writes one file, `/etc/caddy/sites/conference-drawing.caddy`
    (`<domain> { reverse_proxy unix//run/conference-drawing/web.sock }` plus
    static/media handling), runs `caddy validate` on the full config, and only
    then `systemctl reload caddy`. It never edits any other Caddy file.
  - **Operator commands, run from the laptop** (one each, refusing on the first
    failure):
    - `deploy/up.sh <host> <domain>` — preflight (refuses if any systemd unit is
      failed, Caddy invalid, or the domain's DNS doesn't point at the host),
      `install.sh` also refuses, before changing anything, a server `.env`
      that sets `DJANGO_DEBUG` to a true value (1, true, yes, on), so a server
      never runs with DEBUG on;
      install or upgrade, optional restore of a previous backup, then a smoke
      check of `/health`, `/version`, and `/privacy/` over HTTPS (a page
      that renders a template and reads the database, yet exists before any
      conference does).
    - `deploy/backup.sh <host>` — pulls a consistent backup to the laptop:
      `db.dump` (`pg_dump -Fc` run as the app user, read back fully) plus
      media; root only receives the tar stream.
    - `deploy/down.sh <host>` — **refuses** while Drip outbox jobs are pending.
      Otherwise it stops the web app and the snapshot timer, checks the outbox
      once more (nothing new can be queued now; if a job slipped in, the app
      is started again and it refuses), then stops the Drip worker. It then
      takes a fresh backup itself (the same as `backup.sh`) and aborts, with
      the app left stopped and the command to start it again printed, if the
      backup fails or comes back empty. Only then does it remove the units,
      the Caddy site file (validate + reload) and its access log, the database
      and role, and `/srv/conference-drawing`. It purges `postgresql` and
      `postgresql-<major>` only when all of these hold: the marker exists;
      `pg_lsclusters` shows exactly one cluster, `<major> main`; no database
      but `postgres`, `template0` and `template1` remains; `postgres` and
      `template1` hold no relations outside the system schemas; and no role
      but `postgres` and `pg_*` remains. Every check fails closed. Otherwise
      PostgreSQL and the drop-in stay, and the purge command is printed for
      the operator.
  - **No server?** The README shows creating a $6 DigitalOcean Droplet,
    installing Caddy from its official apt repo, then running `up.sh`.
  - **Init**: `conference-drawing init` (run by `up.sh` on first install)
    generates `SECRET_KEY`, writes the server's `.env` (mode 0600), checks the
    database connection (failing with a message that names the `CD_DB_*`
    settings, never a password), migrates, and creates the owner login.
  - **Books carry across conferences** by restoring the previous backup on
    `up.sh --restore <file>`; deleting a finished conference's entrants first
    keeps personal data out of the backup. Restore checks the tarball (only
    `db.dump` and plain files under `media/`), refuses a database that already
    has tables, and loads the dump into the fresh database as the app user
    with `pg_restore --clean --if-exists --no-owner --role=conference-drawing`.
  - A migrate that fails after the pre-migrate snapshot leaves the exact
    recovery commands in the install log (drop and recreate the database,
    then `pg_restore` the snapshot); with no snapshot (an empty database) it
    says there is nothing to put back.
  - Every page footer shows the running version (and short commit); the
    booth display and the printed flyer hide their footer, so the monitor
    stays clean and the flyer stays one page. `GET`/`HEAD /health` (no DB)
    and `GET /version` exist.
- **License**: MIT. Package/distribution name `conference-drawing`.
- **Drip is optional per install** (see "Without Drip" above).
- **Privacy note**: a page whose text the owner edits; linked from the form.

## Left to the builder

- Exact model fields, URL structure, template layout and styling.
- Outbox mechanism (DB table + management-command worker, or Django's tasks
  framework with a DB-backed worker).
- Rate-limit implementation details.
- Test structure, Hypothesis properties (e.g. no excluded or past winner is
  ever picked; picks per line never exceed quantity; each pool code is used
  at most once).
