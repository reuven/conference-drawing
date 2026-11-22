# Conference Drawing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A self-hosted Django app that runs a conference booth's book raffle end to end — setup, flyer, entry, drawings at the booth, Drip hand-off — installed on a systemd+Caddy host for one conference and torn down after.

**Architecture:** One Django project (`conference_drawing`) with one app (`raffle`). Business rules live in small service modules (`eligibility`, `draw`, `codes`, `wrapup`, `deletion`) that views call; views are thin. Drip calls go only through a DB outbox drained by a separate worker process, so the booth never waits on Drip. HTMX (vendored) gives the booth screen and display view their live behavior with no first-party JavaScript.

**Tech Stack:** Python 3.14, uv, Django 6.1, HTMX 2.0.11 (vendored), SQLite, gunicorn, WhiteNoise, segno (QR), httpx, Pydantic 2, Pillow, django-ratelimit, python-dotenv. Tests: pytest, pytest-django, pytest-cov, pytest-timeout, pytest-playwright, Hypothesis, respx, mutmut, pytest-testmon. Lint/types: ruff, mypy `--strict` + django-stubs.

**Spec:** `docs/superpowers/specs/2026-11-22-conference-raffle-design.md`

## Global Constraints

- `requires-python = ">=3.14,<3.15"`; `.python-version` = `3.14`.
- Distribution name `conference-drawing`; import package `conference_drawing`; console script `conference-drawing = "conference_drawing.cli:main"`.
- License MIT. Repo: `github.com/reuven/conference-drawing` (public).
- `mypy --strict` passes on `src` and `tests` (CI runs `uv run mypy src tests`). `ruff format --check` and `ruff check` clean before every commit.
- `pytest --cov=conference_drawing --cov-branch --cov-fail-under=100` is the CI gate. 100% applies to any first-party JS (target: none).
- TDD for every task: failing test first, watch it fail, then code.
- Tests run through `~/.local/bin/testslot` (`testslot -- uv run pytest …`; e2e with `testslot --heavy -- …`). Never pytest-xdist. Agents run only their own test files.
- All times stored UTC; entered and displayed in `Conference.time_zone` (IANA).
- Randomness only via an injected `rng: random.Random`, default `secrets.SystemRandom()`.
- Logs and outbox error records carry ids, counts and exception class names only — never names, emails, answers, or codes.
- Secrets only from `.env` (python-dotenv): `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DRIP_API_KEY`, `DRIP_ACCOUNT_ID`. Never read `~/.env`.
- Drip tags: `<slug>-raffle`, `<slug>-raffle-winner`, `<slug>-raffle-nonwinner`, `<slug>-bd-optin`. Events: `Won raffle prize`, `Entered raffle`. Drip `User-Agent: conference-drawing (+https://github.com/reuven/conference-drawing)`.
- Every HTML response: `Cache-Control: private, no-store`, `X-Robots-Tag: noindex, nofollow`, CSP `default-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; frame-ancestors 'none'` via Django's `ContentSecurityPolicyMiddleware`. No inline scripts or styles anywhere.
- Fixed copy (exact strings):
  - Must-be-present line: `Drawings at {times} at {booth}. You must be present to win a printed book.`
  - Follow-up acknowledgement default: `Entering means one follow-up email from me after the conference.`
  - Better Developers checkbox default: `Also subscribe me to Better Developers`
  - Thanks page (same for new and repeat entries): `Thanks — you're in the drawing.`
  - Closed entry page: `Entries for this conference are closed.`
- Default optional questions (key / prompt / kind / choices):
  - `want_to_learn` / `What do you most want to learn?` / text
  - `coding_style` / `How do you code these days?` / choice / `I'm just starting to code` · `I write my own code, and use AI a little or not at all` · `I work alongside AI: chat, autocomplete, asking it to explain` · `AI agents write most of my code; I direct and review`
  - `anything_else` / `Anything else to tell me?` / text

**Spec delta (my call, recorded in the spec in Task 1):** a repeat entry shows the same thanks page as a new one (not "You're already entered"), so the form can't be used to test whether an email has entered.

## Review Focus

1. **Double tap / two devices on "Not here"** — the same pick marked twice must yield exactly one replacement. Test in Task 4: `test_not_here_twice_draws_one_replacement`.
2. **Email variants** — `Alice@Example.COM ` re-entering, or appearing on the exclusion list in a different case, must match the same person. Tests in Task 3 (`test_exclusion_matches_case_insensitively`) and Task 6 (`test_repeat_entry_with_case_variant_does_not_duplicate`).
3. **Entry at the cutoff instant and DST** — an entry created exactly at the cutoff is out; a drawing scheduled at 14:00 local on a DST-change day lands at the right UTC instant. Tests in Task 3 (`test_entry_at_cutoff_is_excluded`) and Task 2 (`test_scheduled_local_time_across_dst`).
4. **Spreadsheet formula injection** — a name or answer starting with `=`, `+`, `-`, `@`, tab or CR must not execute when the CSV opens in Excel. Test in Task 11: `test_formula_cells_are_neutralized`.
5. **Running out** — fewer eligible entrants than copies, or a redraw when nobody is left, must award what it can and say so, never crash or reuse a person. Tests in Task 4: `test_pool_smaller_than_quantity_leaves_slots_unawarded`, `test_not_here_with_empty_pool_reports_exhausted`.

---

## File Structure

```
pyproject.toml                       deps, scripts, pytest/coverage/mypy/ruff/mutmut config
LICENSE  README.md  TODO.md
.github/workflows/ci.yml             lint+types+unit on push; e2e on dispatch/tags
.github/dependabot.yml
src/conference_drawing/
  __init__.py                        __version__
  cli.py                             main(): "init" subcommand, else Django manage
  settings.py  urls.py  wsgi.py
  middleware.py                      default no-store / noindex headers
  ops.py                             /health, /version views
  raffle/
    apps.py  admin.py  models.py  migrations/
    timezones.py                     local<->UTC helpers
    text.py                          normalize_email, validate_single_line_label
    eligibility.py                   eligible_entrant_ids
    draw.py                          start/claim/not-here/undo/close
    codes.py                         parse_codes, render_prize_url
    forms.py                         EntryForm
    csv_export.py                    rows + writer
    wrapup.py  deletion.py
    drip/payloads.py  drip/client.py  drip/outbox.py  drip/config.py
    management/commands/drip_worker.py
    management/commands/outbox_status.py
    views/public.py  views/booth.py  views/display.py  views/flyer.py  views/export.py
    templates/raffle/…               base, entry, thanks, closed, privacy, booth/*, display/*, flyer
    static/raffle/vendor/htmx-2.0.11.min.js   static/raffle/site.css  static/raffle/print.css
deploy/
  up.sh  down.sh  backup.sh          laptop-side operator commands
  remote/install.sh  remote/uninstall.sh
  systemd/conference-drawing-web.service  …-drip.service  …-snapshot.service  …-snapshot.timer
  caddy/conference-drawing.caddy.tmpl
tests/
  conftest.py                        hypothesis profiles, empty-DB guard, factories
  test_*.py                          one per module
  deploy/                            fake ssh/scp on PATH
  e2e/                               playwright flows
```

---

### Task 1: Project scaffold, settings, ops endpoints, test harness, CI

**Files:**
- Modify: `pyproject.toml`, `.python-version`, `README.md` (stub), `docs/superpowers/specs/2026-11-22-conference-raffle-design.md` (spec delta)
- Delete: `src/conferencedrawing/`
- Create: `src/conference_drawing/{__init__,cli,settings,urls,wsgi,middleware,ops}.py`, `src/conference_drawing/raffle/{__init__,apps}.py`, `LICENSE`, `.github/workflows/ci.yml`, `.github/dependabot.yml`, `tests/conftest.py`, `tests/test_ops.py`, `tests/test_settings.py`, `tests/test_conftest_profiles.py`

**Interfaces:**
- Produces: `conference_drawing.__version__: str`; `cli.main(argv: list[str] | None = None) -> int`; URL names `health`, `version`; middleware `conference_drawing.middleware.DefaultHeadersMiddleware`; fixture `db_is_empty` (autouse, names the leaking model); Hypothesis profiles `dev`/`ci`/`mutmut`.

- [ ] **Step 1: Dependencies and config.** `uv python pin 3.14`; set `requires-python`, rename project to `conference-drawing`, script entry; `uv add django pydantic httpx segno pillow whitenoise gunicorn django-ratelimit python-dotenv`; `uv add --dev pytest pytest-django pytest-cov pytest-timeout pytest-playwright hypothesis respx mutmut pytest-testmon ruff mypy django-stubs`. Configure: pytest (`DJANGO_SETTINGS_MODULE`, `--timeout=60`, `addopts = "--strict-markers"`), coverage (`source = ["conference_drawing"]`, `branch = true`, `fail_under = 100`, omit `migrations/*`, `wsgi.py`), mypy (`strict = true`, `plugins = ["mypy_django_plugin.main"]`), ruff (`select = ["E","F","I","B","UP","S","DJ"]`, tests ignore `S101`), mutmut (`source_paths = ["src/conference_drawing"]`).
- [ ] **Step 2: Failing tests.**

```python
# tests/test_ops.py
def test_health_get_and_head(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.head("/health").status_code == 200


def test_version_has_all_keys_and_never_500s(client, monkeypatch):
    monkeypatch.delenv("CD_COMMIT", raising=False)
    body = client.get("/version").json()
    assert set(body) == {"version", "commit", "commit_short", "built_at", "branch"}
    assert body["commit"] == ""


def test_html_responses_carry_default_headers(client):
    r = client.get("/admin/login/")
    assert r["Cache-Control"] == "private, no-store"
    assert r["X-Robots-Tag"] == "noindex, nofollow"
    assert "script-src 'self'" in r["Content-Security-Policy"]


# tests/test_settings.py
def test_settings_refuse_missing_secret_key_when_not_debug(
    monkeypatch,
): ...  # ImproperlyConfigured
def test_settings_never_read_home_env(
    monkeypatch, tmp_path
): ...  # HOME=tmp_path with .env containing DJANGO_SECRET_KEY → not used


# tests/test_conftest_profiles.py
def test_ci_profile_runs_200_examples(
    monkeypatch,
): ...  # CI=1, reload → settings().max_examples == 200
```

- [ ] **Step 3: Run, watch fail.** `testslot -- uv run pytest tests/test_ops.py tests/test_settings.py tests/test_conftest_profiles.py -q` → FAIL (no module / 404).
- [ ] **Step 4: Implement.** `settings.py` loads `.env` from `CD_ENV_FILE` or the project base dir only, via `dotenv_values` (no os.environ pollution); `DEBUG` from `DJANGO_DEBUG` (default off); SQLite at `CD_DATA_DIR/db.sqlite3` (default `BASE_DIR/var`); `MEDIA_ROOT = CD_DATA_DIR/media`; WhiteNoise; `DATA_UPLOAD_MAX_MEMORY_SIZE = 262_144`, `FILE_UPLOAD_MAX_MEMORY_SIZE = 5_242_880`; CSP via `SECURE_CSP` with `django.utils.csp.CSP`; `SECURE_PROXY_SSL_HEADER`, secure cookies when not DEBUG; logging to stderr with format `%(levelname)s %(name)s %(message)s`. `/version` reads `CD_VERSION_FILE` (JSON written by deploy), falls back to `__version__` + empty strings. Footer version comes from a context processor `conference_drawing.ops.version_context`. Hypothesis profile selection order: `HYPOTHESIS_PROFILE` → `mutmut` if `Path.cwd().name == "mutants"` → `ci` if `CI` → `dev`. `db_is_empty` autouse fixture iterates `apps.get_models()` for the `raffle` app and fails naming the first non-empty model (only for tests using `db`).
- [ ] **Step 5: CI.** `ci.yml`: on push to main → `uv sync --frozen`, `ruff format --check`, `ruff check`, `mypy src tests`, `pytest --cov=conference_drawing -m "not e2e"`; on `workflow_dispatch` and `v*` tags → also `playwright install --with-deps chromium` and `pytest -m e2e`. Actions pinned to full commit SHAs. `concurrency` cancels superseded main runs. `dependabot.yml`: uv, github-actions, weekly.
- [ ] **Step 6: Record spec delta** (repeat-entry thanks page) in the spec's Eligibility section.
- [ ] **Step 7: Pass + gates.** Same pytest command → PASS; `uv run ruff format --check . && uv run ruff check . && uv run mypy src tests` each exit 0.
- [ ] **Step 8: Commit + push.** `git commit -m "Scaffold Django project, ops endpoints, test harness, CI"`.

---

### Task 2: Data model

**Files:**
- Create: `src/conference_drawing/raffle/models.py`, `raffle/timezones.py`, `raffle/text.py`, `raffle/migrations/0001_initial.py` (generated), `tests/test_models.py`, `tests/test_timezones.py`, `tests/test_text.py`, `tests/factories.py`

**Interfaces:**
- Produces (models, fields abbreviated — names are binding):
  - `InstallSettings` (singleton, `get() -> InstallSettings`): `owner_name`, `privacy_note`, `followup_ack_text`, `bd_checkbox_text`.
  - `Book`: `title`, `cover` (ImageField, blank), `link` (URLField, blank).
  - `Conference`: `name`, `slug` (unique, `^[a-z0-9]+(-[a-z0-9]+)*$`), `time_zone` (validated in `zoneinfo.available_timezones()`), `booth_location`, `starts_on`, `ends_on` (≥ starts_on), `exclusion_emails` (TextField; property `excluded_emails -> frozenset[str]` normalized), `display_token` (default `secrets.token_urlsafe(32)`), `wrapped_up_at` (null), `deleted_entrant_count` (null). Method `tags() -> ConferenceTags` (NamedTuple `all`, `winner`, `nonwinner`, `bd_optin`).
  - `Question`: `conference` FK, `key` (slug, unique per conference), `prompt`, `kind` (`text`|`choice`), `choices` (JSON list[str]), `position`. Function `create_default_questions(conference) -> None`.
  - `Entrant`: `conference` FK, `name`, `email` (normalized), `answers` (JSON dict[str,str]), `followup_ack_text`, `followup_ack_at`, `bd_optin_text`, `bd_optin_at` (null), `created_at`. `UniqueConstraint(conference, email)`.
  - `Drawing`: `conference` FK, `scheduled_at`, `status` (`scheduled`|`open`|`finished`), `started_at`, `cutoff_at`, `closed_at`, `awarded_count`. `UniqueConstraint(conference, condition=Q(status="open"))`.
  - `PrizeLine`: `drawing` FK, `book` FK (PROTECT), `kind` (`physical`|`digital`), `quantity` (≥1), `position`, `url_template`, `code_mode` (`pool`|`shared`|`""`), `shared_code`. `clean()` rejects digital without url_template or code source, and physical with any code fields.
  - `CouponCode`: `prize_line` FK, `code`, `pick` OneToOne(null, SET_NULL). Unique `(prize_line, code)`.
  - `Pick`: `prize_line` FK, `entrant` FK (CASCADE), `slot` (1..quantity), `status` (`pending`|`claimed`|`not_here`|`digital`|`void`), `pool_size`, `drawn_at`, `replaces` FK self (null), `marked_at` (null).
  - `AuditEvent`: `drawing` FK, `kind` (`started`|`picked`|`claimed`|`not_here`|`undo`|`void`|`exhausted`|`closed`), `pick` FK (null, SET_NULL), `at`, `detail` (JSON; ids/counts only).
  - `OutboxJob`: `conference` FK, `kind` (`event`|`subscribers`), `payload` (JSON), `status` (`pending`|`sent`|`failed`), `attempts`, `next_attempt_at`, `last_error_class`, `created_at`, `sent_at`.
- `timezones.to_utc(local: datetime.datetime, tz: str) -> datetime.datetime`; `to_local(utc: datetime.datetime, tz: str) -> datetime.datetime`.
- `text.normalize_email(raw: str) -> str` (strip, NFC, lowercase); `text.validate_single_line_label(value: str, max_len: int = 200) -> str` (raises `ValidationError` on newline, `Cc`/`Cf` categories incl. bidi overrides, over-length; returns stripped).

- [ ] **Step 1: Failing tests.**

```python
def test_conference_tags_follow_scheme(conference_factory):
    c = conference_factory(slug="pycon-us-2027")
    assert c.tags() == ("pycon-us-2027-raffle", "pycon-us-2027-raffle-winner",
                        "pycon-us-2027-raffle-nonwinner", "pycon-us-2027-bd-optin")

def test_unknown_time_zone_rejected(): ...        # full_clean raises on "Mars/Olympus"
def test_ends_before_starts_rejected(): ...
def test_one_open_drawing_per_conference(): ...   # second open → IntegrityError
def test_duplicate_email_per_conference_rejected(): ...
def test_default_questions_exact(conference_factory):
    c = conference_factory(); create_default_questions(c)
    assert [q.key for q in c.question_set.order_by("position")] == ["want_to_learn", "coding_style", "anything_else"]
    assert c.question_set.get(key="coding_style").choices[3] == "AI agents write most of my code; I direct and review"
def test_digital_line_requires_url_and_code_source(): ...
def test_physical_line_rejects_code_fields(): ...
def test_scheduled_local_time_across_dst():
    assert to_utc(datetime(2027, 3, 14, 14, 0), "America/Los_Angeles") == datetime(2027, 3, 14, 21, 0, tzinfo=UTC)
# text.py — examples + Hypothesis
def test_normalize_email_examples(): assert normalize_email("  Alice@Example.COM ") == "alice@example.com"
@given(st.emails()) def test_normalize_email_idempotent(e): assert normalize_email(normalize_email(e)) == normalize_email(e)
@pytest.mark.parametrize("bad", ["a\nb", "x‮y", "z\x00", "n" * 201])
def test_label_rejects(bad): ...
@given(st.text(alphabet=st.characters(categories=["L","M","N","P","Zs"]), min_size=1, max_size=200).filter(str.strip))
def test_label_accepts_printable_single_line(s): assert validate_single_line_label(s) == s.strip()
```

- [ ] **Step 2: Run, watch fail.** `testslot -- uv run pytest tests/test_models.py tests/test_timezones.py tests/test_text.py -q` → FAIL.
- [ ] **Step 3: Implement** models, helpers, factories (plain functions in `tests/factories.py`, exposed as fixtures in `conftest.py`); `uv run python -m conference_drawing.cli makemigrations raffle`.
- [ ] **Step 4: Pass + gates** (same command; ruff; mypy).
- [ ] **Step 5: Commit + push.** `"Add raffle data model"`.

---

### Task 3: Eligibility

**Files:** Create `raffle/eligibility.py`, `tests/test_eligibility.py`

**Interfaces:**
- Consumes: Task 2 models.
- Produces: `eligible_entrant_ids(drawing: Drawing) -> list[int]` — sorted ascending. Rules: entrant in `drawing.conference`; `created_at < drawing.cutoff_at` (strict); `email not in conference.excluded_emails`; no `Pick` in this conference with status `claimed` or `digital`; no `Pick` in *this drawing* with status in `{pending, claimed, not_here, digital}` (`void` does not exclude). Raises `ValueError` if `cutoff_at` is None.

- [ ] **Step 1: Failing tests** — known-answer examples plus one property:

```python
def test_excludes_staff_past_winners_and_late_entries(...)
def test_exclusion_matches_case_insensitively(...)       # list has "Staff@X.com", entrant "staff@x.com"
def test_entry_at_cutoff_is_excluded(...)                # created_at == cutoff_at → out
def test_not_here_in_this_drawing_excluded_but_eligible_next(...)
def test_void_pick_does_not_exclude(...)
def test_other_conference_entrants_never_included(...)
@settings(deadline=None)
@given(population=entrant_population_strategy())        # random mix: flags excluded/won/late/not_here
def test_property_never_returns_ineligible(population): ...  # result ∩ (excluded ∪ won ∪ late ∪ picked-here) == ∅ and result ⊇ the rest
```

- [ ] **Step 2: Run, watch fail.** `testslot -- uv run pytest tests/test_eligibility.py -q`.
- [ ] **Step 3: Implement** as one queryset (`.exclude(...)` chains + `values_list("id", flat=True).order_by("id")`).
- [ ] **Step 4: Break-to-prove.** Temporarily change `<` to `<=` and drop the `void` carve-out; confirm the property and examples fail; revert.
- [ ] **Step 5: Pass + gates; commit + push.** `"Add drawing eligibility rules"`.

---

### Task 4: Drawing service

**Files:** Create `raffle/draw.py`, `tests/test_draw.py`

**Interfaces:**
- Consumes: `eligible_entrant_ids`, models, `outbox.enqueue_won_prize` (Task 12 — until then, `draw.py` takes an `on_digital_pick: Callable[[Pick], None]` parameter defaulting to a no-op; Task 12 wires the real one in the view layer).
- Produces (all `@transaction.atomic`, all take `now: datetime`):
  - `class DrawingError(Exception)` — message is user-facing copy.
  - `start_drawing(drawing: Drawing, *, now, rng: random.Random, on_digital_pick=...) -> list[Pick]` — refuses if another drawing in the conference is open, if status ≠ `scheduled`, or if a `pool` line has fewer unused codes than `quantity` (message: `"{book}: needs {quantity} codes, has {available}."`). Sets `started_at=now`, `cutoff_at=min(scheduled_at, now)`, `status="open"`. Draws lines by `position`, slots 1..quantity; each pick = `rng.choice(eligible_entrant_ids(drawing))`, `pool_size` = len of that list. Digital → status `digital`, assigns next unused `CouponCode` by id (pool) or none (shared). Empty pool → `AuditEvent(kind="exhausted")`, slot left unawarded. Audit `started` + one `picked` per pick. If no physical line, drawing finishes immediately.
  - `mark_claimed(pick: Pick, *, now) -> Pick` — only from `pending`; finishes drawing when every physical slot has a `claimed` pick.
  - `mark_not_here(pick: Pick, *, now, rng) -> Pick | None` — only from `pending` (a second call on the same pick raises `DrawingError("Already marked.")`); sets `not_here`, draws a replacement for the same slot with `replaces=pick`; returns `None` and audits `exhausted` if the pool is empty.
  - `undo_last(pick: Pick, *, now) -> Pick` — drawing must be `open`; `claimed → pending`; `not_here → pending` and its replacement → `void` (refuses if the replacement isn't `pending`). Audits `undo` (+ `void`).
  - `close_drawing(drawing: Drawing, *, now) -> None` — refuses while any physical pick is `pending` (`"Mark every pick Claimed or Not here first."`); otherwise closes with any remaining copies unawarded: sets `finished`, `closed_at`, `awarded_count` = claimed + digital; audit `closed`.

- [ ] **Step 1: Failing tests** (seeded `random.Random(0)`; frozen `now`):

```python
def test_start_sets_cutoff_to_earlier_of_scheduled_and_now(...)
def test_lines_drawn_in_position_order_and_slots_numbered(...)
def test_digital_picks_get_distinct_pool_codes(...)
def test_shared_code_line_assigns_no_coupon_rows(...)
def test_start_refuses_when_codes_short(...)             # message "Pandas Workout: needs 3 codes, has 2."
def test_start_refuses_second_open_drawing(...)
def test_pool_smaller_than_quantity_leaves_slots_unawarded(...)   # 2 entrants, qty 3 → 2 picks + 1 exhausted audit
def test_not_here_draws_replacement_and_keeps_person_eligible_later(...)
def test_not_here_twice_draws_one_replacement(...)
def test_not_here_with_empty_pool_reports_exhausted(...)
def test_undo_not_here_voids_replacement_and_restores_pick(...)
def test_undo_refused_after_replacement_marked(...)
def test_claiming_last_physical_finishes_drawing(...)
def test_close_refuses_with_pending_picks(...)
def test_digital_only_drawing_finishes_at_start(...)
def test_on_digital_pick_called_once_per_digital_pick(...)
def test_audit_detail_contains_no_names_or_emails(...)   # serialize all AuditEvent.detail, assert no entrant name/email substring
@given(...)  # random entrants/quantities/not-here sequences
def test_property_nobody_wins_twice_and_slots_never_exceed_quantity(...)
```

- [ ] **Step 2: Run, watch fail.** `testslot -- uv run pytest tests/test_draw.py -q`.
- [ ] **Step 3: Implement.** Lock with `Drawing.objects.select_for_update().get(pk=...)` at the top of each operation and re-read the pick inside the transaction (SQLite serializes writers; the re-read is what makes the double-tap test pass).
- [ ] **Step 4: Break-to-prove** the property (e.g., skip the this-drawing exclusion); revert.
- [ ] **Step 5: Pass + gates; commit + push.** `"Add drawing service: start, claim, not-here redraw, undo, close"`.

---

### Task 5: Coupon codes and prize URLs

**Files:** Create `raffle/codes.py`, `tests/test_codes.py`

**Interfaces:**
- Produces: `parse_codes(text: str) -> list[str]` — split lines, strip, drop blanks, keep first occurrence order, reject (ValidationError naming the line number) any code with inner whitespace, control chars, or >100 chars. `render_prize_url(template: str, code: str) -> str` — replace every `{code}` with `urllib.parse.quote(code, safe="")`; template without `{code}` returned unchanged.

- [ ] **Step 1: Failing tests:** examples (`"A1\n\n A2 \nA1"` → `["A1","A2"]`; `"bad code"` → error "line 1"); `render_prize_url("https://s.example/c?coupon={code}", "X/1") == "https://s.example/c?coupon=X%2F1"`; Hypothesis: `parse_codes("\n".join(parse_codes(t))) == parse_codes(t)` for valid outputs (idempotent), and `unquote` of the substituted segment round-trips the code.
- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates.
- [ ] **Step 5: Commit + push.** `"Add coupon-code parsing and prize URL rendering"`.

---

### Task 6: Public entry flow

**Files:** Create `raffle/forms.py`, `raffle/views/public.py`, templates `base.html`, `entry.html`, `thanks.html`, `closed.html`, `privacy.html`, `static/raffle/site.css`; modify `urls.py`; tests `tests/test_forms.py`, `tests/test_public_views.py`

**Interfaces:**
- Consumes: `normalize_email`, `validate_single_line_label`, `InstallSettings.get()`, models.
- Produces: URL names `entry` (`/c/<slug>/`), `thanks` (`/c/<slug>/thanks/`), `privacy` (`/privacy/`). `EntryForm(conference: Conference, *args, **kwargs)` with fields `name`, `email`, `followup_ack` (required BooleanField), `bd_optin` (BooleanField, initial False), `website` (honeypot), plus `q_<key>` per Question. `EntryForm.save(now: datetime) -> Entrant | None` — `None` when the email already entered (no changes made).

- [ ] **Step 1: Failing tests:**

```python
def test_form_requires_name_email_and_ack(...)
def test_bd_optin_unchecked_by_default(...)              # rendered input has no "checked"
def test_choice_question_rejects_unknown_value(...)
def test_answers_saved_by_question_key(...)
def test_consent_texts_and_timestamps_recorded(...)      # followup_ack_text == InstallSettings text; bd_optin_at set only if ticked
def test_repeat_entry_with_case_variant_does_not_duplicate(...)  # count stays 1, original name/answers unchanged
def test_repeat_and_new_entries_get_identical_thanks(...) # same status, same body text "Thanks — you're in the drawing."
def test_honeypot_filled_shows_thanks_but_saves_nothing(...)
def test_entry_page_lists_books_and_local_drawing_times(...)  # times rendered in conference tz; must-be-present line exact
def test_thanks_page_repeats_must_be_present_line(...)
def test_wrapped_up_conference_shows_closed(...)
def test_unknown_slug_404(...)
def test_post_rate_limited_per_ip(...)                   # 11th POST in a minute from same client IP → 429 page
def test_rate_limit_uses_rightmost_forwarded_for(...)    # spoofed leftmost XFF entries don't get fresh buckets
def test_entry_never_logs_personal_data(caplog, ...)
```

- [ ] **Step 2: Run, watch fail.** `testslot -- uv run pytest tests/test_forms.py tests/test_public_views.py -q`.
- [ ] **Step 3: Implement.** Rate limit with `django_ratelimit.decorators.ratelimit(key=client_ip, rate="10/m", method="POST", block=True)`; `client_ip(group, request) -> str` takes the rightmost `X-Forwarded-For` entry (Caddy appends the real peer; the app only listens on a unix socket behind Caddy), IPv6 masked to /64. Mobile-first CSS, no inline styles.
- [ ] **Step 4: Pass + gates; commit + push.** `"Add public entry form, thanks, closed and privacy pages"`.

---

### Task 7: Booth drawing screen (phone-first, HTMX)

**Files:** Create `raffle/views/booth.py`, templates `booth/conference_list.html`, `booth/conference.html`, `booth/drawing.html`, `booth/_pick_row.html`, `booth/_drawing_status.html`, `static/raffle/vendor/htmx-2.0.11.min.js` (downloaded from the npm tarball for `htmx.org@2.0.11`, sha256 recorded in a comment in `base.html` and checked by a test); modify `urls.py`; tests `tests/test_booth_views.py`

**Interfaces:**
- Consumes: `draw.*`, `DrawingError`.
- Produces: URL names `booth-home` (`/booth/`), `booth-conference` (`/booth/<slug>/`), `booth-drawing` (`/booth/drawing/<int:pk>/`), POST-only `booth-start` (`…/start/`), `booth-claim` / `booth-not-here` / `booth-undo` (`/booth/pick/<int:pk>/<action>/`), `booth-close` (`…/close/`). All `@login_required`; POST endpoints return `_pick_row.html` / `_drawing_status.html` partials when `HX-Request` is set, else redirect to `booth-drawing`. Digital rows show code and URL only when Drip isn't configured (`drip.config.drip_settings() is None`).

- [ ] **Step 1: Failing tests:**

```python
def test_booth_requires_login(...)                       # every booth URL → redirect to login
def test_start_renders_picks_grouped_by_book(...)
def test_drawing_error_shown_as_message_not_500(...)
def test_claim_returns_partial_for_htmx(...)
def test_not_here_partial_includes_replacement_row(...)
def test_get_on_post_endpoints_405(...)
def test_unknown_pick_404(...)
def test_digital_code_hidden_when_drip_configured(...)
def test_vendored_htmx_matches_recorded_sha256(...)
```

- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates. Buttons ≥ 48px tall; one action per row.
- [ ] **Step 5: Commit + push.** `"Add phone-first booth drawing screen"`.

---

### Task 8: Display view

**Files:** Create `raffle/views/display.py`, templates `display/display.html`, `display/_panel.html`, `static/raffle/display.css`; tests `tests/test_display_views.py`

**Interfaces:**
- Produces: `/display/<token>/` (full page) and `/display/<token>/panel/` (partial; the page polls it with `hx-get` + `hx-trigger="every 2s"`). Token matched with `secrets.compare_digest` against each conference's `display_token`; no match → 404. Panel shows, in order of precedence: the open drawing's non-void, non-not-here picks by book; else a drawing closed within the last 30 minutes; else next scheduled drawing (local time) and entry count. Link to display URL shown on `booth-conference`.

- [ ] **Step 1: Failing tests:** `test_wrong_token_404`, `test_panel_shows_winners_during_open_drawing`, `test_not_here_and_void_picks_not_shown`, `test_panel_shows_next_drawing_and_entry_count_when_idle`, `test_recently_closed_drawing_shown_for_30_minutes`, `test_display_response_no_store`.
- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates.
- [ ] **Step 5: Commit + push.** `"Add booth display view"`.

---

### Task 9: Flyer

**Files:** Create `raffle/views/flyer.py`, template `flyer.html`, `static/raffle/print.css`; tests `tests/test_flyer.py`

**Interfaces:**
- Produces: `booth-flyer` (`/booth/<slug>/flyer/`, login required). Context: entry URL `request.build_absolute_uri(reverse("entry", ...))`, inline QR SVG from `segno.make(url, error="m").svg_inline(scale=10)`, books with covers from all the conference's prize lines (deduplicated), drawing schedule in local time grouped by day, booth location, must-be-present line.

- [ ] **Step 1: Failing tests:** `test_flyer_qr_encodes_entry_url` (decode by regenerating with segno and comparing the SVG path data), `test_flyer_lists_each_book_once`, `test_flyer_schedule_in_local_time`, `test_flyer_has_must_be_present_line`, `test_flyer_requires_login`.
- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates. Print CSS: one US Letter / A4 page, no color dependence.
- [ ] **Step 5: Commit + push.** `"Add printable flyer with QR code"`.

---

### Task 10: Admin setup screens

**Files:** Create `raffle/admin.py`; tests `tests/test_admin.py`

**Interfaces:**
- Consumes: `parse_codes`, `create_default_questions`.
- Produces: admin for `InstallSettings` (singleton: no add if one exists, no delete), `Book`, `Conference` (inline `Question`; `save_model` calls `create_default_questions` on create; read-only `display_token`, `wrapped_up_at`), `Drawing` (inline `PrizeLine`; `scheduled_at` entered as local time in the conference's zone via a form field converted with `to_utc`), `PrizeLine` form with `codes_text` textarea that appends parsed codes and shows "N unused / M total". `Entrant` admin: list + search, no add/change. `OutboxJob` admin: read-only list with a "Retry failed" action.

- [ ] **Step 1: Failing tests:** `test_new_conference_gets_default_questions`, `test_pasted_codes_create_coupon_rows_without_duplicates`, `test_drawing_local_time_saved_as_utc`, `test_install_settings_singleton`, `test_entrant_admin_is_read_only`, `test_retry_failed_action_requeues`.
- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates.
- [ ] **Step 5: Commit + push.** `"Add admin setup screens"`.

---

### Task 11: CSV export

**Files:** Create `raffle/csv_export.py`, `raffle/views/export.py`; tests `tests/test_csv_export.py`

**Interfaces:**
- Produces: `class CsvRow(pydantic.BaseModel)`; `export_rows(conference: Conference) -> list[CsvRow]`; `write_csv(rows: list[CsvRow], questions: list[Question], out: TextIO) -> None`; URL `booth-export` (`/booth/<slug>/export.csv`, login required, `Content-Disposition: attachment; filename="<slug>-entrants.csv"`). Columns in order: `name, email, entered_at` (local ISO 8601 with offset), one per question key in position order, `status` (`winner`|`nonwinner`), `won` (book titles `; `-joined), `digital_codes`, `digital_urls`, `followup_ack_at`, `bd_optin` (`yes`|`no`), `bd_optin_at`.

- [ ] **Step 1: Failing tests:** `test_columns_exact_order`, `test_winner_status_counts_claimed_and_digital_only`, `test_times_in_conference_zone`, `test_formula_cells_are_neutralized` (each of `=1+1`, `+x`, `-x`, `@x`, `\tx`, `\rx` gets a leading `'`), `test_export_requires_login`, Hypothesis `test_roundtrip_through_csv_reader` (rows written then read back equal the neutralized values).
- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates.
- [ ] **Step 5: Commit + push.** `"Add CSV export"`.

---

### Task 12: Drip client, payloads, outbox, worker

**Files:** Create `raffle/drip/{__init__,config,payloads,client,outbox}.py`, `management/commands/drip_worker.py`, `management/commands/outbox_status.py`; modify `views/booth.py` (wire `on_digital_pick`); tests `tests/test_drip_payloads.py`, `tests/test_drip_client.py`, `tests/test_outbox.py`

**Interfaces:**
- Produces:
  - `config.DripSettings(BaseModel)`: `account_id: str` (digits), `api_key: SecretStr`; `drip_settings() -> DripSettings | None` (None unless both env vars set).
  - `payloads.EventPayload(BaseModel)`: `email: EmailStr`, `action: Literal["Won raffle prize", "Entered raffle"]`, `properties: dict[str, str]`, `occurred_at: datetime`. `payloads.SubscriberPayload`: `email`, `first_name` (from name), `tags: list[str]`, `eu_consent: Literal["granted"] | None`, `eu_consent_message: str | None` (validator: both or neither). Serialization omits `None` and never includes `status`.
  - `client.DripClient(settings: DripSettings, http: httpx.Client)`: `record_event(p: EventPayload) -> None` (`POST /v2/{id}/events`, body `{"events":[...]}`), `upsert_subscribers(ps: list[SubscriberPayload]) -> None` (`POST /v2/{id}/subscribers/batches`, ≤1000 per call). Raises `DripError(status_code: int)` on non-2xx; timeouts 10 s.
  - `outbox.enqueue_won_prize(pick: Pick, *, now) -> OutboxJob | None` (None when Drip unconfigured); `outbox.enqueue(conference, kind, payload: dict, *, now) -> OutboxJob`; `outbox.process_due(client: DripClient, *, now, limit: int = 20) -> ProcessResult` (NamedTuple `sent`, `retried`, `failed`); backoff after attempt n: `[60, 300, 1800, 7200]` seconds then 7200; status `failed` after 8 attempts; `last_error_class` = exception class name only; `outbox.pending_count(conference: Conference | None = None) -> int`.
  - `booth-conference` shows `Drip: N pending, M failed` and a POST `booth-retry-failed` button (requeues failed jobs: `status=pending`, `attempts=0`, `next_attempt_at=now`).
  - `drip_worker` command: loop `process_due` every 5 s; clean exit on SIGTERM/SIGINT; `--once` flag for tests. `outbox_status` command: prints `pending=N failed=M`, exits 1 if `pending > 0`.

- [ ] **Step 1: Failing tests** (respx mocks; no real network):

```python
def test_won_prize_event_properties(...)   # {"book_title","coupon_code","prize_url","conference"}; url rendered via render_prize_url
def test_subscriber_payload_never_has_status(...)
def test_eu_consent_only_with_message(...)
def test_client_posts_expected_json_and_user_agent(respx_mock): ...
def test_client_raises_drip_error_on_422(...)
def test_process_due_marks_sent(...)
def test_process_due_backoff_schedule(...)  # attempts 1..4 → next_attempt_at offsets 60,300,1800,7200
def test_job_fails_after_8_attempts(...)
def test_error_record_holds_class_name_only(...)   # DripError / httpx.ConnectTimeout; no email in any field
def test_unconfigured_drip_enqueues_nothing(...)
def test_digital_pick_from_booth_enqueues_one_event(...)
def test_drawing_never_calls_drip_inline(respx_mock, ...)  # start_drawing via view with respx asserting zero calls
def test_worker_once_processes_and_exits(...)
def test_outbox_status_exit_codes(...)
def test_booth_shows_outbox_counts_and_retry_requeues(...)
```

- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates.
- [ ] **Step 5: One real call (manual, with Reuven's OK at execution time).** With the project `.env` holding Drip credentials, run a one-off `uv run conference-drawing shell -c` snippet that `record_event`s a `Won raffle prize` test event to the owner's own address, and confirm 204. Record the outcome in the commit message; no data in the repo.
- [ ] **Step 6: Commit + push.** `"Add Drip client, outbox and worker"`.

---

### Task 13: Wrap-up

**Files:** Create `raffle/wrapup.py`; modify `views/booth.py`, `booth/conference.html`; tests `tests/test_wrapup.py`

**Interfaces:**
- Consumes: outbox, payloads, `Conference.tags()`.
- Produces: `wrap_up(conference: Conference, *, now) -> WrapUpResult` (NamedTuple `entrants`, `winners`, `jobs`). Refuses (`DrawingError`) if any drawing is `open`, any drawing has `pending` picks, or already wrapped up. Sets `wrapped_up_at`. When Drip is configured: enqueues `subscribers` jobs in chunks of 1000 (tags: `all` + `winner`/`nonwinner` + `bd_optin` if opted in; `eu_consent="granted"` and `eu_consent_message=bd_optin_text` only for opt-ins) and one `Entered raffle` event per entrant with properties `conference` + each answer by question key. URL `booth-wrapup` (POST, login, confirmation form with the counts).

- [ ] **Step 1: Failing tests:** `test_wrapup_closes_entry`, `test_tags_per_entrant`, `test_eu_consent_only_for_optins`, `test_entered_event_carries_answers_by_key`, `test_chunks_of_1000`, `test_refuses_with_open_drawing`, `test_refuses_twice`, `test_without_drip_only_closes_entry`.
- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates.
- [ ] **Step 5: Commit + push.** `"Add conference wrap-up and Drip hand-off"`.

---

### Task 14: Deleting a conference's entrants

**Files:** Create `raffle/deletion.py`; modify `views/booth.py`, `booth/conference.html`; tests `tests/test_deletion.py`

**Interfaces:**
- Produces: `delete_entrants(conference: Conference, *, now) -> int`. Refuses if `outbox.pending_count(conference) > 0` or conference not wrapped up. Sets `deleted_entrant_count`, deletes entrants (picks cascade; coupon `pick` → NULL; audit `pick` → NULL), replaces every `OutboxJob.payload` for the conference with `{"redacted": true}`. URL `booth-delete-entrants` (POST, login, typed-slug confirmation).

- [ ] **Step 1: Failing tests:** `test_refuses_with_pending_jobs`, `test_refuses_before_wrapup`, `test_deletes_entrants_and_picks_keeps_drawings_and_counts`, `test_outbox_payloads_redacted`, `test_no_email_left_anywhere_in_db` (dump every table's rows to text, assert no deleted entrant email appears), `test_confirmation_requires_typed_slug`.
- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates.
- [ ] **Step 5: Commit + push.** `"Add entrant deletion"`.

---

### Task 15: `init` command

**Files:** Modify `cli.py`; tests `tests/test_cli.py`

**Interfaces:**
- Produces: `conference-drawing init [--env-file PATH] [--non-interactive]` — writes `DJANGO_SECRET_KEY` (`secrets.token_urlsafe(50)`) and `DJANGO_ALLOWED_HOSTS` (from `--domain`) if absent, file mode 0600, never overwriting existing values; runs `migrate`; creates the superuser interactively, or from `DJANGO_SUPERUSER_USERNAME/EMAIL/PASSWORD` with `--non-interactive`; creates `InstallSettings` if missing. Idempotent. Any other argv → Django `execute_from_command_line`.

- [ ] **Step 1: Failing tests:** `test_init_writes_env_0600`, `test_init_keeps_existing_secret`, `test_init_idempotent`, `test_non_interactive_superuser_from_env`, `test_passthrough_to_manage`.
- [ ] **Step 2–4:** run (fail) → implement → run (pass) + gates.
- [ ] **Step 5: Commit + push.** `"Add init command"`.

---

### Task 16: Deploy, backup and teardown scripts

**Files:** Create everything under `deploy/`; tests `tests/deploy/test_scripts.py`, `tests/deploy/fakes/{ssh,scp,dig}`

**Interfaces:**
- `deploy/up.sh <host> <domain> [--restore <backup.tar.gz>]` (laptop): `set -euo pipefail`; builds wheel (`uv build --wheel`) and `uv export --frozen --no-dev --no-hashes > requirements.txt`; preflight over ssh — refuses if `systemctl --failed` non-empty, `caddy validate --config /etc/caddy/Caddyfile` fails, or `dig +short <domain>` ≠ host's IP; copies `deploy/remote`, systemd units, Caddy template, wheel, requirements and `version.json` (`git describe`, commit, build time, branch) to `/tmp/conference-drawing-<commit>/`; runs `remote/install.sh` there; smoke: `curl -fsS https://<domain>/health`, `/version` commit matches, entry list page 200.
- `remote/install.sh` (server, root): create system user `conference-drawing`; install pinned uv into `/usr/local/bin` if missing; `uv venv --python 3.14 /srv/conference-drawing/venv` (uv-managed Python under `/srv/conference-drawing/python`); install requirements + wheel; `init --non-interactive` on first run (prompts operator over the ssh tty for the owner password; Drip key/account id copied from laptop project `.env` only if present); `collectstatic`; install units (`RuntimeDirectory=conference-drawing`, `ProtectSystem=strict`, `ReadWritePaths=/srv/conference-drawing/data`, `NoNewPrivileges=yes`), `systemctl daemon-reload && enable --now`; render Caddy site file to `/etc/caddy/sites/conference-drawing.caddy.new`, move into place, `caddy validate` — on failure restore the previous file and exit 1 — then `systemctl reload caddy`. Caddy site: `request_body { max_size 6MB }`, `handle /static/* { root … file_server }`, `handle /media/* { … }`, `reverse_proxy unix//run/conference-drawing/web.sock`, log output with `/c/*` query strings stripped.
- `deploy/backup.sh <host>` (laptop): on server `sqlite3 db.sqlite3 ".backup /tmp/…"` + `PRAGMA integrity_check` == `ok`; tar with media; scp to `./backups/<host>-<UTC timestamp>.tar.gz` (`backups/` git-ignored); writes `/srv/conference-drawing/data/last_backup` with the DB's `data_version` so `down.sh` can tell nothing changed since.
- `deploy/down.sh <host>` (laptop): refuses unless `outbox_status` exits 0; runs `backup.sh`; then `remote/uninstall.sh`: disable+remove units and timer, remove Caddy site file, `caddy validate` then reload, remove `/srv/conference-drawing` and the user.
- Snapshot timer: hourly `sqlite3 .backup` into `/srv/conference-drawing/data/snapshots/`, keep newest 48.

- [ ] **Step 1: Failing tests** — scripts run against fakes first on `PATH` (fake `ssh`/`scp` log their argv and replay canned outputs; unknown commands exit 97; default host `example.invalid`):

```python
def test_up_refuses_when_failed_units(...)
def test_up_refuses_when_caddy_invalid(...)
def test_up_refuses_when_dns_mismatch(...)
def test_install_restores_previous_caddy_file_on_validate_failure(...)  # run remote/install.sh's caddy step in a tmp root with fake caddy/systemctl
def test_down_refuses_with_pending_outbox(...)
def test_down_runs_backup_before_uninstall(...)          # order in fake ssh log
def test_scripts_propagate_failure_exit_status(...)      # each fake failure → non-zero exit, no later step runs
def test_no_secret_values_in_ssh_argv(...)               # Drip key travels via stdin, never argv
def test_caddy_template_renders_valid_domain_only(...)   # domain validated against hostname regex before rendering
```

- [ ] **Step 2–4:** run (fail) → implement → run (pass). Also `shellcheck deploy/**/*.sh` clean.
- [ ] **Step 5: Commit + push.** `"Add deploy, backup and teardown scripts"`.

---

### Task 17: End-to-end browser tests

**Files:** Create `tests/e2e/conftest.py`, `tests/e2e/test_flows.py`

**Interfaces:**
- Consumes: everything. Uses pytest-django `live_server`, Chromium, iPhone-sized viewport (390×844) for entry and booth.

- [ ] **Step 1: Write flows** (marked `e2e`):
  - `test_attendee_enters_on_phone` — open entry page, fill form by label, submit, see `Thanks — you're in the drawing.`
  - `test_owner_runs_drawing_with_redraw` — log in, start drawing (2 physical + 1 digital), tap Not here on the first pick, see a replacement row, tap Claimed on all, see drawing finished.
  - `test_display_updates_after_drawing` — open display in a second page; start drawing in the first; `expect(display).to_have_text(...)` (no sleeps).
  - `test_flyer_prints_one_page` — `page.pdf()` page count == 1.
  - `test_no_csp_violations_on_tour` — listen for `securitypolicyviolation` console messages across entry, booth, display, flyer; assert none.
- [ ] **Step 2: Run.** `testslot --heavy -- uv run pytest -m e2e tests/e2e -q` → PASS (these test already-built behavior; to watch each fail, temporarily break the matching template text and revert).
- [ ] **Step 3: Commit + push.** `"Add end-to-end browser tests"`.

---

### Task 18: Docs and release readiness

**Files:** Modify `README.md`; create `docs/drip-setup.md`, `CONTRIBUTING.md`; modify `TODO.md`

- [ ] **Step 1: README** — what it is; quick start (`uv sync`, `uv run conference-drawing init`, `uv run conference-drawing runserver`); conference walkthrough (books → conference → drawings → flyer → booth → wrap-up → export → delete → `down.sh`); deploying with `up.sh` (and "No server?" Droplet + Caddy apt repo steps); configuration table (env vars); privacy notes.
- [ ] **Step 2: `docs/drip-setup.md`** — the two workflows Reuven builds in Drip: (a) trigger event `Won raffle prize` → email using `{{ event.coupon_code }}`, `{{ event.prize_url }}`, `{{ event.book_title }}`; (b) trigger tag `<slug>-raffle` → follow-up; branch on `<slug>-raffle-nonwinner` for the consolation code; branch on `<slug>-bd-optin` (add to Better Developers now) vs. not (notice email, wait, add unless opted out, optionally only non-EU).
- [ ] **Step 3: Gates** — `uv run ruff format --check .` (also formats Markdown code blocks), ruff check, mypy; mutmut on `raffle.eligibility*`, `raffle.draw*`, `raffle.codes*`, `raffle.text*` with `--max-children 2`, `NO_PROXY='*'`, `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`; close real survivors with tests.
- [ ] **Step 4: CI full gate** on main via `workflow_dispatch`; read each job's status.
- [ ] **Step 5: Version + tag** — `version = "0.1.0"` already; tag `v0.1.0` only after Step 4 is green; push tag.
- [ ] **Step 6: Commit + push.** `"Add README, Drip setup guide, contributing notes"`.
