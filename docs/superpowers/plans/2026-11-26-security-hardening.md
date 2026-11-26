# Security hardening (v0.4.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 0.3.0 security review's findings before the public announcement: no rigged rerolls, no stuffing by alias or by network, entries only during the conference, password rules and per-account login limits, no early starts, and the smaller fixes.

**Architecture:** Each fix lives where its rule already lives: the reroll rule in `raffle/draw.py`, the email key in `raffle/text.py`, the entry window and cap in the entry view and form, login limits in `urls.py`/`auth.py`, Drip timing in `raffle/drip/`, server fixes in `deploy/`. New state is one nullable column, one per-conference setting with a database default, and cache counters.

**Tech Stack:** Python 3.14, Django 6.1, PostgreSQL, django-ratelimit, HTMX, httpx, Pillow, pytest-django, Hypothesis, Playwright, bash deploy scripts tested with fake ssh.

**Spec:** `docs/superpowers/specs/2026-11-26-security-hardening-design.md` (read it; its Decisions section is the requirements).

## Global Constraints

- Python 3.14, Django 6.1, PostgreSQL only; `uv add` / `uv run`.
- `DJANGO_SECRET_KEY=x uv run mypy src tests` (strict), `uv run ruff format`, `uv run ruff check` pass before every commit.
- 100% branch coverage; TDD (watch each test fail for the right reason first); Hypothesis where an invariant spans many inputs.
- Tests only through `~/.local/bin/testslot` (browser tests `--heavy`), one at a time, never xdist.
- Migrations add-only or state-only; 0.3.0 must run on the new schema (nullable columns or `db_default`).
- Every refusal of a booth/flyer/export request stays a 404; logs carry ids and counts only; no inline scripts or styles.
- Small commits, one concern each; US English.
- Exact values: reroll pool excludes entrants voided **for the same slot** in the drawing; email key strips `+tag`, lowercases, NFC, IDNA domain; **Entries per network per hour** default **60**, **0 = no cap**; entry flood guard **300/m**; entries only **starts_on ≤ today ≤ ends_on** in the conference time zone, no grace day; passwords: Django's four standard validators, **minimum length 12**; per-account login limit **10 failures / 15 minutes**, failures only, shared by both login pages; helper start window **15 minutes** before `scheduled_at`; Drip `first_name` only letters, marks, spaces, `'`, `-`, `.`, **≤ 40** characters; snapshots rotate by **48 hours** as well as count.

## Review Focus

- A conference in a time zone far from UTC (e.g. `Pacific/Auckland`) must open and close at local midnight, not UTC midnight (Task 4).
- Someone who entered as `ada+1@example.com` under 0.3 and enters again as `ada@example.com` after the upgrade is told they're already entered: the data migration must backfill keys (Task 3).
- Rerolling a slot until every eligible person is voided ends in the normal "nobody left" state, never an error (Task 1).
- An owner who confirms an early start on a stale screen, after someone else started the drawing, gets the normal "already started" message (Task 6).
- Re-encoding an uploaded PNG with transparency or a GIF keeps its format and transparency; only metadata goes (Task 7).

---

### Task 1: Rerolls cost a person

**Files:** Modify `src/conference_drawing/raffle/draw.py` (`_draw_slot`, lines 99-133). Test: `tests/test_draw.py`.

**Interfaces:** Produces nothing new; `_draw_slot`'s pool excludes `Pick.objects.filter(prize_line=line, slot=slot, status=Pick.Status.VOID, prize_line__drawing=drawing)`'s entrants.

- [ ] **Step 1: Write the failing tests** in `tests/test_draw.py`:
  - `test_reroll_never_redraws_someone_voided_for_that_slot`: 20 entrants; loop `mark_not_here` → `undo_last` on the original; collect each replacement's entrant; assert no entrant appears twice, and after the pool is used up `mark_not_here` returns `None` with an `exhausted` audit (Review Focus line 3).
  - `test_voided_person_can_still_win_another_slot`: voided for slot 1 of line L, then drawn for slot 2 is allowed.
  - Hypothesis `test_reroll_sequences_never_repeat_a_voided_entrant_for_the_slot`: a random sequence of `not_here`/`undo` operations (strategy of booleans, length ≤ 30) on one slot with a seeded `random.Random`; invariant: every replacement entrant ∉ entrants voided for that slot before it.
- [ ] **Step 2: Run** `~/.local/bin/testslot uv run pytest -q -p no:cacheprovider --no-cov tests/test_draw.py` — Expected: the first and third fail (entrant repeats).
- [ ] **Step 3: Implement** the exclusion in `_draw_slot`; `pool_size` records the reduced pool.
- [ ] **Step 4: Run** the same — PASS. Run `tests/test_eligibility.py tests/test_booth_views.py` too.
- [ ] **Step 5: Commit** — "Rerolls: a slot never redraws someone voided for it".

### Task 2: The owner sees the history

**Files:** Modify `src/conference_drawing/raffle/admin.py` (register `AuditEvent`), `raffle/views/booth.py` (`conference_page`), `raffle/templates/raffle/booth/conference.html`. Test: `tests/test_admin.py`, `tests/test_booth_views.py`.

**Interfaces:** `AuditEventAdmin`: read-only (no add/change/delete permission), `list_display` time, drawing, kind, pick; `list_filter` drawing, kind; ordered newest first. Conference page context gains `history: dict[int, tuple[int, int]]` (drawing id → (undos, voids)), only when `is_owner`.

- [ ] **Step 1: Failing tests**: `test_audit_events_admin_is_read_only` (changelist 200 for owner; add URL 403; change form has no save button / POST refused); `test_owner_sees_undo_and_void_counts` (after one undo of a Not here, the page shows "1 undo, 1 voided" for that drawing); `test_helper_does_not_see_history_counts`.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (one aggregate query for the counts). **Step 4: Run** `tests/test_admin.py tests/test_booth_views.py tests/test_booth_access_crawler.py` — PASS.
- [ ] **Step 5: Commit** in two commits: admin page; booth counts.

### Task 3: One person per mailbox

**Files:** Modify `src/conference_drawing/raffle/text.py`, `raffle/models.py` (`Entrant.email_key`), `raffle/forms.py` (duplicate check and exclusion), wherever exclusion matching lives (`Conference.excluded_emails` / `eligibility.py`). Create two migrations (add field; backfill `RunPython`). Test: `tests/test_text.py`, `tests/test_forms.py`, `tests/test_eligibility.py`, `tests/test_migrations_backfill.py`.

**Interfaces:**
- `email_key(raw: str) -> str` in `raffle/text.py`: `normalize_email`, then drop `+…` from the local part, domain to its IDNA ASCII form (`idna` via `encodings.idna` or email-validator's normalized ASCII domain), lowercase.
- `Entrant.email_key = models.CharField(max_length=320, null=True, blank=True, db_index=True, editable=False)`.
- `Conference.excluded_keys() -> frozenset[str]` (or the existing exclusion method switched to keys — keep one function).

- [ ] **Step 1: Failing tests**: known answers (`Ada+x@Example.com` → `ada@example.com`; `ada@bücher.example` and `ada@xn--bcher-kva.example` → same key; `+` alone; a local part that is only `+tag` keeps a non-empty key or is refused — pick: refuse as invalid email); Hypothesis: `email_key` idempotent on its own output, and `email_key(f"{u}+{t}@{d}") == email_key(f"{u}@{d}")` over generated parts; form: entering `ada+2@example.com` after `ada@example.com` gives the same "already entered" outcome and creates no row; exclusion of `staff@example.com` also excludes `staff+x@example.com`; migration: rows created before (key NULL) are backfilled, then a new alias of a backfilled row is refused (Review Focus line 2).
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; the duplicate check runs under the existing entry lock. The backfill migration is reversible (noop reverse). Extend `tests/test_migrations_sql.py` only if a migration is meant to be state-only (these aren't).
- [ ] **Step 4: Run** the touched test files plus `tests/test_public_views.py tests/test_wrapup.py tests/test_csv_export.py` — PASS.
- [ ] **Step 5: Commit** in three commits: `email_key`; field + backfill; form and exclusion use it.

### Task 4: Entry window and the per-network cap

**Files:** Modify `raffle/models.py` (`Conference.entries_per_network_per_hour`), `raffle/views/public.py` (`entry`), `raffle/forms.py` if creation is signaled there, `raffle/admin.py` (field in the conference form), templates `raffle/closed.html` / new `raffle/not_open.html` / new `raffle/network_cap.html`. Migration. Test: `tests/test_public_views.py`, `tests/test_models.py`.

**Interfaces:**
- `Conference.entries_per_network_per_hour = models.PositiveIntegerField(default=60, db_default=60, help_text="How many new entries one network (a venue's Wi-Fi counts as one) may create per hour. 0 means no limit.")`.
- `Conference.entry_window(today: datetime.date) -> Literal["before", "open", "after"]` using `starts_on`/`ends_on`; the view passes today in the conference's time zone.
- `network_entries_allowed(conference: Conference, key: str, *, now: datetime.datetime) -> bool` and `record_network_entry(conference, key, *, now) -> None` in a new `raffle/entry_cap.py`, using Django's cache with a fixed-hour bucket key `f"entrycap:{conference.pk}:{sha256(key)}:{hour}"` (no raw address in the cache key).
- Entry view flood guard: `rate="300/m"`.

- [ ] **Step 1: Failing tests**:
  - window: day before → "not open" page with the opening date and status 200; first day and last day → form; day after → closed page; with `time_zone="Pacific/Auckland"` and a frozen UTC time that is already the next local day (Review Focus line 1); wrapped up → closed as today.
  - cap: 60 entries created from one IPv4 → the 61st valid new entry gets the cap page (status 429) and no row; honeypot, invalid and repeat posts don't count; a different /64 is unaffected; cap 0 = unlimited; the cache key contains no raw address.
  - flood guard: 300 POSTs/minute, 301st 429.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement**; count only after the entrant row is created.
- [ ] **Step 4: Run** `tests/test_public_views.py tests/test_forms.py tests/test_models.py tests/test_admin.py` — PASS; `makemigrations --check`.
- [ ] **Step 5: Commit** in three commits: window; cap setting + counter; flood guard rate.

### Task 5: Passwords and per-account login limits

**Files:** Modify `src/conference_drawing/settings.py` (`AUTH_PASSWORD_VALIDATORS`), `src/conference_drawing/urls.py` and/or `src/conference_drawing/auth.py`. Test: `tests/test_auth.py`, `tests/test_booth_login.py`, `tests/test_cli.py`.

**Interfaces:**
- `AUTH_PASSWORD_VALIDATORS`: UserAttributeSimilarity, MinimumLength(`min_length=12`), CommonPassword, NumericPassword.
- `account_key(request: HttpRequest) -> str` in `auth.py`: `"acct:" + POST username stripped and lowercased` (empty → `"acct:"`).
- A per-account limiter shared by both login views (`group="login-account"`, `rate="10/15m"`), counting only failed attempts: check without incrementing before authentication; increment on failure (`django_ratelimit.core.is_ratelimited(..., increment=...)`). Over the limit: the same 429 response as the address limit.

- [ ] **Step 1: Failing tests**: admin add-user with password `"short-pass"` refused, 12+ char non-common accepted; `init`/`owner_password` path refuses a short password (find where `cli.py` sets passwords); 10 failed booth logins for `helper` then the 11th attempt (even correct) on the **admin** login is 429; failures for `Helper` and `helper` share the bucket; correct logins don't count (10 successes then a failure is not 429); a different account is unaffected.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** `tests/test_auth.py tests/test_booth_login.py tests/test_cli.py tests/test_booth_access_crawler.py` — PASS.
- [ ] **Step 5: Commit** in two commits: password validators; per-account limit.

### Task 6: No early starts

**Files:** Modify `raffle/draw.py` (`start_drawing`) or `raffle/views/booth.py` (`start`), `raffle/templates/raffle/booth/drawing.html`, new `raffle/templates/raffle/booth/confirm_early_start.html`. Test: `tests/test_booth_views.py`, `tests/test_draw.py`.

**Interfaces:** `EARLY_START_WINDOW = datetime.timedelta(minutes=15)` in `draw.py`; `start_drawing(..., early_ok: bool = False)` raises `DrawingError("This drawing can start at <local time>.")` when `now < scheduled_at - EARLY_START_WINDOW` and not `early_ok`. The view passes `early_ok=True` only for an owner who posted `confirm_early=1`; an owner without it gets the confirm page; a helper never gets `early_ok`.

- [ ] **Step 1: Failing tests**: helper at 16 minutes early → refused with the local time in the message, drawing still scheduled; helper at 15 minutes → starts; owner at 2 hours early → confirm page, then POST with `confirm_early=1` → starts; owner confirm after another phone already started it → the normal already-started message (Review Focus line 4); Start button text includes the scheduled local time.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement** (no inline script; the confirm page is a plain form). **Step 4: Run** `tests/test_draw.py tests/test_booth_views.py tests/test_booth_access_crawler.py` — PASS (the crawler's start-route expectations may need the drawing scheduled in the past; adjust its seed, not its rules).
- [ ] **Step 5: Commit** in two commits: rule; confirm page and button text.

### Task 7: App-side smaller fixes

**Files:** `settings.py`, `raffle/views/public.py` (`client_ip`), `raffle/admin.py` (rotate action), `raffle/csv_export.py`, `raffle/validators.py` + `raffle/models.py` (`Book.cover`), settings `LOGGING`. Tests in the matching `tests/test_*.py`.

**Interfaces:**
- Debug key: `SECRET_KEY = secrets.token_urlsafe(50)` when DEBUG and no key (never a fixed string).
- `CD_TRUSTED_PROXIES` env: comma-separated IPs; `client_ip` honors `X-Forwarded-For` only when `REMOTE_ADDR` is empty or in that list; otherwise uses `REMOTE_ADDR`.
- `@admin.action(permissions=["change"], ...)` on `rotate_display_token`.
- `_FORMULA_STARTS` adds `"＝", "＋", "－", "＠"`.
- `LOGGING` sets `django.security` to `WARNING`.
- `reencode_image(file: File[Any]) -> ContentFile` in `validators.py`: `ImageOps.exif_transpose`, save in the detected format without EXIF, keep transparency and GIF frames (Review Focus line 5); used for logos and covers. `Book.cover` gets `validators=[validate_logo]`-style content checks (rename to `validate_image` if it reads better; keep one function) and a random-name `upload_to` like `logo_upload_to`, with migrations as state-only.

- [ ] **Step 1: Failing tests**, one per bullet: two DEBUG processes without a key get different keys and never the old constant; spoofed XFF ignored from an untrusted peer and honored from a trusted one and over the unix socket; a view-only staff user doesn't see the action; `＝1+1` neutralized; a too-many-fields POST logs at WARNING; a JPEG with GPS EXIF comes back without EXIF and correctly rotated, a transparent PNG keeps alpha, an animated GIF keeps its frames; a PNG named `cover.pdf` refused or renamed `.png`; a 3 MB cover refused.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** the touched test files plus `tests/test_after.py tests/test_admin.py` — PASS.
- [ ] **Step 5: Commit** one per bullet.

### Task 8: Drip fixes

**Files:** `raffle/drip/outbox.py`, `raffle/drip/client.py`, `raffle/drip/payloads.py`, `raffle/management/commands/drip_worker.py`. Tests: `tests/test_outbox.py`, `tests/test_drip_client.py`, `tests/test_drip_payloads.py`.

**Interfaces:**
- `process_due(..., clock: Callable[[], datetime.datetime] = timezone.now)`: every claim, outcome and lease check reads `clock()` at that moment.
- `DripError` gains `retry_after: float | None` from a 429's `Retry-After` (seconds or HTTP date, capped at 3600); `process_due` stops the pass on a 429 and returns it; the worker sleeps `retry_after` (default 60) before the next pass.
- `httpx.Client(timeout=..., trust_env=False)` plus an overall per-request deadline (e.g. a 30 s cap enforced around the call).
- `safe_first_name(name: str) -> str | None` in `payloads.py`: `\A[\p{L}\p{M} .'-]{1,40}\Z` semantics (use `unicodedata` categories; no new dependency); `None` → field omitted.

- [ ] **Step 1: Failing tests**: with a fake clock advancing 5 minutes per job, a second worker can't claim a job the first still leases, and backoff is from the failure time; a 429 with `Retry-After: 120` stops the pass and reports 120; HTTP-date form parsed; cap at 3600; `trust_env` is False; `safe_first_name("Ada")=="Ada"`, `"José"`, `"O'Neil"` kept, `"<a href=x>"`, 41 letters, `"a‮b"` dropped; payload omits `first_name` when unsafe.
- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** **Step 4: Run** the three test files plus `tests/test_wrapup.py` — PASS.
- [ ] **Step 5: Commit** one per bullet.

### Task 9: Deploy fixes

**Files:** `deploy/remote/snapshot.sh`, `deploy/remote/install.sh` (`write_env_keys`, PostgreSQL drop-in), `deploy/systemd/conference-drawing-web.service`, `deploy/systemd/conference-drawing-drip.service`, `deploy/systemd/conference-drawing-snapshot.service`. Tests: `tests/deploy/test_scripts.py` (fake ssh/scp harness).

**Interfaces / values:**
- Snapshot rotation: before each dump, `find "$snapshots" -name 'db-*.dump' -mmin +2880 -delete`, then the existing count rotation after a successful dump.
- `write_env_keys`: replace an existing `KEY=` line when the value differs (write via temp file + rename as the app user; never a symlink).
- Drop-in gains `log_min_error_statement = panic`.
- Web unit: `RestrictAddressFamilies=AF_UNIX`; drip unit: `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`; web, drip and snapshot units: `ProtectProc=invisible`, `CapabilityBoundingSet=`, `SystemCallFilter=@system-service`, `RestrictSUIDSGID=yes`. Snapshot unit keeps whatever families pg_dump over a unix socket needs (`AF_UNIX`).

- [ ] **Step 1: Failing tests**: an old dump (mtime 3 days) is deleted even when the new dump fails; a changed Drip key replaces the old line, an unchanged one is left alone, a symlinked `.env` is refused; the drop-in text contains the setting; each unit file contains the new directives (text tests, as the existing unit tests do).
- [ ] **Step 2: Run** `~/.local/bin/testslot uv run pytest -q -p no:cacheprovider --no-cov tests/deploy` — FAIL. **Step 3: Implement.** **Step 4: Run** — PASS. If `systemd-analyze` is available in CI, add `systemd-analyze verify` for the units; otherwise say so in the report.
- [ ] **Step 5: Commit** one per bullet.

### Task 10: Docs, privacy text, probes, mutation testing, version

**Files:** `docs/admin-guide.md`, `docs/booth-staff-guide.md`, `docs/drip-setup.md`, `docs/server-setup.md`, `raffle/templates/raffle/privacy.html` (default text: snapshots kept 48 hours, web server log kept 14 days, both after deletion), `pyproject.toml` (`version = "0.4.0"`) + `uv.lock`. Test: `tests/test_public_views.py` (privacy text), the reviewers' probes.

- [ ] **Step 1: Privacy page test** (fails), then the template text; commit.
- [ ] **Step 2: Docs**, one commit per guide: entry window and how to test before a conference (change the dates); the per-network cap and a venue's shared network; password rules; the account lockout tradeoff; the early-start window and owner confirmation; the audit history; anyone can enter someone else's email (and opt them in); don't send account-wide Drip broadcasts; `CD_TRUSTED_PROXIES` in the configuration reference; rotated Drip keys now reach the server on `up.sh`.
- [ ] **Step 3: Re-run the reviewers' probes** in the review's scratch folders against the new code (paths given in the dispatch); each finding fixed above must now fail its old exploit. Record results in the report.
- [ ] **Step 4: Mutation testing** on `raffle/draw.py`, `raffle/text.py`, `raffle/entry_cap.py`, `auth.py` (as `only_mutate` allows), `--max-children 2`, delete `mutants/` before and after; close real survivors with tests.
- [ ] **Step 5: e2e suite once** (`--heavy`, `--browser-channel chrome` locally); fix what the new rules break (e.g. e2e conferences need today inside their dates; drawings scheduled in the past for helper starts).
- [ ] **Step 6: Version 0.4.0** and `uv lock`; commit.
