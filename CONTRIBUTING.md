# Contributing

Thanks for helping. This is a small project with strict checks; they are the
same ones CI runs.

## Set up

You need [uv](https://docs.astral.sh/uv/) and a local PostgreSQL 15 or later
(Homebrew's `postgresql@18` is what the maintainer uses).

```sh
uv sync
```

The tests create their own database, `test_conference_drawing`, on the
PostgreSQL named by the `CD_DB_*` variables. By default they use the local
socket in `/tmp` as your login (the Homebrew setup); set `CD_DB_HOST`,
`CD_DB_PORT`, `CD_DB_USER`, and `CD_DB_PASSWORD` to point somewhere else, such
as a Docker container. For safety the test suite refuses any host that is not
`localhost`, `127.0.0.1`, or a socket directory, and it will not run if a
libpq variable such as `PGHOST` could redirect it.

## The checks

Run these before every commit:

```sh
uv run ruff format --check .
uv run ruff check .
DJANGO_SECRET_KEY=x uv run mypy src tests
uv run pytest --cov=conference_drawing -m "not e2e"
```

- `mypy` runs in strict mode on `src` and `tests`.
- Coverage must be 100% (branches included); the run fails below that.
- The browser tests need a browser once: `uv run playwright install chromium`,
  then `uv run pytest -m e2e`. CI runs them on manual dispatch and on release
  tags.
- Do not run the tests in parallel (no `pytest-xdist`); they share one
  database.

## How we work

- Test first. Write the failing test, watch it fail for the right reason, then
  write the code. Prefer known-answer tests and Hypothesis properties for
  anything with an invariant (eligibility, drawing, codes, text handling).
- Randomness only through an injected `rng`, never a bare `random` call.
- Logs carry ids and counts only: never names, emails, answers, or codes.
- Test data uses made-up `example.com` addresses. Never put real people's data
  in tests, fixtures, issues, or commit messages.
- Use `uv add` and `uv run`, not `pip`. Dependencies are pinned in `uv.lock`.
- US English in code, comments, and docs.
- Mutation testing (`uv run mutmut run`) audits the tests of
  `raffle/eligibility.py`, `draw.py`, `codes.py`, and `text.py`. Run it on
  modules you change, and close real survivors with tests.
- One logical change per commit.

## Layout

See `docs/superpowers/specs/` for the design and `docs/superpowers/plans/`
for the implementation plan. The Drip integration lives in
`src/conference_drawing/raffle/drip/`; the deploy scripts in `deploy/` have
their own tests in `tests/deploy/`, which run them against fake `ssh` and
`scp` commands.
