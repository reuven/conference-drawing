# Development

For working on the code on your own computer. To put the app on a real
server, see [Setting up a server](server-setup.md) instead; nothing here is
needed for that.

## Run it on your laptop

You need [uv](https://docs.astral.sh/uv/), which installs Python 3.14 for you,
and a local PostgreSQL 15 or later. The app uses PostgreSQL only.

1. Install PostgreSQL and start it. On a Mac with Homebrew:

   ```sh
   brew install postgresql@18
   brew services start postgresql@18
   ```

2. Create an empty database, owned by your own login:

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
   work over plain `http://localhost`, and it accepts `localhost`,
   `127.0.0.1` and `[::1]` whatever `DJANGO_ALLOWED_HOSTS` says. A server
   refuses to install with it on.

5. Run the one-time setup, then start the server:

   ```sh
   uv run conference-drawing init --domain localhost
   uv run conference-drawing runserver
   ```

   `init` writes a secret key and the allowed host into `.env`, checks that it
   can reach the database, creates the tables, and asks for the owner's
   username, email, and password. It is safe to run again: it only fills in
   what is missing, and it applies new migrations, so run it after pulling a
   new version.

6. Open <http://localhost:8000/admin/> to set things up and
   <http://localhost:8000/booth/> to run drawings. The
   [admin guide](admin-guide.md) walks through both.

Every setting is listed in the
[configuration reference](server-setup.md#configuration-reference).

## The tests

The tests create their own database, `test_conference_drawing`, on the
PostgreSQL named by the `CD_DB_*` variables. By default they use the local
socket in `/tmp` as your login (the Homebrew setup); set `CD_DB_HOST`,
`CD_DB_PORT`, `CD_DB_USER`, and `CD_DB_PASSWORD` to point somewhere else, such
as a Docker container. For safety the test suite refuses any host that is not
`localhost`, `127.0.0.1`, or a socket directory, and it will not run if a
libpq variable such as `PGHOST` could redirect it.

These are the checks CI runs:

```sh
uv run ruff format --check .
uv run ruff check .
DJANGO_SECRET_KEY=x uv run mypy src tests
uv run pytest --cov=conference_drawing -m "not e2e"
```

- `mypy` runs in strict mode on `src` and `tests`. It loads the Django
  settings, so it needs a secret key (any value) unless your `.env` has one.
- Coverage must be 100%, branches included; the run fails below that.
- The browser tests need a browser once: `uv run playwright install chromium`,
  then `uv run pytest -m e2e`. A plain `uv run pytest` skips them (`-m "not
  e2e"` is in the pytest `addopts`); `uv run pytest -m ""` runs everything.
  The admin's one script, `raffle/static/raffle/admin-forms.js`, is covered
  by `tests/e2e/test_admin_forms.py`, which reads Chrome's own coverage data.
- Never run the tests in parallel (no `pytest-xdist`): they share one
  database.

## How the code is written

- Test first. Write the failing test, watch it fail for the right reason, then
  write the code. Prefer known-answer tests, and Hypothesis properties for
  anything with an invariant (eligibility, drawing, codes, text handling, the
  Markdown renderer, validators).
- Randomness only through an injected `rng`, never a bare `random` call.
- Logs carry ids and counts only: never names, emails, answers, or codes.
- Test data uses made-up `example.com` addresses. Never put real people's data
  in tests, fixtures, issues, or commit messages.
- Use `uv add` and `uv run`, not `pip`. Dependencies are pinned in `uv.lock`.
- US English in code, comments, and docs.
- Small commits, one change each.
- Migrations only add (new columns with a database default, new tables) or
  change Django's own bookkeeping (choices, help text), so the previous
  release still runs on the new schema and a rollback is safe.
- Mutation testing (`uv run mutmut run`) audits the modules listed under
  `only_mutate` in `pyproject.toml`. Run it on the modules a branch changes,
  with `--max-children 2`, and close real survivors with tests. Delete
  `mutants/` first.

## Cutting a release

1. Work on a branch named `release-vX.Y.Z` (never just `vX.Y.Z`: that would
   clash with the tag). Bump `version` in `pyproject.toml` and run `uv lock`.
2. Run CI's full gate on the branch, browser tests included:
   `gh workflow run ci.yml --ref release-vX.Y.Z`. Read every step's result;
   don't trust the watcher's exit status alone.
3. When it is green, fast-forward `main` to the tested commit, tag that commit
   (`git tag -a vX.Y.Z`), push both, and delete the branch.
4. Deploy with `deploy/up.sh` from a clean checkout of `main` (see
   [Upgrading](server-setup.md#upgrading)), then check `/version` and the
   server's other sites.

A tag never moves. If a tag's CI fails, fix it in a new patch version.

## Where things are

- `src/conference_drawing/raffle/` is the app: models, views, templates, the
  drawing logic (`draw.py`, `eligibility.py`), and the Drip client in
  `drip/`.
- `src/conference_drawing/templates/admin/` holds the one override of
  Django's admin templates (it loads the form script and adds the Booth link).
- `deploy/` holds the operator scripts; their tests in `tests/deploy/` run
  them against fake `ssh` and `scp` commands, so they never reach a server.
- `docs/superpowers/specs/` holds the designs and `docs/superpowers/plans/`
  the implementation plans.
