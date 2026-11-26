# Between conferences (v0.2) — design

Status: approved approach (option B), 2026-11-23. Builds on
`2026-11-22-conference-raffle-design.md` (the "main spec"), which stays the
authority for everything not changed here.

## Why

The install is now **permanent** (Reuven, 2026-11-23): the owner wraps up one
conference and creates the next one in the same install; it is not torn down
between conferences. People keep visiting after a conference ends — mostly by
scanning the QR code on an old flyer, which opens that conference's entry page
`/c/<slug>/`. Today they get a bare "Entries for this conference are closed",
and the bare domain `/` is a 404. That page should instead say who runs the
raffle and point people somewhere useful: for Reuven, the LernerPython logo, a
summary of his offerings, and links to lernerpython.com.

It must be **configurable, not hard-coded**: this is open-source software, and
another owner must be able to put their own logo, text and links there.

## Goals

1. After a conference is wrapped up, `/c/<slug>/` shows the owner's "after"
   content, or redirects to the owner's URL.
2. `/` shows (or redirects to) the site-wide "after" content.
3. The content is set in the admin: site-wide defaults, overridable per
   conference (approach B).
4. Entrants are **kept** by default, per conference; "Delete entrants" stays an
   explicit, optional action (it already is in code; the docs change).
5. The admin login accepts the **email address or the username**.
6. The docs describe the permanent-install routine; the privacy page states
   how long entries are kept; there is a one-command owner password reset.

## Non-goals

- No change to the entry form, drawings, Drip payloads or wrap-up behavior.
- No "before entries open" page: a conference's entry form is open until
  wrap-up, as today.
- No themes, custom CSS, or HTML templates for owners.
- No per-conference privacy text.

## Anti-goals (would make this a failure even if it "works")

- Owner-entered content that can run script or break the CSP (`script-src
  'self'`, `img-src 'self' data:`, no inline styles or scripts).
- Any Reuven-specific text, logo or URL in the code or templates.
- A redirect that can be pointed at a `javascript:` or `data:` URL, or that
  carries `user:password@`.
- Showing winners or any entrant data on the public "after" page.

## Constraints

- Everything in the main spec's Global Constraints still binds: Python 3.14,
  Django 6.1, PostgreSQL only, HTMX, `mypy --strict`, 100% branch coverage,
  TDD, Hypothesis for parsers/validators, Playwright e2e for web-facing
  behavior, logs carry ids and counts only.
- Uploaded images are served by Caddy from `data/media/`. Folders the app
  creates get mode 0770 under the units' `UMask=007`, which Caddy cannot
  read; that is why `install.sh` pre-creates `media/covers/` as 0755. The new
  logo folder must be pre-created the same way.
- Migrations are add-only (new nullable/blank columns), so v0.1.2 code runs on
  the v0.2 schema (rollback safety, release-lessons).

## Approaches considered

- **A. One site-wide setting.** Simplest; one text for every conference.
- **B. Per-conference fields that fall back to the site-wide ones (chosen by
  Reuven).** Lets the text after PyData differ from the text after the HR
  conference, with nothing to fill in when the default fits.
- **C. Owner-editable HTML template.** Rejected: breaks the CSP and gives any
  admin a script-injection path.

## The design

### Data (one add-only migration)

`InstallSettings` (the existing singleton) gains:

| field | type | notes |
|---|---|---|
| `after_mode` | choice `page` / `redirect` | default `page` |
| `after_logo` | `ImageField(upload_to="logos/")`, blank | |
| `after_markdown` | `TextField`, blank | |
| `after_redirect_url` | `URLField(max_length=500)`, blank | https only (see validation) |

`Conference` gains the same four, except `after_mode` has a third choice,
`inherit` (the default), and every other field may be blank:

| field | type |
|---|---|
| `after_mode` | `inherit` / `page` / `redirect`, default `inherit` |
| `after_logo` | `ImageField(upload_to="logos/")`, blank |
| `after_markdown` | `TextField`, blank |
| `after_redirect_url` | `URLField(max_length=500)`, blank |

### Resolution (one pure function, the single source of truth)

`raffle/after.py`: `resolve_after(site: InstallSettings, conference:
Conference | None) -> AfterContent`, where `AfterContent` is a frozen
pydantic model `{mode: Literal["page", "redirect"], logo_url: str | None,
html: str, redirect_url: str | None}`.

- Each field falls back **individually**: the conference's value if set,
  otherwise the site's. (`inherit` mode means "use the site's mode".)
- `conference=None` (the `/` route) uses the site's values only.
- If the resolved mode is `redirect` but no URL resolves, fall back to `page`
  (validation should prevent this; the fallback makes it impossible to 500).

### Validation (one function per rule, used by both models' `clean()`)

- `validate_redirect_url`: scheme exactly `https`; no userinfo; a host; max
  500 characters; no control or whitespace characters. Hypothesis property:
  never raises anything but `ValidationError`; accepts only URLs whose parsed
  scheme is `https` and that have no userinfo.
- Saving `redirect` mode needs a URL that resolves: site `redirect` needs its
  own URL; conference `redirect` needs its own URL or the site's.
- `after_markdown`: max 20,000 characters.
- Logos: Django's `ImageField` validation (Pillow), max 2 MB, formats PNG,
  JPEG, WebP, GIF (SVG refused: it can carry script).

### Rendering Markdown safely

`markdown-it-py` (new dependency, pinned by hash like every other),
CommonMark preset, `html=False` (raw HTML is shown as text), with:

- the **image** rule disabled (`img-src 'self'` would block remote images,
  and images belong in the logo field);
- `validateLink` allowing only `https:`, `http:` and `mailto:` URLs (relative
  links are refused too);
- links rendered with `rel="noopener noreferrer"`.

Rendering happens per request (the text is small). Hypothesis property:
for any input, the output contains no `<script`, no `on…=` attribute, no
`javascript:`/`data:` URL and no `<img`.

### Pages

- `GET /c/<slug>/` for a wrapped-up conference: `redirect` → `302` to the
  resolved URL (302, not 301: the owner can change it); `page` → the
  existing closed template, extended: the conference name and "Entries for
  this conference are closed", then the logo (if any) and the rendered
  Markdown (if any). A POST to a wrapped-up conference behaves as today
  (closed page; no redirect on POST).
- `GET /`: site-wide content. `redirect` → `302`. `page` with a logo or
  Markdown → a page with the logo, the Markdown and the footer, no
  conference line. Nothing configured → `404`, as today. *(Changed in
  v0.2.1, Reuven 2026-11-26: nothing configured shows a built-in page,
  the owner name or "Conference raffle" and how to enter, never a list
  of conferences.)*
- Both pages keep the footer (privacy link, version) and the global headers
  (CSP, `X-Robots-Tag: noindex`, `Cache-Control`).
- Rough look:

  ```
          [ logo ]
    Entries for HR Tech TLV 2026 are closed.
    ────────────────────────────────────────
    <owner's Markdown>
                               Privacy · v0.2.0
  ```

### Admin

- `InstallSettings` admin: an "After a conference" fieldset with the four
  fields and help text explaining the fallback.
- `Conference` admin: the same fieldset, collapsed, with help text "Leave
  blank to use the site-wide settings".
- A logo preview (thumbnail) next to each logo field.

### Email or username login

- An authentication backend: if the typed name contains `@`, look up an
  active user by email, case-insensitively; exactly one match → check the
  password; zero or several → fail. Otherwise, the standard username lookup.
  A failed lookup still runs the password hasher once (as Django's
  `ModelBackend` does), so timing doesn't reveal which emails exist.
- The login form's label reads "Username or email". The existing rate limit
  (10 POSTs a minute per client address) is unchanged.

### Deploy and docs

- `install.sh` pre-creates `data/media/logos/` as 0755 (like `covers/`), with
  a test.
- `deploy/owner-password.sh <host> <username>`: runs Django's
  `changepassword` on the server over `ssh -t`, so the new password is typed
  on the server's prompt and never passes through argv or the laptop's
  files. Tests use the existing fakes.
- README "Running a conference": the permanent routine (wrap up → export →
  create the next conference); "Delete entrants" described as optional, with
  what it removes; "Take the server down" moves to an "If you ever retire the
  install" note. The "after" settings and `owner-password.sh` documented.
- Privacy page: a fixed sentence, "We keep your entry until the organizer
  deletes it", followed by the owner's existing `privacy_note`.
- Main spec: its "ephemeral per conference" wording updated to "permanent;
  teardown optional".

### Tests

- Unit: `resolve_after` field-by-field fallback (table-driven, every
  combination of set/blank at both levels), the redirect-without-URL
  fallback; validators (examples plus Hypothesis); the Markdown renderer
  (examples plus Hypothesis); the auth backend (email, username,
  case-insensitive email, duplicate email refused, inactive refused, timing
  path runs the hasher).
- Views: wrapped-up `/c/<slug>/` page and redirect; `/` page, redirect and
  404; POST to a wrapped-up conference unchanged; headers present.
- Admin: saving redirect mode without a URL is refused at both levels.
- e2e (Playwright): wrap up a conference, visit its entry URL, see the logo
  and the owner's Markdown link; log in to the admin with an email address.
- Deploy: `media/logos/` created 0755; `owner-password.sh` runs
  `changepassword <username>` through `ssh -t` and never puts a password in
  argv.

## Settled during review (builder check, 2026-11-23)

Marked "call" where the designer decided without asking the owner.

- **`/` needs no Caddy change.** The template's catch-all `handle` already
  proxies `/` to the app; only `urls.py` gains the route. Uploaded files are
  served by Caddy's `/media/*` block, which already sends `nosniff` and
  `Content-Security-Policy: default-src 'none'`.
- **Logos are public files** (like book covers), served without an access
  check; that is intended. *Call:* uploads are stored under a random name
  (`logos/<uuid4 hex>.<ext>`), never the owner's file name, and the old file
  is deleted when a logo is replaced or cleared, or when its conference is
  deleted, after the transaction commits.
- **No data migration or backfill.** Rolling back to v0.1.2 ignores the new
  columns, and the old closed page simply shows again.
- **Only wrap-up triggers the "after" content**, as stated in Non-goals.
- *Call:* **Fallback is per field, with no "explicitly none".** A conference
  that sets its own Markdown but no logo shows the site logo. A conference
  that wants a different look sets its own values. The admin help text says
  so.
- *Call:* **With nothing configured, the wrapped-up page is byte-for-byte
  today's:** the conference name and "Entries for this conference are
  closed". A test pins it.
- *Call:* **Email login is wired through the admin's login form only.** A
  subclass of `AdminAuthenticationForm` relabels the field "Username or
  email" and is set as `admin.site.login_form`, and the backend is added to
  `AUTHENTICATION_BACKENDS` ahead of `ModelBackend`. Django doesn't enforce
  unique emails, so two active users sharing one fail, as stated.
- **`owner-password.sh` follows the existing precedent:** `run_app` in
  `deploy/remote/common.sh`, with the app user, venv and env file. A
  `remote/owner_password.sh` runs `changepassword <username>` under `ssh -t`.
  The username is validated with the same pattern `up.sh` uses.
- *Call:* **A refused Markdown link renders as its plain text**, with no
  link.
- **Entries are kept indefinitely until the owner deletes them.** There is
  no automatic purge, and the privacy sentence says exactly that.

## Left to the builder

- Exact help texts and template markup, within the existing styles.
- Whether `AfterContent` lives in `after.py` or next to the models.
- The size of the logo's thumbnail in the admin.

## Release

Version 0.2.0 (a minor bump: new features, add-only migration). Mutation
testing on the changed pure modules (`after.py`, the validators, the auth
backend) before the merge, per CLAUDE.md.
