# Conference helpers and owner-neutral defaults Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-conference helpers who can only run drawings, a booth sign-in page, and no owner-specific names or defaults in the code (v0.3.0).

**Architecture:** One access module (`raffle/access.py`) decides owner / helper / none for a user and a conference; every booth, flyer and export view gets its objects through it, so a refusal is always a 404. A `Conference.helpers` many-to-many field holds the list. "Better Developers" names become "newsletter" names with `db_column` kept, so no SQL changes.

**Tech Stack:** Python 3.14, Django 6.1, PostgreSQL, django-ratelimit, HTMX, pytest-django, Hypothesis, Playwright.

**Spec:** `docs/superpowers/specs/2026-11-26-helpers-and-generic-defaults-design.md` (read it; its tables are the requirements).

## Global Constraints

- Python 3.14, Django 6.1, PostgreSQL only; `uv add` / `uv run`.
- `DJANGO_SECRET_KEY=x uv run mypy src tests` (strict) and `uv run ruff format` / `ruff check` pass before every commit.
- 100% branch coverage (`--cov-fail-under=100`); TDD: watch each test fail for the right reason first.
- Tests run only through `~/.local/bin/testslot` (browser tests with `--heavy`), never in parallel, never xdist.
- Every refusal of a booth, flyer or export request is a **404**, for GET, HEAD and POST alike; never 403.
- Owner = active `is_superuser`. Helper of C = any other active user in `C.helpers` while `C.wrapped_up_at is None`. Staff status grants nothing.
- Migrations are add-only or state-only: v0.2.1 must run on the new schema. Renamed fields keep their column with `db_column`.
- Logs carry ids and counts only. No inline scripts or styles (CSP `script-src 'self'`).
- Small commits, one concern each; US English.
- Reserved conference slugs: `login`, `logout`, `drawing`, `pick`.
- Booth login rate limit: 10 POSTs a minute per client address, its own counter (`group="booth-login"`).

## Review Focus

- A newsletter text of only spaces or newlines must hide the checkbox, like an empty one (Task 6).
- A helper removed mid-drawing who taps Claimed (an HTMX POST) must not see a silently stuck page: the 404 carries `HX-Refresh: true` so the page reloads into the 404 (Task 2).
- A superuser added to a conference's helpers stays an owner, and the helper picker never offers superusers or inactive users (Tasks 1, 5).
- A signed-in user who opens `/booth/login/` again is sent to `/booth/`, not shown a second login form (Task 4).
- Only the exact reserved words are refused as slugs: `pick-up-2027` and `login-day` stay valid (Task 4).

---

### Task 1: The access rule and the helpers field

**Files:**
- Create: `src/conference_drawing/raffle/access.py`
- Modify: `src/conference_drawing/raffle/models.py` (Conference gains `helpers`)
- Create: the next migration in `src/conference_drawing/raffle/migrations/` (generated)
- Modify: `pyproject.toml` (`only_mutate` gains `"*/raffle/access.py"`)
- Test: `tests/test_access.py`

**Interfaces:**
- Produces:
  - `Conference.helpers = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="helped_conferences", help_text="People who can run this conference's drawings, until it is wrapped up. Add accounts under Users.")`
  - `class Access(enum.Enum): OWNER, HELPER, NONE`
  - `access_to(user: AbstractBaseUser | AnonymousUser, conference: Conference) -> Access`
  - `is_owner(user) -> bool` (active superuser)
  - `visible_conferences(user) -> QuerySet[Conference]`: all for an owner (ordered `-starts_on, name`, as today); for anyone else, those listing them and not wrapped up; none for anonymous or inactive.
  - `conference_or_404(user, slug: str, *, owner_only: bool = False) -> Conference`
  - `drawing_or_404(user, pk: int) -> Drawing` (select_related conference)
  - `pick_or_404(user, pk: int) -> Pick` (select_related prize_line__drawing__conference)

- [ ] **Step 1: Write the failing tests** in `tests/test_access.py`:
  - `test_access_table` parametrized over every combination of `active`, `superuser`, `staff`, `listed`, `wrapped` (32 cases), expecting: inactive → NONE; active superuser → OWNER (listed or not, wrapped or not); otherwise listed and not wrapped → HELPER; else NONE. Staff never changes the result.
  - `test_anonymous_has_no_access`.
  - `test_visible_conferences_per_role`: owner sees A, B and wrapped W; helper of A and W sees only A; nobody else sees any.
  - `test_lookups_404_for_none_and_for_owner_only`: `conference_or_404(helper_of_A, "b")` and `conference_or_404(helper_of_A, "a", owner_only=True)` raise `Http404`; a missing slug raises `Http404`; `drawing_or_404` and `pick_or_404` raise `Http404` for B's ids and return A's.
- [ ] **Step 2: Run** `~/.local/bin/testslot uv run pytest -q --no-cov tests/test_access.py` — Expected: ImportError (no `access` module).
- [ ] **Step 3: Implement** the field, `makemigrations raffle -n conference_helpers`, and `access.py`. Lookups query the object first, then check `access_to`, raising `Http404` for NONE and, with `owner_only`, for HELPER.
- [ ] **Step 4: Run** the same command — Expected: PASS. Then `makemigrations --check --dry-run` reports no changes.
- [ ] **Step 5: Commit** — "Access rule: owner, conference helper, or none".

### Task 2: Booth, flyer and export views go through the access rule

**Files:**
- Modify: `src/conference_drawing/raffle/views/booth.py`, `views/flyer.py`, `views/export.py`
- Modify: `src/conference_drawing/raffle/templates/raffle/booth/conference.html`
- Modify (fixtures): `tests/test_booth_views.py:50`, `tests/test_csv_export.py:224`, `tests/test_flyer.py:23`, `tests/test_wrapup.py:280`, `tests/test_deletion.py:200,405`, `tests/test_display_views.py:269` — the signed-in "owner" gets `is_superuser=True`
- Test: `tests/test_booth_views.py`

**Interfaces:**
- Consumes: Task 1's `visible_conferences`, `conference_or_404`, `drawing_or_404`, `pick_or_404`, `is_owner`.
- Produces: the conference page context gains `is_owner: bool`.

Owner-only: `export_csv`, `wrapup_page`, `delete_entrants_page`, `retry_failed` (`owner_only=True`). Helper-allowed: `home` (uses `visible_conferences`), `conference_page`, `flyer`, `drawing_page`, `start`, `close`, `claim`, `not_here`, `undo`.

- [ ] **Step 1: Switch the listed fixtures to a superuser**, run those files, and confirm they still pass (they must, before any view changes).
- [ ] **Step 2: Write the failing tests** in `tests/test_booth_views.py`:
  - `test_helper_sees_only_their_open_conferences_on_booth_home`.
  - `test_helper_conference_page_hides_owner_controls`: response contains the display link, the flyer link and the drawings; does **not** contain the hrefs of export, wrap-up, delete-entrants or retry-failed.
  - `test_owner_conference_page_shows_owner_controls`.
  - `test_helper_can_run_a_drawing`: start, claim, not-here, undo, close on A all succeed for A's helper.
  - `test_removed_helper_htmx_tap_gets_404_with_refresh`: remove the helper, POST claim with `HX-Request: true` → 404 and `HX-Refresh: true`.
  - `test_wrapping_up_ends_helper_access`: after setting `wrapped_up_at`, A's conference page is 404 for the helper.
- [ ] **Step 3: Run** them — Expected: FAIL (helpers currently see owner controls; removed helpers aren't refused).
- [ ] **Step 4: Implement**: replace every `get_object_or_404` in these views with the access lookups; pass `is_owner` and wrap the export link, the Drip form, wrap-up and delete-entrants in `{% if is_owner %}`; return `HX-Refresh: true` on a 404 for HTMX requests (one small `Http404` handling point in `booth.py`, or a decorator).
- [ ] **Step 5: Run** `tests/test_booth_views.py tests/test_csv_export.py tests/test_flyer.py tests/test_wrapup.py tests/test_deletion.py tests/test_display_views.py` — Expected: PASS.
- [ ] **Step 6: Commit** in two commits: fixtures ("Booth tests sign in as a superuser owner"), then views and template ("Booth views enforce owner/helper access").

### Task 3: The route crawler

**Files:**
- Test: `tests/test_booth_access_crawler.py`

**Interfaces:**
- Consumes: Task 1 and 2; `conference_drawing.urls.urlpatterns`.

- [ ] **Step 1: Write the crawler**: seed conferences A and B (each with a drawing, a prize line and a pick), and users: owner, helper of A, helper of nothing, staff non-superuser, inactive helper of A, anonymous. Build the URL for every pattern whose route starts with `booth/` from A's and B's slug / drawing pk / pick pk. A pattern the crawler can't fill fails the test with its name. For each user × URL × method (GET, HEAD, POST): owner never gets 404; helper of A gets non-404 exactly for A's helper-allowed routes (Task 2 list) and 404 for everything of B and A's owner-only routes; the other signed-in users get 404 everywhere except `booth-home` (an empty list); anonymous gets a redirect to `booth-login` for every booth page. Methods a view doesn't accept (405) count as "not reached", and are fine for anyone.
- [ ] **Step 2: Run** it — Expected: PASS if Task 2 is complete. Then break one view on purpose (drop `owner_only=True` from `export_csv`), confirm the crawler fails naming that route, and revert.
- [ ] **Step 3: Commit** — "Route crawler: every booth URL for every role".

Note: until Task 4 adds `booth-login`, assert anonymous gets a redirect (302) and Task 4 tightens it to the booth login URL.

### Task 4: Booth sign-in and sign-out, reserved slugs

**Files:**
- Modify: `src/conference_drawing/auth.py` (add `BoothLoginForm`)
- Modify: `src/conference_drawing/urls.py`, `src/conference_drawing/settings.py` (`LOGIN_URL = "booth-login"`)
- Create: `src/conference_drawing/raffle/templates/raffle/booth/login.html`
- Modify: `src/conference_drawing/raffle/templates/raffle/booth/base.html` (sign-out button)
- Modify: `src/conference_drawing/raffle/models.py` (slug validator) + the next migration (state-only)
- Test: `tests/test_booth_login.py`, `tests/test_models.py`, `tests/test_booth_access_crawler.py`

**Interfaces:**
- Produces: URL names `booth-login` (`booth/login/`) and `booth-logout` (`booth/logout/`, POST only, then to `booth-login`), listed **before** `booth/<slug:slug>/`; `BoothLoginForm(AuthenticationForm)` with the username label "Username or email"; `RESERVED_SLUGS = frozenset({"login", "logout", "drawing", "pick"})` in `models.py`.

- [ ] **Step 1: Write the failing tests** in `tests/test_booth_login.py`:
  - non-staff helper signs in by username, and by email in any case → redirect to `/booth/`.
  - inactive user can't sign in.
  - `?next=/booth/a/` returns there; `?next=https://evil.example/` goes to `/booth/` instead.
  - a signed-in user opening `/booth/login/` is redirected to `/booth/`.
  - the 11th POST in a minute from one address gets the app's rate-limit response (429), while `/admin/login/` from the same address still answers normally (separate counters).
  - `/booth/logout/` refuses GET (405) and signs out on POST.
  - every booth page shows a sign-out button to a signed-in user.
  - `tests/test_models.py`: `full_clean` refuses each reserved slug with a message naming it, and accepts `pick-up-2027` and `login-day`.
  - in the crawler, anonymous now redirects to `reverse("booth-login")` with `next`.
- [ ] **Step 2: Run** — Expected: FAIL (no route `booth-login`).
- [ ] **Step 3: Implement**: `LoginView.as_view(template_name="raffle/booth/login.html", authentication_form=BoothLoginForm, redirect_authenticated_user=True)` wrapped in `ratelimit(key=public.client_ip, rate="10/m", method="POST", block=True, group="booth-login")`; `LogoutView` with `next_page="booth-login"`; the slug validator; `makemigrations`.
- [ ] **Step 4: Run** the login, models and crawler tests — Expected: PASS.
- [ ] **Step 5: Commit** in two commits: reserved slugs; booth sign-in/out.

### Task 5: Helpers in the admin

**Files:**
- Modify: `src/conference_drawing/raffle/admin.py` (ConferenceAdmin)
- Test: `tests/test_admin.py`

**Interfaces:**
- Consumes: `Conference.helpers` (Task 1).

- [ ] **Step 1: Write the failing tests**:
  - `test_conference_admin_offers_only_active_non_superusers_as_helpers`: the add page's helpers field lists an active plain user and an active staff user, and not a superuser or an inactive user.
  - `test_saving_helpers_on_a_conference`: posting `helpers=[id]` stores them.
- [ ] **Step 2: Run** — Expected: FAIL.
- [ ] **Step 3: Implement**: `"helpers"` in the main fieldset after `pages`, `filter_horizontal = ("helpers",)`, and `formfield_for_manytomany` limiting the queryset to `is_active=True, is_superuser=False`, ordered by username.
- [ ] **Step 4: Run** `tests/test_admin.py` — Expected: PASS.
- [ ] **Step 5: Commit** — "Admin: pick a conference's helpers".

### Task 6: Owner-neutral names and defaults

**Files:**
- Modify: `src/conference_drawing/raffle/models.py`, `forms.py`, `csv_export.py`, `wrapup.py`, `admin.py`, `templates/raffle/entry.html`, `templates/raffle/booth/wrapup.html`
- Create: the next migration (state-only renames and defaults)
- Modify: tests that use the old names (`tests/test_forms.py`, `test_csv_export.py`, `test_wrapup.py`, `test_models.py`, `test_admin.py`, `test_after.py`, `test_public_views.py`, `test_drip_client.py`)
- Test: `tests/test_migrations_sql.py` (new), plus the files above

**Interfaces (renames, exact):**
- `InstallSettings.bd_checkbox_text` → `newsletter_checkbox_text` (`db_column="bd_checkbox_text"`, `default=""`, `blank=True`, help text "Optional newsletter checkbox on the entry form. Leave blank for none.")
- `Entrant.bd_optin_text` / `bd_optin_at` → `newsletter_optin_text` / `newsletter_optin_at` (`db_column` the old names)
- `DEFAULT_BD_CHECKBOX_TEXT` → removed; `DEFAULT_FOLLOWUP_ACK_TEXT = "Entering means one follow-up email after the conference."`
- `EntryForm.bd_optin` → `newsletter_optin`, present only when `newsletter_checkbox_text.strip()` is non-empty
- `ConferenceTags.bd_optin` → `newsletter_optin`, value `f"{slug}-newsletter-optin"`
- CSV columns `bd_optin`, `bd_optin_at` → `newsletter_optin`, `newsletter_optin_at`
- Wrap-up count label "Newsletter opt-ins"
- `_DEFAULT_QUESTIONS` and `create_default_questions` removed, and their call in `ConferenceAdmin.save_model` (`admin.py:209`); existing questions untouched.
- Tests that built questions with `create_default_questions` (`tests/test_admin.py:105`, `test_forms.py:41`, `test_models.py:172,227`, `test_public_views.py:38`) use a new `tests/factories.py::make_questions(conference) -> list[Question]`, which creates three neutral questions with the same keys and kinds (`want_to_learn` text, `coding_style` choice with two made-up choices, `anything_else` text), so their assertions keep working.

- [ ] **Step 1: Write the failing tests**:
  - form: with blank, and with whitespace-only, newsletter text the form has no `newsletter_optin` field and the page has no checkbox; with text set, ticking it records `newsletter_optin_text` and `newsletter_optin_at` as today.
  - models: a new `InstallSettings` has `newsletter_checkbox_text == ""` and the new follow-up default; `conf.tags().newsletter_optin == "pycon-newsletter-optin"`.
  - admin: a new conference gets **no** questions (replaces `test_new_conference_gets_default_questions`).
  - CSV header contains `newsletter_optin`, `newsletter_optin_at` and no `bd_`.
  - wrap-up sends the `-newsletter-optin` tag; the wrap-up page says "Newsletter opt-ins".
  - `tests/test_migrations_sql.py::test_rename_migration_emits_no_sql`: `call_command("sqlmigrate", "raffle", <name>)` output, ignoring `BEGIN;`, `COMMIT;`, blank and `--` lines, is empty.
  - a source sweep test: no file under `src/` matches `(?i)better developers|lernerpython|\bbd_` except migration files numbered before this one.
- [ ] **Step 2: Run** them — Expected: FAIL.
- [ ] **Step 3: Implement** the renames and defaults; `makemigrations raffle -n newsletter_names`. If `sqlmigrate` shows SQL, rewrite the migration with `SeparateDatabaseAndState` (state ops only).
- [ ] **Step 4: Run** every test file listed above — Expected: PASS.
- [ ] **Step 5: Commit** in three commits: newsletter renames (with migration and SQL test); hide-when-empty and defaults; no default questions.

### Task 7: Browser test, docs, version

**Files:**
- Test: `tests/e2e/test_flows.py` (new test), `tests/e2e/conftest.py` (a helper page fixture, if needed)
- Modify: `docs/admin-guide.md`, `docs/booth-staff-guide.md`, `docs/drip-setup.md`, `docs/server-setup.md` (Passwords and logins), `README.md` if a line changes, `pyproject.toml` (`version = "0.3.0"`) and `uv.lock`

- [ ] **Step 1: Write the failing e2e test** `test_helper_signs_in_and_runs_a_drawing`: a non-staff helper of the conference signs in at `/booth/login/` through the form, opens the conference, sees "Booth display link" and no "Wrap up", starts the drawing and taps Claimed; CSP watch stays empty.
- [ ] **Step 2: Run** `~/.local/bin/testslot --heavy uv run pytest -q --no-cov -m e2e tests/e2e/` — Expected: the new test passes on the finished code; confirm it can fail by pointing it at a wrapped-up conference (404), then restore.
- [ ] **Step 3: Docs**: admin guide replaces the "Helpers" paragraph with the new model (Users → Add user without Staff status; the conference's Helpers field; access ends at wrap-up, on removal, or on deactivation; helpers sign in at `/booth/login/`; what they can and can't do); booth staff guide gains "Signing in" (the address and that the organizer gives them an account); drip-setup uses `<slug>-newsletter-optin` and "your newsletter", and notes the tag was `-bd-optin` before 0.3; server guide notes `owner-password.sh` resets any account, helpers included; admin guide's Install settings list uses the new label and says a blank newsletter text hides the checkbox.
- [ ] **Step 4: Bump** the version to 0.3.0 and `uv lock`.
- [ ] **Step 5: Mutation testing** on `raffle/access.py`: delete `mutants/`, run `NO_PROXY='*' OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES DJANGO_SECRET_KEY=x ~/.local/bin/testslot uv run mutmut run --max-children 2 "conference_drawing.raffle.access*"`; close every real survivor with a test (ignore message-string mutants); delete `mutants/`.
- [ ] **Step 6: Commit** — e2e test; docs (one commit per guide); any mutation-killing tests; version.
