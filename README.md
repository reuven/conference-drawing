# conference-drawing

A self-hosted web app for giving away books at a conference booth. Visitors
scan a QR code and enter on their phones; you run the drawings from your own
phone at the booth; afterward the entrants go to
[Drip](https://www.drip.com/) with tags, and Drip sends the follow-up email.

One install has one owner and serves one conference after another. The owner
can add helpers who sign in to run a conference's drawings.

What it does:

- Sets up a conference: dates, time zone, booth location, an exclusion list
  (you and your staff), optional entry questions, and a schedule of drawings
  with prizes (a book, a quantity, printed or digital).
- Prints a flyer with a QR code, the books, the schedule, and the "you must be
  present" line.
- Takes entries on a phone-friendly form, one entry per email address, with a
  consent record for each.
- Runs each drawing from a phone: fair random picks, Claimed / Not here for
  printed books with an immediate redraw, and digital prizes sent through
  Drip. An optional full-screen display works for a booth monitor.
- After the conference, wraps up: every entrant goes to Drip with tags, and
  you can export a CSV and delete the entrants. The conference's address then
  shows your own page, or redirects wherever you like.

## The guides

| If you are… | Read |
|---|---|
| Putting the app on a server | [Setting up a server](docs/server-setup.md) |
| Running conferences on a working server | [Admin guide](docs/admin-guide.md) |
| Helping at the booth | [Booth staff guide](docs/booth-staff-guide.md) |
| Working on the code | [Development](docs/development.md) |

Connecting Drip, and exactly what the app sends there, is in
[Setting up Drip](docs/drip-setup.md). The designs are in
[`docs/superpowers/specs/`](docs/superpowers/specs/).

## Security

Hardening for the entry window, per-network limits, passwords, sign-in
lockout, and the server units is described in the
[admin guide](docs/admin-guide.md) and
[Setting up a server](docs/server-setup.md). What the app sends to Drip, and
what to avoid doing there, is in [Setting up Drip](docs/drip-setup.md).

To report a vulnerability, please do not open a public issue. Use GitHub's
private vulnerability reporting: on the repository's **Security** tab, choose
**Report a vulnerability** (a security advisory).

Licensed under the MIT License.
