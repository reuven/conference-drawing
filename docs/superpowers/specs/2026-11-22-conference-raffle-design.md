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
| **A. Django + HTMX + SQLite** | Auth, sessions, CSRF, forms, migrations, and an admin come built in; HTMX gives live updates without custom JS. Heaviest framework, own conventions. | **Chosen** |
| B. FastAPI + Jinja + SQLAlchemy/Alembic + HTMX | Lighter, typed; but login, sessions, CSRF, forms, and setup screens are hand-built — more security surface on a public app. | Rejected |
| C. React SPA + Python API | Doubles the test burden (100% coverage both sides) for no user-visible gain. | Rejected |

Reuven picked A, on Python 3.14.

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
  existing email does **not** modify the entry — it shows "You're already
  entered." Otherwise anyone could overwrite a stranger's name or consent by
  typing their email.)*
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
  never knows the consolation code.
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
- "Delete this conference's entrants" removes entrant rows and their picks,
  and redacts any outbox rows that hold their emails. It is refused while
  Drip jobs for that conference are still pending. It does **not** touch Drip
  — what lives in Drip is managed in Drip. *(My call.)*
  *(My call: keep the conference, drawings, and aggregate counts — pool size,
  number of winners — so history survives without personal data.)*

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
  (allowed only if the replacement hasn't been marked yet). Undo is recorded
  in the audit trail as a reversal, never by deleting history.
- **Display view privacy**: the display URL contains an unguessable
  per-conference token, so winner names aren't on a public, guessable page.
- **Entry form abuse**: per-IP rate limit and a honeypot field; no CAPTCHA
  (friction on phones). Entry closes at wrap-up.
- **Time handling**: all times stored in UTC, displayed and entered in the
  conference's IANA zone.
- **QR code**: generated server-side as SVG (e.g. `segno`).
- **Live updates**: HTMX (vendored, pinned) with polling; no WebSockets.
- **Deployment** (serving goal 8; revised 2026-11-22 after Reuven chose
  DigitalOcean and pointed at his existing server). The app is **ephemeral per
  conference**: installed when the conference starts, removed when it ends.
  Target is any Ubuntu server with **systemd + Caddy** that the operator can
  ssh to as root. Reuven's is `wp2.lerner.co.il` (Ubuntu 24.04, 1 vCPU,
  2 GB RAM + 2 GB swap, native Caddy 2.11 importing `/etc/caddy/sites/*.caddy`,
  no Docker, and it also serves his WordPress sites and store — so the install
  must be small and must never break Caddy).
  - **Native, no Docker**: `uv` installs Python 3.14 and the locked
    dependencies under `/srv/conference-drawing/`, owned by a dedicated
    `conference-drawing` system user. Two systemd units: `conference-drawing-web`
    (gunicorn, 2 workers, on a unix socket) and `conference-drawing-drip`
    (the outbox worker). An hourly systemd timer snapshots SQLite with
    `sqlite3 .backup`, keeping 48.
  - **Caddy**: the install writes one file, `/etc/caddy/sites/conference-drawing.caddy`
    (`<domain> { reverse_proxy unix//run/conference-drawing/web.sock }` plus
    static/media handling), runs `caddy validate` on the full config, and only
    then `systemctl reload caddy`. It never edits any other Caddy file.
  - **Operator commands, run from the laptop** (one each, refusing on the first
    failure):
    - `deploy/up.sh <host> <domain>` — preflight (refuses if any systemd unit is
      failed, Caddy invalid, or the domain's DNS doesn't point at the host),
      install or upgrade, optional restore of a previous backup, then a smoke
      check of `/health`, `/version`, and the entry page over HTTPS.
    - `deploy/backup.sh <host>` — pulls a consistent SQLite snapshot plus
      media to the laptop.
    - `deploy/down.sh <host>` — **refuses** while Drip outbox jobs are pending
      or if no backup newer than the last write exists; then removes the units,
      the Caddy site file (validate + reload), and `/srv/conference-drawing`.
  - **No server?** The README shows creating a $6 DigitalOcean Droplet,
    installing Caddy from its official apt repo, then running `up.sh`.
  - **Init**: `conference-drawing init` (run by `up.sh` on first install)
    generates `SECRET_KEY`, writes the server's `.env` (mode 0600), migrates,
    and creates the owner login.
  - **Books carry across conferences** by restoring the previous backup on
    `up.sh --restore <file>`; deleting a finished conference's entrants first
    keeps personal data out of the backup.
  - Every page footer shows the running version; `GET`/`HEAD /health`
    (no DB) and `GET /version` exist.
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
