# Conference helpers and owner-neutral defaults (v0.3.0) — design

Status: approved in conversation by Reuven, 2026-11-26 (this document awaits
his read). Builds on the main spec (`2026-11-22-conference-raffle-design.md`)
and the v0.2 spec (`2026-11-23-between-conferences-design.md`), which stay the
authority for everything not changed here.

## Why

**Helpers.** Today every signed-in account can open every booth page of every
conference, past and present, including Wrap up, the CSV export and Delete
entrants. Reuven's words: helpers "should only be allowed to run drawings",
and "someone who was fired from a company, or who volunteered last year,
shouldn't be allowed to view/modify what they're doing this year." Helpers
change from conference to conference, with some overlap.

**Owner-neutral defaults.** This is open-source software. Reuven asked to
remove what is hard-coded about him, LernerPython, or Better Developers, so
another owner gets neutral defaults and names.

## Goals

1. An owner names helpers **per conference**. A helper reaches only the
   conferences they are named on, and only until that conference is wrapped
   up.
2. A helper can run drawings and nothing more (list below).
3. Removing a helper from a conference, or deactivating the account, ends
   their access on their next request, even mid-conference.
4. Helpers never need, or get, the Django admin.
5. No owner-specific text or names in the code, templates, defaults, Drip
   tags, or CSV columns.

## Non-goals

- No per-helper audit trail of who claimed which pick.
- No self-service helper sign-up, invitations, or password-reset email. The
  owner creates the account and hands over the password; the owner resets
  it with `deploy/owner-password.sh`, which works for any username.
- No time-boxed accounts (an expiry date per account).
- No change to the public entry, thanks, privacy and display pages, or the
  drawing algorithm.

## Anti-goals

- A helper reaching any page or action of a conference they aren't named on,
  or of a wrapped-up conference, by any URL, method or id.
- A helper reaching an owner-only action (wrap up, export, delete entrants,
  retry Drip) on their own conference.
- Telling a helper that another conference exists: a refusal looks exactly
  like a missing conference or pick (404).
- A rename that changes a database column in a way the previous release
  can't run on (rollback safety).

## Roles

- **Owner**: an active user with `is_superuser`. Everything, as today.
- **Helper of conference C**: any other active user listed in C's helpers,
  while C is not wrapped up. Staff status doesn't matter: a staff user who
  isn't a superuser is treated as a helper, so access is never granted by
  staff status alone (fail closed).

## Approaches considered

- **A. A per-conference helper list (chosen).** A many-to-many field from
  Conference to User. Matches "per conference, with overlap", and wrap-up
  ends access by itself.
- **B. Django groups and permissions.** Install-wide, so a per-conference
  layer would still be needed on top. More moving parts, no gain.
- **C. Accounts that expire on a date.** Breaks when one person helps at two
  overlapping conferences, and leaves access open between wrap-up and the
  date.

## Access, page by page

One function decides, and every booth view calls it:

`booth_conference(user, conference) -> Access` returning *owner*, *helper*,
or *none*, and a lookup helper that raises `Http404` for *none*. Views that
start from a drawing or pick id resolve its conference first and check that.

| Route | Owner | Helper of this conference (not wrapped up) |
|---|---|---|
| `/booth/` (list) | all conferences | only conferences they help at, not wrapped up |
| `/booth/<slug>/` (conference page) | full page | drawings, flyer link, display link; no export, wrap-up, delete, Drip status or retry |
| `/booth/<slug>/flyer/` | yes | yes |
| `/booth/drawing/<pk>/` and start, close | yes | yes |
| `/booth/pick/<pk>/` claim, not-here, undo | yes | yes |
| `/booth/<slug>/export.csv` | yes | 404 |
| `/booth/<slug>/wrap-up/` | yes | 404 |
| `/booth/<slug>/delete-entrants/` | yes | 404 |
| `/booth/<slug>/retry-failed/` | yes | 404 |
| `/display/<token>/` | public by token, unchanged | unchanged (helpers may see the link: Reuven, 2026-11-26) |
| `/admin/` | yes | no (helpers aren't staff; a staff non-superuser sees no models) |

Everything refused is a **404**, for GET, HEAD and POST alike, never a 403.

## Signing in

- A new **`/booth/login/`** page (Django's `LoginView` with the existing
  `EmailOrUsernameBackend`, so username or email both work), with the same
  rate limit as the admin login (10 POSTs a minute per client address). It
  accepts any active user. `LOGIN_URL` becomes this page, so a signed-out
  visit to a booth page lands here, then returns to the page asked for.
- A **sign-out** button (POST) on booth pages.
- The admin login is unchanged and still refuses non-staff users.
- Owners can keep signing in at `/admin/` and using the Booth link; the
  session is shared.

## Managing helpers (admin)

- The conference admin page gains a **Helpers** field: a two-pane picker
  (`filter_horizontal`) listing active users who aren't superusers, with help
  text: "People who can run this conference's drawings, until it is wrapped
  up. Add accounts under Users."
- Creating a helper account is Django's own **Users → Add user** (username and
  password; leave Staff status off). The admin guide gets a short "Helpers"
  section and the booth staff guide a sign-in line.

## Owner-neutral defaults

| Today | After |
|---|---|
| Install setting `bd_checkbox_text`, default "Also subscribe me to Better Developers" | `newsletter_checkbox_text`, default **empty**; when empty, the entry form shows **no** newsletter checkbox |
| Entrant `bd_optin_text`, `bd_optin_at` | `newsletter_optin_text`, `newsletter_optin_at` |
| Form field `bd_optin` | `newsletter_optin` |
| Drip tag `<slug>-bd-optin` | `<slug>-newsletter-optin` |
| CSV columns `bd_optin`, `bd_optin_at` | `newsletter_optin`, `newsletter_optin_at` |
| Wrap-up count "Better Developers opt-ins" | "Newsletter opt-ins" |
| Three default entry questions (Reuven's own wording, about AI and coding) created for every new conference | **No** default questions; the owner adds their own |
| Follow-up default "Entering means one follow-up email from me after the conference." | "Entering means one follow-up email after the conference." |
| `docs/drip-setup.md` workflow B written around Better Developers | written around "your newsletter" |

- **Renames keep the database columns.** Each renamed model field sets
  `db_column` to its old column name, so the migration changes no SQL and
  v0.2.1 still runs on the new schema. (Changing a Python default is also
  state-only.)
- **Existing data stays as stored.** wp2's Install settings row keeps the
  text it has; Reuven can edit it. Existing conferences keep their questions.
- **What stays:** the copyright line in `LICENSE`, the author in
  `pyproject.toml`, and the project's GitHub address (clone instructions and
  the Drip client's User-Agent). Those name the project's author and home,
  not the owner of an install.
- **The Drip tag change is breaking** for anyone with a Drip workflow keyed
  on `-bd-optin`. Reuven hasn't built his Drip workflows yet; the release
  notes and `drip-setup.md` say so.

## Tests

- **Route crawler (the main guard).** Seed two conferences A and B and an
  owner, a helper of A, a helper of nothing, a staff non-superuser, and an
  inactive helper of A. For every URL pattern under `/booth/`, request it
  (GET, HEAD and POST) with A's and B's slugs and ids. Expect: the owner
  reaches all; the helper of A reaches exactly the helper rows of the table
  above for A, and 404 for everything of B and every owner-only route of A;
  everyone else gets 404 or the login redirect. A booth URL pattern the
  crawler doesn't know about fails the test.
- Wrapping up A turns its helper's 200s into 404s; removing the helper does
  the same on the next request.
- `/booth/` lists exactly the right conferences per role.
- `/booth/login/`: username and email both sign in; a non-staff helper can;
  an inactive user can't; the rate limit applies; `next` returns to the booth
  page asked for and refuses off-site addresses.
- The conference page hides owner-only controls from helpers (and the
  crawler proves the routes refuse them anyway).
- Defaults: a new install has an empty newsletter text and the form has no
  checkbox; a set text shows it and records consent as today; a new
  conference has no questions; the Drip tag and CSV columns use the new
  names; the migration emits no SQL for the renames (a test runs
  `sqlmigrate`).
- e2e (Playwright): a helper signs in at `/booth/login/`, runs a drawing, and
  doesn't see Wrap up; the crawler-style refusals stay in the fast suite.
- Mutation testing on the new access module.

## Release

v0.3.0: a new permission model and sign-in page, and breaking renames of a
Drip tag and CSV columns. Migrations stay rollback-safe (one new many-to-many
table; state-only renames and defaults).

## Settled during review (builder check, 2026-11-26)

Marked "call" where the designer decided without asking the owner.

- *Call:* **Reserved slugs.** `booth/<slug>/` would swallow `/booth/login/`
  (and, in principle, `drawing`, `pick`, `logout`). The login and logout
  routes are listed before the slug route, and the conference slug validator
  refuses `login`, `logout`, `drawing` and `pick`. No conference exists on
  wp2 yet, so there is no data to migrate; a test pins each reserved word.
- **"Wrapped up" is `wrapped_up_at` being set**, nothing else. Wrap-up already
  refuses while a drawing is open or a pick is pending, so a helper can't lose
  a drawing mid-claim to a wrap-up.
- **Drawings are created and edited only in the admin**, by the owner.
  Helpers run what the owner prepared; the booth has no create or edit
  controls today, and none are added.
- **The helper picker** limits its choices with a custom queryset (active,
  not superuser). The access rule doesn't trust the list alone: a listed
  account that later becomes a superuser is an owner, and an inactive one
  has no access.
- **`LOGIN_URL`** only affects views that require sign-in: the booth, flyer
  and export. The admin keeps its own login: a signed-in non-staff helper who
  opens `/admin/` gets the admin's login page, as Django does today.
- *Call:* **Rate limits** use the same mechanism as the admin login
  (`django_ratelimit`, keyed by client address, 10 POSTs a minute), with a
  **separate** counter per login page, so failures on one don't lock out the
  other.
- **The renames emit no SQL** on PostgreSQL (the only database). A test runs
  `sqlmigrate` to prove it; if Django's generated migration emits any, it is
  rewritten with `SeparateDatabaseAndState`. The new helper table is
  add-only: v0.2.1 ignores it, so rollback stays safe.
- **No data migration** for the newsletter text: only the default changes.
  Rows already stored keep their text.
- **The sweep is complete.** A search for Reuven, Lerner, LernerPython, Better
  Developers and `bd` in code, templates and docs found only the items in the
  table above, plus the author and project-home lines kept on purpose.

## Left to the builder

- Module and function names, within the access rule above.
- The exact look of the login page and sign-out button, within the existing
  booth styles.
- Whether the conference page hides owner-only controls with one template
  flag or separate includes.
