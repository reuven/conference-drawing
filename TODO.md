# TODO

Plan: `docs/superpowers/plans/2026-11-22-conference-drawing.md`
Spec: `docs/superpowers/specs/2026-11-22-conference-raffle-design.md`

- [x] Design doc approved
- [x] Implementation plan written
- [x] 1. Project scaffold, settings, ops endpoints, test harness, CI
- [x] 2. Data model
- [x] 3. Eligibility
- [x] 4. Drawing service (start, claim, not-here redraw, undo, close)
- [x] 5. Coupon codes and prize URLs
- [x] 6. Public entry flow
- [x] 7. Booth drawing screen (phone-first, HTMX)
- [x] 8. Display view
- [x] 9. Flyer
- [x] 10. Admin setup screens
- [x] 11. CSV export
- [x] 12. Drip client, payloads, outbox, worker
- [x] 13. Wrap-up
- [x] 14. Deleting a conference's entrants
- [x] 15. `init` command
- [x] 16. Deploy, backup and teardown scripts (wp2 / any systemd+Caddy host)
- [x] 16b. Switch from SQLite to PostgreSQL (Reuven's decision, 2026-11-22)
- [x] 17. End-to-end browser tests
- [x] 18. Docs and release readiness (v0.1.0)

## v0.2.0 — between conferences

Plan: `docs/superpowers/plans/2026-11-23-between-conferences.md`
Spec: `docs/superpowers/specs/2026-11-23-between-conferences-design.md`

- [x] Design doc approved (2026-11-23)
- [x] Implementation plan written
- [x] 1. Safe Markdown renderer
- [x] 2. After-conference fields, validation, migration, resolver
- [x] 3. Pages: wrapped-up entry URL and `/`
- [x] 4. Admin and logo file lifecycle
- [x] 5. Log in with email or username
- [x] 6. Deploy: logos folder, `owner-password.sh`
- [x] 7. Docs, privacy line, e2e, version 0.2.0
- [ ] Release v0.2.0 (mutmut, CI gate, tag, upgrade wp2)
