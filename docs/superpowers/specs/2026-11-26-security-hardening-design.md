# Security hardening before the public announcement (v0.4.0) — design

Status: decisions made by Reuven in conversation, 2026-11-26; awaiting his
read of this document. Builds on the main spec, the v0.2 spec and the v0.3
spec (`2026-11-26-helpers-and-generic-defaults-design.md`), which stay the
authority for everything not changed here.

## Why

Reuven plans to announce the project as open source. A three-part security
review of 0.3.0 (who can do what; hostile input; data leaving the app,
fairness and the deploy scripts) found no Critical or High problems, four
Medium, eight Low and fourteen Info findings. The full list, with probes, is
in the review's findings file (kept outside the repo); this document is what
we change because of it.

The Medium findings are about fairness and guessing, not data leaks: a helper
can rig a prize, a script can stuff the drawing, and passwords have no rules.

## Goals

1. A helper can't rig a drawing with the normal buttons, and the owner can
   see every undo and void.
2. One person can't enter many times with `+` aliases, and one network can't
   create entries without limit; junk posts can't lock a venue out.
3. Entries are accepted only during the conference's dates.
4. Passwords have rules, and guessing is limited per account as well as per
   address.
5. Helpers can't start a drawing early.
6. The smaller findings below are fixed or documented.

## Non-goals

- Email confirmation before an entry counts (strongest anti-stuffing step;
  costs every attendee a round trip to their inbox at the booth).
- An owner warning about entrants sharing a name or domain (Reuven didn't
  pick it).
- Hiding digital codes from helpers: they keep seeing them when Drip is off,
  because the booth is then the only way a winner gets the code (Reuven,
  2026-11-26).
- Batched Drip events; Drip's per-account hourly limit is handled by backing
  off, not by batching.
- Rotating the display link automatically when a helper is removed (the admin
  guide already says to rotate it).

## Decisions

### 1. Rerolls (Reuven: "each reroll costs a person")

- When a replacement is drawn for a physical slot, the pool excludes anyone
  who has been **voided for that same slot** in this drawing. So Not here →
  Undo → Not here draws from a smaller pool each time and can't come back to
  someone already passed over; it can't loop until a chosen person wins.
- Undo still works for a mis-tap, as today.
- The owner sees the history:
  - a read-only **Audit events** page in the admin, filterable by drawing,
    listing each start, claim, not-here, void, undo and close with its time;
  - on the booth conference page, for the owner only, each drawing's counts
    of undos and voided picks.
- Marking people "Not here" falsely still consumes them; the history is the
  control for that.

### 2. Stuffing (Reuven: "+aliases as one person", "cap created entries per network")

- **One person per mailbox:** a new stored key per entrant: the normalized
  email with any `+tag` removed from the local part and the domain in one
  consistent IDNA form (so `ada+1@bücher.example`, `Ada@xn--bcher-kva.example`
  and `ada@bücher.example` are one person). A second entry with the same key
  gets the same "you're already entered" answer as today. The exclusion list
  matches by the same key. Existing rows get the key in a data migration; the
  column is nullable, so 0.3 can still run on the new schema.
- **Cap on created entries per network:** a new per-conference setting,
  **Entries per network per hour**, default **60**, 0 = no cap. "Network" is
  the same key the rate limiter uses (an IPv4 address, or an IPv6 /64). Only
  entries actually created count; invalid posts, repeats and honeypot posts
  don't. Over the cap, the attendee sees a page saying too many people have
  entered from their network in the last hour and to ask at the booth; the
  owner can raise the number in the admin.
  - *Call, with its tradeoff:* a venue's Wi-Fi, or a mobile carrier's shared
    IPv4 address, is one "network". 60 an hour covers a busy booth on one
    Wi-Fi; a conference that sends everyone through one address at once
    raises it. The admin guide says so.
- **The flood guard stays but stops being a lockout:** the per-address limit
  on entry POSTs rises from 60 to 300 a minute. Its job is to protect the two
  web workers, not to judge entries.

### 3. Entry window (Reuven: "only during the conference dates, not before/after")

- The entry form accepts entries only when today, in the conference's time
  zone, is on or between `starts_on` and `ends_on`. No grace day.
- Before the start date, the entry page says entries open on that date (and
  shows the conference name and the prize list as today). After the end date,
  the existing closed page. Wrapped up stays closed as today.
- *Call:* the owner who wants to test the form before the conference changes
  the dates for the test; the admin guide says so.

### 4. Passwords and login guessing

- Django's four standard password validators, minimum length 12. Existing
  passwords keep working; the rules apply when a password is set (admin, the
  `init` command, `owner-password.sh`).
- A per-account limit on both login pages, shared between them: 10 failed
  attempts per 15 minutes per account name (the typed username or email,
  trimmed and lowercased). Only failures count. Over the limit, the page
  answers the same rate-limited response as the address limit. The per-address
  limits stay.
  - *Tradeoff:* someone who knows the owner's username can keep that account
    locked out by failing on purpose. The window is short, the admin and booth
    pages share it, and a signed-in session isn't affected. Documented.

### 5. Early start (Reuven: "15-minute window for helpers")

- A helper's Start is refused until 15 minutes before the scheduled time,
  with a message saying when it can start. The owner can start any time; more
  than 15 minutes early, the owner confirms on a second screen.
- The Start button shows the scheduled time.

### 6. Smaller fixes

| Finding | Change |
|---|---|
| Debug mode falls back to a fixed, public secret key | With `DJANGO_DEBUG` on and no key, use a random key per process (sessions don't survive a restart, which is fine on a laptop); never a fixed string |
| Forwarded-For trusted from anyone | Honor `X-Forwarded-For` only when the request came through the unix socket (no peer address) or from an address in a new `CD_TRUSTED_PROXIES` setting; otherwise use the peer address |
| Drip worker uses the pass's start time for leases and backoff | Fresh time for each claim and each outcome; a second worker can't claim a job still leased |
| Drip 429s use up retries | On 429, stop the pass and wait for `Retry-After` (capped) before the next |
| Drip request has no overall deadline | An overall deadline per request; `trust_env=False` |
| First name sent to Drip unchecked | Send `first_name` only if it is letters, marks, spaces, `'`, `-` or `.`, up to 40 characters; otherwise leave it out |
| Snapshot "48 hours" is really 48 files | Rotate by age (48 hours) as well as by count, before each dump, so a failing dump can't keep old ones; the default privacy page mentions snapshots and the web server log |
| Up.sh never replaces a changed Drip key | Replace the line when the value differs (so a rotated key reaches the server) |
| Uploaded images keep EXIF (GPS) | Re-encode logos and covers on upload without EXIF, after applying the orientation |
| Book covers accept any image extension, no size cap | Validate covers like logos: type by content, 2 MB cap, random name with the detected extension |
| Rotate-display-token action needs only view permission | Require change permission |
| Fullwidth ＝ ＋ － ＠ pass the CSV formula guard | Treat them like their ASCII forms |
| Oversized or many-field POSTs log ERROR | Log Django's security errors at WARNING |
| PostgreSQL may log a failed statement's values | `log_min_error_statement = panic` in the PostgreSQL drop-in `install.sh` writes |
| systemd units could be tighter | Web: `RestrictAddressFamilies=AF_UNIX`; worker: `AF_UNIX AF_INET AF_INET6`; both: `ProtectProc=invisible`, `CapabilityBoundingSet=`, `SystemCallFilter=@system-service`, `RestrictSUIDSGID=yes` (checked with `systemd-analyze security` in the deploy tests where possible) |

### 7. Docs only

- The booth form can't prove who typed an address: anyone can enter (and opt
  in) someone else's email. Say so in the admin guide and drip-setup, and
  warn against account-wide Drip broadcasts (entrants who didn't opt in are
  still Drip subscribers).
- The entry cap and a venue's shared network (decision 2).
- The account lockout tradeoff (decision 4).

## Release

v0.4.0: behavior changes (entry window, password rules, entry cap). Migrations
stay rollback-safe: a nullable column with a data migration, a new
per-conference setting with a database default, state-only changes otherwise.

## Tests

- Rerolls: a Hypothesis property that Not here / Undo sequences never draw a
  person already voided for that slot, and a known-answer test of the
  reviewer's probe (20 entrants, rerolling can't reach a chosen one once the
  pool is used up).
- Audit history visible to the owner; hidden from helpers.
- The email key: known-answer cases (`+tag`, case, NFC, IDNA), a Hypothesis
  property that the key is idempotent and that two addresses differing only by
  `+tag` or case collide; exclusion by key.
- The cap: created entries counted, junk not; the 61st created entry from one
  network refused with the friendly page; a different /64 unaffected; 0 = no
  cap.
- The window: the day before, the first day, the last day, the day after, in a
  time zone far from UTC.
- Passwords: short ones refused in the admin; the per-account limit shared
  across both login pages, failures only, correct logins not counted.
- Early start: helper refused at 16 minutes early, allowed at 15; owner
  confirms.
- Each smaller fix gets its own test, as in the main spec's style.
- The reviewers' probes are re-run against the fixed code.

## Left to the builder

- Names of the new field, setting and modules.
- The wording of the new pages, within the existing style.
