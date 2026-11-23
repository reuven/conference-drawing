# Between Conferences (v0.2.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After wrap-up, a conference's entry URL (and `/`) shows the owner's configurable logo and Markdown, or redirects; the admin login accepts email or username; the docs describe a permanent install.

**Architecture:** Add-only fields on `InstallSettings` and `Conference`; one pure resolver (`raffle/after.py`) applies the per-field fallback; one safe Markdown renderer (`raffle/markup.py`); the existing closed page grows the content. Email login is an auth backend plus the admin's login form. Deploy gains a pre-created `media/logos/` and an owner password script.

**Tech Stack:** Python 3.14, Django 6.1, PostgreSQL, HTMX, pydantic, markdown-it-py (new), Pillow, pytest, Hypothesis, Playwright, bash + shellcheck.

**Spec:** `docs/superpowers/specs/2026-11-23-between-conferences-design.md` (approved 2026-11-23), on top of `docs/superpowers/specs/2026-11-22-conference-raffle-design.md`.

## Global Constraints

- TDD: each test written first and seen failing. `uv run pytest --cov=conference_drawing` keeps **100%** branch coverage. `DJANGO_SECRET_KEY=x uv run mypy src tests` (strict) and `uv run ruff format --check . && uv run ruff check .` pass before every commit.
- Tests run only through `~/.local/bin/testslot -- …`; e2e through `testslot --heavy -- …`. No pytest-xdist. Run the touched test files while working, not the suite.
- No owner-specific text, logo or URL in code, templates or fixtures. Test data uses `example.com`/`example.invalid`.
- CSP stays `script-src 'self'`, `img-src 'self' data:`, with no inline styles or scripts. All new styling goes in the existing static CSS.
- Logs carry ids and counts only. Never log Markdown, URLs or emails.
- Migration `0005_after_conference` is add-only (blank/default columns, no data migration).
- Redirect URL: scheme `https` only (case-insensitive), no userinfo, a host, at most **500** characters, no whitespace or control characters.
- Markdown: at most **20,000** characters. Logo: at most **2 MB**, Pillow formats PNG, JPEG, WEBP, GIF only (no SVG). Uploads are stored as `logos/<uuid4 hex>.<ext>`.
- A wrapped-up conference with nothing configured renders exactly today's closed page: `<h1>{{ conference.name }}</h1>` and `<p>Entries for this conference are closed.</p>`.
- Redirects are `302`. The `/` route with nothing configured is `404`.
- Version **0.2.0** in `pyproject.toml`, `src/conference_drawing/__init__.py` and `uv.lock`.

## Review Focus

1. **Link schemes in other Markdown forms.** `<javascript:alert(1)>` autolinks, reference definitions (`[a][x]` with `[x]: javascript:…`), entity-encoded schemes (`jav&#x61;script:`) and `data:` must never produce an `href`. Task 1 tests each one.
2. **Usernames can contain `@`.** `up.sh` allows `@` in usernames. A username like `ops@example.com` must still log in as that username. Rule: try an exact username match first, and only if none exists and the input contains `@`, try the email. Task 5 tests this.
3. **Hostile logo files.** An SVG or HTML file renamed `.png`, a truncated PNG, and a decompression bomb (a huge pixel count) must each become a form error, never a 500. Task 2 tests these.
4. **HEAD and POST on a wrapped-up entry URL.** `HEAD` behaves like `GET` (a 302 in redirect mode, a 200 page otherwise). `POST` always gets the closed page, never a redirect. Task 3 tests these.
5. **The logo file is already gone.** Replacing or clearing a logo whose file is already missing from disk must not fail the save. Task 4 tests it.

---

### Task 1: Safe Markdown renderer

**Files:**
- Create: `src/conference_drawing/raffle/markup.py`
- Test: `tests/test_markup.py`
- Modify: `pyproject.toml`, `uv.lock` (`uv add markdown-it-py`, current release, hashed like the rest)

**Interfaces:**
- Produces: `render_markdown(text: str) -> str`. It returns an HTML string ready to mark safe, and `""` for empty or whitespace-only input.

- [ ] **Step 1: Write the failing tests** in `tests/test_markup.py`:
  - `test_renders_headings_lists_and_links`: `"## Hi\n\n- [site](https://example.com)"` contains `<h2>Hi</h2>` and `<a href="https://example.com" rel="noopener noreferrer">site</a>`.
  - `test_raw_html_is_escaped`: `"<script>x</script><b>y</b>"` contains `&lt;script&gt;` and no `<script`.
  - `test_images_are_not_rendered`: `"![a](https://example.com/x.png)"` contains no `<img`.
  - `test_mailto_and_http_links_allowed`: both produce an `href`.
  - `test_refused_links_render_as_text` is parametrized over `javascript:alert(1)`, `JAVASCRIPT:alert(1)`, `data:text/html,x`, `vbscript:x`, `/relative`, `#frag`, the autolink `<javascript:alert(1)>`, a reference definition, and the entity-encoded `jav&#x61;script:x`. Each output contains no `href=`. The text survives: a refused inline link's source appears as escaped text.
  - `test_empty_is_empty`: `render_markdown("  \n")` returns `""`.
  - Hypothesis `test_never_emits_active_content`, using `st.text()` mixed with sampled fragments (`"<script>"`, `"javascript:"`, `"](data:"`, `"onerror="`, `"![x]("`). For every input, the output has no `<script`, no `<img`, no `javascript:` or `data:` inside an `href`, and no `on…=` attribute (`re.search(r"<[^>]*\son\w+=", out)` is `None`).
- [ ] **Step 2: Run** `~/.local/bin/testslot -- uv run pytest tests/test_markup.py -q`. Expected: it fails with `ModuleNotFoundError`.
- [ ] **Step 3: Implement** with a module-level `MarkdownIt("commonmark", {"html": False})`. Call `.disable("image")`. Override `validateLink` with one that allows only parsed schemes `https`, `http` and `mailto`, and refuses anything without a scheme. Register a `link_open` render rule that sets `rel="noopener noreferrer"`. Check the API against Context7 (`/executablebooks/markdown-it-py`) before writing it.
- [ ] **Step 4: Run** the same command. Expected: PASS. Then break `validateLink` on purpose (allow everything), watch the Hypothesis test fail, and revert.
- [ ] **Step 5: Commit** with the message "Safe Markdown renderer for owner-written pages".

### Task 2: Fields, validation, migration and resolver

**Files:**
- Modify: `src/conference_drawing/raffle/models.py`
- Create: `src/conference_drawing/raffle/migrations/0005_after_conference.py` (via `makemigrations`)
- Create: `src/conference_drawing/raffle/after.py`
- Test: `tests/test_after.py`; extend `tests/test_models.py`

**Interfaces:**
- Consumes: `render_markdown` (Task 1).
- Produces:
  - in `models.py`:
    - `AfterMode(models.TextChoices)`, with `PAGE = "page"`, `REDIRECT = "redirect"` and `INHERIT = "inherit"`;
    - `validate_redirect_url(value: str) -> None`;
    - `validate_logo(file: File) -> None`;
    - `logo_upload_to(instance: models.Model, filename: str) -> str`, which returns `logos/<uuid4().hex>.<ext>`, where `ext` comes from Pillow's detected format, not the uploaded name;
    - fields `after_mode`, `after_logo`, `after_markdown` and `after_redirect_url` on `InstallSettings` (mode choices are PAGE and REDIRECT, default PAGE) and on `Conference` (mode choices are all three, default INHERIT).
  - in `after.py`:
    - `class AfterContent(BaseModel)` (frozen): `mode: Literal["page", "redirect"]`, `logo_url: str | None`, `html: str`, `redirect_url: str | None`, and the property `is_empty` (no logo and an empty `html`);
    - `resolve_after(site: InstallSettings, conference: Conference | None) -> AfterContent`.

- [ ] **Step 1: Write the failing tests:**
  - `validate_redirect_url`:
    - Accepts `https://example.com/x?y=1` and `HTTPS://Example.com`.
    - Refuses each of these with a `ValidationError`: `http://example.com`, `javascript:alert(1)`, `https://user:pw@example.com`, `https:///nohost`, `https://exa mple.com`, `"https://example.com/\n"`, and a 501-character URL.
    - Hypothesis property: for any text, it raises only `ValidationError`. Anything it accepts parses with `urlsplit(...).scheme.lower() == "https"`, has no `@` in its netloc, and is at most 500 characters.
  - `validate_logo` (build images with Pillow into `SimpleUploadedFile`): a PNG and a WEBP are accepted. These are refused:
    - SVG bytes named `logo.png`;
    - HTML named `logo.png`;
    - a truncated PNG;
    - a file over 2 MB;
    - a decompression bomb (a PNG header claiming 50,000 × 50,000 pixels).
    Each refusal is a `ValidationError`, never another exception.
  - `logo_upload_to`: the name matches `^logos/[0-9a-f]{32}\.png$` for a PNG uploaded as `../../evil name.PNG`.
  - Model `clean()`:
    - The site in REDIRECT mode without a URL raises.
    - A conference in REDIRECT mode with no URL of its own and no site URL raises.
    - A conference in REDIRECT mode with only the site URL passes.
    - A Markdown body of 20,001 characters raises.
  - `resolve_after`, table-driven (`@pytest.mark.parametrize`) over set or blank for each field at both levels:
    - each field falls back separately;
    - INHERIT uses the site's mode;
    - `conference=None` uses only the site's values;
    - a resolved REDIRECT with no URL becomes PAGE;
    - `html` is `render_markdown` of the resolved Markdown;
    - `logo_url` is the file's `.url` or `None`.
- [ ] **Step 2: Run** `~/.local/bin/testslot -- uv run pytest tests/test_after.py tests/test_models.py -q`. Expected: failures (import errors or missing fields).
- [ ] **Step 3: Implement.**
  - Fields are `ImageField(upload_to=logo_upload_to, blank=True, validators=[validate_logo])`, `TextField(blank=True, validators=[MaxLengthValidator(20_000)])` (a TextField's `max_length` is form-only) and `URLField(max_length=500, blank=True, validators=[validate_redirect_url])`.
  - `validate_logo` opens the file with `Image.open`, then `.verify()`, and catches `(OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning)`. Turn `DecompressionBombWarning` into an error with `warnings.catch_warnings()` and `simplefilter("error", Image.DecompressionBombWarning)`. It checks `img.format in {"PNG", "JPEG", "WEBP", "GIF"}`, and the size is checked before opening.
  - Then run `uv run conference-drawing makemigrations raffle -n after_conference` and check that the migration contains only `AddField`s.
- [ ] **Step 4: Run** the same command. Expected: PASS. Break the per-field fallback on purpose (use the conference's value even when it's blank) and watch the table fail, then revert.
- [ ] **Step 5: Commit** with the message "After-conference settings: fields, validation, resolver".

### Task 3: Pages — wrapped-up entry URL and `/`

**Files:**
- Modify: `src/conference_drawing/raffle/views/public.py` (`entry`), `src/conference_drawing/urls.py` (add `path("", public.home, name="home")`)
- Modify: `src/conference_drawing/raffle/templates/raffle/closed.html`
- Create: `src/conference_drawing/raffle/templates/raffle/_after.html`, `src/conference_drawing/raffle/templates/raffle/home.html`
- Modify: the app's existing static CSS (`.after-logo`: max-width 240px, height auto)
- Test: `tests/test_public_views.py`

**Interfaces:**
- Consumes: `resolve_after`, `AfterContent` (Task 2).
- Produces: `public.home(request) -> HttpResponseBase`. The templates get the context key `after` (an `AfterContent`).

- [ ] **Step 1: Write the failing tests:**
  - `test_wrapped_up_entry_with_nothing_configured_is_unchanged`: the response contains exactly today's `<h1>` and `<p>` lines, and no `after-logo` or `<a ` inside the main content.
  - `test_wrapped_up_entry_shows_conference_markdown_and_logo`: the page shows the conference's Markdown as HTML and an `<img class="after-logo" src="/media/logos/…" alt="">` (alt from `install.owner_name` when it's set).
  - `test_wrapped_up_entry_redirects_302` with the conference's URL, and `test_..._uses_site_redirect_when_inheriting`.
  - `test_head_on_wrapped_up_entry_redirects`, and `test_head_on_page_mode_is_200`.
  - `test_post_to_wrapped_up_entry_shows_closed_page_even_in_redirect_mode`: status 200, with the closed text.
  - `test_open_conference_entry_ignores_after_settings`: the form still shows.
  - `test_home_404_when_nothing_configured`, `test_home_page_mode_shows_site_content` (no conference line), and `test_home_redirect_mode_302`.
  - `test_after_pages_keep_security_headers`: CSP, `X-Robots-Tag: noindex, nofollow` and `Cache-Control` match what the other public pages send.
- [ ] **Step 2: Run** `~/.local/bin/testslot -- uv run pytest tests/test_public_views.py -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement.**
  - **Redirect:** `entry` resolves only in the wrapped-up branch. On GET or HEAD in REDIRECT mode it returns `HttpResponseRedirect` (302); otherwise it renders `closed.html` with `after`.
  - **POST:** POST never redirects.
  - **`home`:** it is `@require_http_methods(["GET", "HEAD"])`. It returns a 404 if `after.mode == "page" and after.is_empty`.
  - **Templates:** `_after.html` renders the logo and `{{ after.html|safe }}`, the only `|safe` in the app, with a comment pointing at `markup.py`. `closed.html` keeps its two lines and includes `_after.html` after them.
- [ ] **Step 4: Run** the same command. Expected: PASS. Then grep the templates for `|safe`: exactly one hit.
- [ ] **Step 5: Commit** with the message "Show the owner's content after wrap-up and at /".

### Task 4: Admin and logo file lifecycle

**Files:**
- Modify: `src/conference_drawing/raffle/admin.py` (`InstallSettingsAdmin`, `ConferenceAdmin`)
- Modify: `src/conference_drawing/raffle/models.py` (delete the old files)
- Test: `tests/test_admin.py`, `tests/test_models.py`

**Interfaces:**
- Consumes: the Task 2 fields.
- Produces: `delete_replaced_logo` behavior. It's internal, and nothing else calls it.

- [ ] **Step 1: Write the failing tests:**
  - **Admin fieldsets:**
    - Both admins show an "After a conference" fieldset with the four fields; the conference's is collapsed.
    - The conference's help text includes "Leave blank to use the site-wide settings".
    - `after_logo` shows a thumbnail (`<img` with class `after-logo-thumb`) once a logo exists.
  - **Admin refusals:**
    - Posting the site in REDIRECT mode without a URL is a form error.
    - Posting a conference in REDIRECT mode with neither its own URL nor the site's is a form error.
  - **Logo files on disk** (use `settings.MEDIA_ROOT` set to `tmp_path` and `django_capture_on_commit_callbacks(execute=True)`):
    - Replacing a logo deletes the old file.
    - Clearing it deletes the file.
    - Deleting a conference deletes its logo file.
    - The site's logo is untouched when a conference changes.
    - `test_replacing_a_logo_whose_file_is_missing_saves`: the save succeeds.
- [ ] **Step 2: Run** `~/.local/bin/testslot -- uv run pytest tests/test_admin.py tests/test_models.py -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement.**
  - **Admin:** `fieldsets` on both admins; the thumbnail is a readonly method using `format_html`.
  - **Deleting files:** in each model's `save()`, compare the stored `after_logo.name` with the new one. After `transaction.on_commit`, delete the old name with `default_storage.delete`, which tolerates a missing file. A `post_delete` handler does the same for `Conference`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** with the message "Admin for after-conference content; delete replaced logos".

### Task 5: Log in with email or username

**Files:**
- Create: `src/conference_drawing/auth.py`
- Modify: `src/conference_drawing/settings.py` (`AUTHENTICATION_BACKENDS`), `src/conference_drawing/urls.py` (`admin.site.login_form`)
- Test: `tests/test_auth.py`; keep `tests/test_admin.py::test_admin_login_still_logs_in` passing

**Interfaces:**
- Produces:
  - `class EmailOrUsernameBackend(ModelBackend)`, with `authenticate(request, username=None, password=None, **kwargs) -> AbstractBaseUser | None`;
  - `class AdminLoginForm(AdminAuthenticationForm)`, whose username field label is "Username or email";
  - `AUTHENTICATION_BACKENDS = ["conference_drawing.auth.EmailOrUsernameBackend"]` (it replaces `ModelBackend`, which it extends).

- [ ] **Step 1: Write the failing tests:**
  - Logging in by username works.
  - Logging in by email works, including in mixed case with surrounding spaces (`"  Boss@Example.COM "`).
  - A username containing `@` (`ops@example.com`, whose email is different) logs in by that username.
  - Two active users sharing an email can't log in by that email, though each can still log in by username.
  - An inactive user fails.
  - A wrong password fails.
  - `test_unknown_email_still_runs_the_hasher`: patch `User.set_password` or `make_password` and assert it was called once for an unknown email.
  - The admin login page shows the label "Username or email".
  - An end-to-end Django test: POST `/admin/login/` with the email, expect a `302` to `/admin/`.
- [ ] **Step 2: Run** `~/.local/bin/testslot -- uv run pytest tests/test_auth.py tests/test_admin.py -q -k "login or auth"`. Expected: the new tests fail.
- [ ] **Step 3: Implement.**
  - **Lookup order:** strip the input. Try an exact username match first. If none exists and the input contains `@`, look up `email__iexact`, active users only, and require exactly one.
  - **Password and timing:** check the password with `user.check_password` and `self.user_can_authenticate`. On any miss, run `UserModel().set_password(password)` once, as `ModelBackend` does.
  - **Form:** set `admin.site.login_form = AdminLoginForm` in `urls.py`, next to `admin_login`.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** with the message "Admin login accepts email or username".

### Task 6: Deploy — logos folder and owner password script

**Files:**
- Modify: `deploy/remote/install.sh` (the media folder lines and the layout comment)
- Create: `deploy/owner-password.sh` (laptop side), `deploy/remote/owner_password.sh` (server side)
- Test: `tests/deploy/test_install.py`, `tests/deploy/test_scripts.py`

**Interfaces:**
- Consumes: `run_app`, `require_root`, `valid_host`, `set_target` and `remote` from the existing `deploy/lib.sh` and `deploy/remote/common.sh`.
- Produces: `deploy/owner-password.sh <host> <username>`.

- [ ] **Step 1: Write the failing tests:**
  - `install.sh` creates `media/logos` with mode 0755: add it to the `("static", "media", "media/covers")` loop in `test_install_fresh_lays_out_the_server` and to `PLANTED` (symlink refusal).
  - `owner-password.sh`:
    - refuses a bad username (the same pattern as `up.sh`: `^[A-Za-z0-9@.+_-]{1,150}$`);
    - refuses a missing argument;
    - runs `ssh -t root@<host>` with a command that ends in `remote/owner_password.sh <username>`;
    - never puts anything password-like in argv (assert the fake ssh's recorded argv has no `--password`).
  - `remote/owner_password.sh` runs `conference-drawing changepassword <username>` through `run_app` and refuses when not root. Add it to `ROOT_SCRIPTS`.
  - shellcheck stays clean (the existing test).
- [ ] **Step 2: Run** `~/.local/bin/testslot -- uv run pytest tests/deploy -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement.** Follow `up.sh`'s staging pattern for copying the remote script: stage, run, and clean up in a trap. If the existing `remote` helper can't allocate a tty, add a `remote_tty` helper in `lib.sh` that uses `ssh -t`, with a test.
- [ ] **Step 4: Run** the same command. Expected: 221+ passed.
- [ ] **Step 5: Commit** with the message "Deploy: logos folder; owner-password.sh".

### Task 7: Docs, privacy line, e2e, version

**Files:**
- Modify: `README.md` ("Running a conference", "Deploying to a server", "Privacy notes")
- Modify: `src/conference_drawing/raffle/templates/raffle/privacy.html`, `tests/test_public_views.py`
- Modify: `docs/superpowers/specs/2026-11-22-conference-raffle-design.md` (deployment wording)
- Modify: `tests/e2e/test_flows.py`
- Modify: `pyproject.toml`, `src/conference_drawing/__init__.py`, `uv.lock` (0.2.0)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Write the failing tests:**
  - `test_privacy_page_states_retention`: the page contains "We keep your entry until the organizer deletes it."
  - e2e `test_after_wrap_up_the_entry_url_shows_the_owner_page`:
    1. The site has Markdown with a link to `https://example.com/` and a PNG logo.
    2. The conference is wrapped up.
    3. Visiting `/c/<slug>/` shows the logo image, loaded with status 200 from `live_server`'s media, and the link.
    4. There are no CSP console errors (`is_csp_message`).
  - e2e `test_admin_login_with_email`.
- [ ] **Step 2: Run** `~/.local/bin/testslot -- uv run pytest tests/test_public_views.py -q -k privacy` and `~/.local/bin/testslot --heavy -- uv run pytest -m e2e -q`. Expected: the new tests fail.
- [ ] **Step 3: Implement.**
  - **Privacy page:** the sentence goes before the owner's note.
  - **README:**
    - The permanent routine: wrap up, export, then create the next conference.
    - "Delete entrants" is optional, with what it removes.
    - Step 9 becomes "If you ever retire the install".
    - Document the "After a conference" settings and `deploy/owner-password.sh`.
    - Note that the admin accepts your email or your username.
  - **Main spec:** "ephemeral per conference" becomes "permanent; teardown optional".
  - **Version:** bump to 0.2.0 with `uv lock`.
- [ ] **Step 4: Run** the same commands. Expected: PASS. Run `uv run ruff format --check .`, which also formats the README's code blocks.
- [ ] **Step 5: Commit** in two commits: "Docs and privacy line for a permanent install" and "Version 0.2.0".

## Release (after the final review, with the owner's OK)

- `mutmut` on the changed pure modules: add `*/raffle/after.py`, `*/raffle/markup.py` and `*/auth.py` to `only_mutate`. Delete `mutants/` first, and run with `--max-children 2`.
- Run CI's full gate on the branch (`workflow_dispatch`) and read each job. Then fast-forward `main`, tag `v0.2.0` and delete the branch.
- The owner runs `deploy/up.sh wp2.lerner.co.il drawing.lernerpython.com` (an upgrade). Afterward, check `/version`, `/`, a wrapped-up `/c/<slug>/` if one exists, and the other sites against the baseline.
