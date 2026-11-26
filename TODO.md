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
- [x] Release v0.2.0 (mutmut, CI gate, tag v0.2.0 on 4d85e3d)
- [x] Upgrade wp2 to v0.2.0 (2026-11-23; checked against baseline)

## v0.2.1 — default home page, local hosts

Approved by Reuven, 2026-11-26.

- [x] 1. `/` shows a built-in default page when nothing is configured (was 404)
- [x] 2. Debug mode always accepts localhost, 127.0.0.1 and [::1]
- [x] 3. e2e: the default home page
- [x] 4. README and v0.2 spec: no more "404 when nothing configured"
- [x] 6. Conference time zone: a dropdown grouped by region, labeled by city
- [x] 7. Admin forms warn before leaving with unsaved changes
- [x] 8. After a failed save, jump to the first field with an error
- [x] 9. Say that a chosen logo/cover must be picked again after a failed save
- [x] 10. Admin links to the booth: conference page (booth, flyer, entry page) and a Booth link in the header
- [x] 5. Release v0.2.1 (CI gate on the branch, mutmut on timezones.py, tag v0.2.1 on 9e7536e)
- [ ] 11. Upgrade wp2 to v0.2.1 when Reuven is ready (he's testing on localhost first)

## Docs split into four guides (no release)

Approved by Reuven, 2026-11-26.

- [x] 1. `docs/development.md` (Reuven's laptop, checks, releases, layout); CONTRIBUTING.md points to it
- [x] 2. `docs/server-setup.md` (fresh server to running site, Drip, upgrades, backups, rollback, retiring)
- [x] 3. `docs/admin-guide.md` (running conferences in the admin and at the booth, in order)
- [x] 4. `docs/booth-staff-guide.md` (non-technical: what attendees scan, what to tell them, FAQs)
- [x] 5. README.md cut to an overview linking the four; check nothing was lost

## v0.3.0 — conference helpers, owner-neutral defaults

Spec: `docs/superpowers/specs/2026-11-26-helpers-and-generic-defaults-design.md` (approved 2026-11-26)
Plan: `docs/superpowers/plans/2026-11-26-helpers-and-generic-defaults.md`

- [x] Design doc approved
- [x] Implementation plan written
- [ ] 1. Access rule (owner / helper / none) and `Conference.helpers`
- [ ] 2. Booth, flyer and export views enforce it; helpers don't see owner controls
- [ ] 3. Route crawler over every booth URL and role
- [ ] 4. `/booth/login/` and sign-out; reserved slugs
- [ ] 5. Helpers picker in the conference admin
- [ ] 6. Newsletter names, hidden-when-empty checkbox, neutral defaults, no default questions
- [ ] 7. e2e helper flow, docs, mutmut on access.py, version 0.3.0
- [ ] Release v0.3.0 (CI gate on the branch, tag)
- [ ] Upgrade wp2 when Reuven is ready
