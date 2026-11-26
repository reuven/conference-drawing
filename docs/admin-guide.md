# Admin guide: running conferences

For the owner of a working install. It follows a conference from setup to
cleanup, in the order you'd do things. (Setting up the server itself is in
[Setting up a server](server-setup.md).)

You work in two places, both under your site's address:

- **The admin**, at `/admin/`, where you set things up: books, conferences,
  drawings, settings. Every admin page has a **Booth** link at the top.
- **The booth**, at `/booth/`, made for your phone at the booth: starting
  drawings, marking winners, wrapping up.

Sign in with your username or email address. Your booth staff can use the
[booth staff guide](booth-staff-guide.md) to answer attendees' questions.

## Once per install: Install settings

In the admin, open **Install settings**:

- **Owner name**: shown on the site's home page and as the logo's alt text.
- **Privacy note**: your text on `/privacy/`, which the entry form links to.
  Say who you are and what happens to the data; the
  [privacy notes](#privacy-and-personal-data) below list what the app does.
  The page always starts with "We keep your entry until the organizer
  deletes it."
- **Followup ack text**: the required checkbox on the entry form (by default
  "Entering means one follow-up email after the conference.").
- **Newsletter checkbox text**: the optional newsletter opt-in checkbox on
  the form, in your own words. It starts blank, and while it is blank (or only
  spaces) the form shows no newsletter checkbox at all.
- **After a conference**: what people see once a conference is over (see
  [After a conference](#after-a-conference)).

## Setting up a conference

### 1. Books

Add each book you'll give away: title, an optional cover image, an optional
link. Books are reused across conferences, so this is mostly a one-time job.

### 2. The conference

Add a conference:

- **Name**, as attendees will see it.
- **Slug**: a short name of lowercase letters, digits, and dashes, such as
  `pycon-us-2027`. It becomes the entry address (`/c/pycon-us-2027/`) and the
  start of the Drip tags.
- **Time zone**: click the list and type a city, such as "Denver", to jump
  there. Drawing times are entered and shown in this zone.
- **Booth location**, shown on the flyer.
- **Starts on** and **ends on**.
- **Exclusion list**: the email addresses of you and your staff, one per
  line. Excluded people can enter (handy for testing the form) but never win
  and are never sent to Drip.

A new conference starts with no entry questions; add any you want on the
conference page. Pick its **Helpers** there too ([see below](#helpers)).
Once the conference is saved, its **Pages** row links to
its booth page, its printable flyer, and its entry page.

If you follow a link away from a form you've typed into, the browser asks
before throwing your changes away. If a save fails, the first field with a
problem is highlighted and everything you typed is kept, except a chosen
image file, which you pick again.

### 3. Drawings

Add each drawing with its date and time, in the conference's time zone. Give
it one or more **prize lines**, each a book, a quantity, and a kind:

- **Physical**: a printed book you hand over at the booth. The winner must be
  there.
- **Digital**: an ebook or course. It has a prize URL (which may contain
  `{code}`) and either a pool of unique codes, pasted one per line, or one
  shared code. The app won't start a drawing whose pool has fewer unused codes
  than the quantity. Digital winners needn't be present: Drip emails them.

### 4. The flyer

On the conference's admin page, open **Printable flyer (QR code)**, then
print it or save it as a PDF from the browser. It shows the QR code, a short
address for typing by hand, the books, the drawing times, and "You must be
present to win a printed book."

### 5. The booth display (optional)

The conference's booth page has a **Booth display link**: open it on a
monitor at the booth. Between drawings it shows the next drawing's time and
how many people have entered; during and after a drawing it shows the
winners' names. Anyone with the link can see those names, so treat it like a
password. If it leaks, select the conference in the admin and run **Rotate
display token**: the old link stops working, and the booth page shows the new
one.

## At the booth

Open `/booth/` on your phone and tap the conference.

1. When a drawing's time comes, open it and tap **Start drawing**. The app
   picks the winners at random from everyone eligible.
2. **Digital** winners are announced at once and emailed through Drip.
3. For each **physical** pick, call the name. Tap **Claimed** if the person is
   there, or **Not here** to draw a replacement straight away.
4. A mis-tap can be undone while the drawing is open, or after the claim that
   finished it (that reopens the drawing), but never after you close the
   drawing yourself or after wrap-up.
5. Tap **Close drawing** when every copy is claimed, or when you decide to
   stop.

Who can be picked:

- One entry covers every drawing at the conference.
- A drawing picks only from people who entered **before** you started it.
- Nobody wins twice at one conference (a claimed book or a digital prize).
- Someone marked **Not here** can still win a later drawing.
- People on the exclusion list never win.

### Helpers

To let someone else run drawings on their own phone:

1. In the admin, open **Users** and add a user. The Add user form asks only
   for a username and a password. On the next screen, fill in the **Email
   address** if you want them to sign in with it; without one they can sign
   in only by username. Leave **Staff status** and **Superuser status**
   unticked: a helper never needs the admin.
2. Open the conference and, in **Helpers**, move that user from the left list
   to the right. Save. (The list shows active accounts that aren't
   superusers.)
3. Give them the username (or email address) and password. They sign in at
   `/booth/login/` and see only the conferences you listed them on.

A helper can open the conference's booth page, the booth display link and the
flyer; start and close drawings; and tap **Claimed**, **Not here**, or undo.
They can't see or use **Download CSV**, **Wrap up**, **Delete entrants**, or
the Drip status and **Retry failed**, and those addresses show them "not
found". Only you, as a superuser, can.

Because helpers see the booth display link, consider rotating it after
someone stops helping: select the conference in the admin and run **Rotate
display token** (see [The booth display](#5-the-booth-display-optional)). The
old link stops working.

A helper's access ends when you take them out of **Helpers**, when you
deactivate the account, or when the conference is wrapped up. If you later
deactivate a helper or make them a superuser, the next save of that
conference removes them from its Helpers list; reactivating the account
doesn't put them back, so add them again. Passwords are reset on the server
([Passwords and logins](server-setup.md#passwords-and-logins)). Signing in
tries 10 times a minute per address. Each booth page has a **Sign out**
button.

**Upgrading from 0.2.x:** before 0.3, a helper was an account with **Staff
status** ticked. Staff status no longer reaches the booth. Add each of those
accounts to the **Helpers** list of every conference they should work on
(they can then untick **Staff status**).

## After the last drawing

1. **Wrap up.** On the conference's booth page, tap **Wrap up**. It shows the
   counts, closes entries, and, if Drip is set up, queues every entrant for
   Drip ([what is sent](drip-setup.md)). A background worker sends the
   queue; the booth page shows how many jobs are pending or failed, with a
   **Retry failed** button.
2. **Export.** Tap **Download CSV**. It holds names, emails, answers, entry
   times, what each person won, and the consent timestamps. It is personal
   data: keep it somewhere private.
3. **Delete (optional).** Once Drip has everything (no jobs pending),
   **Delete entrants** removes the conference's personal data and keeps the
   drawings, prizes, and counts. Nothing deletes entrants for you: entrants
   you keep stay in the install, under their own conference.
4. **Next conference.** Add it in the same admin. Its entrants, drawings, and
   Drip tags are its own; books are reused.

## After a conference

People keep scanning old flyers. Once a conference is wrapped up, its entry
address can show your own page instead of a bare "closed" message, or send
people elsewhere. **Install settings** and each conference have an **After a
conference** section with four fields:

- **Mode**: *page* shows the logo and text below; *redirect* sends visitors
  to the redirect URL. A conference can also *inherit* the site's mode.
- **Logo**: PNG, JPEG, WebP, or GIF, at most 2 MB. SVG is refused (it can
  carry scripts).
- **Markdown**: your text, at most 20,000 characters. Links (https, http, and
  mailto) work; raw HTML and images don't, so put images in the logo field.
- **Redirect URL**: `https` only, for example `https://example.com/books`.

A conference's blank fields use the site's, field by field, so usually you
fill in Install settings once and leave conferences blank.

- A wrapped-up conference's address shows its name, "Entries for this
  conference are closed", then the logo and text (or redirects).
- The site's home page, `/`, shows the site-wide logo and text, or redirects.
  With nothing set, it shows a short built-in page: your owner name (or
  "Conference raffle") and how to enter, with no list of conferences.

## Privacy and personal data

- **What the entry form collects.** Name, email address, answers to the
  optional questions, the follow-up acknowledgement (the wording shown and
  when it was accepted), and, if ticked, the newsletter opt-in (the wording
  shown and when). That box is unchecked by default. Entering the same email
  again changes nothing and shows the same thanks page, so the form can't be
  used to find out who has entered.
- **Who sees names.** Winners' names appear on your phone and on the booth
  display. The flyer and entry pages never show them. Every page tells search
  engines not to index it, and the server's logs never hold names or emails
  (see [Logs](server-setup.md#logs)).
- **What goes to Drip, and when.** Nothing goes to Drip while people enter.
  When a digital prize is drawn, the winner's email gets a `Won raffle prize`
  event with the book title, code, prize URL, and conference name. At wrap-up
  every entrant, except those on your exclusion list, becomes a Drip
  subscriber (email and first name, never a subscription status) with the
  conference's tags, plus an `Entered raffle` event holding the conference
  name and their answers. Only people who ticked the newsletter box carry an
  EU consent grant, with the exact wording they saw. Physical-prize winners
  get no email from the app. Details: [Setting up Drip](drip-setup.md).
- **Deleting.** "Delete entrants" removes every entrant of the conference, and
  with them their picks, the admin-history rows about them, and any failed
  Drip jobs; the remaining Drip jobs keep a row but lose their contents. It
  also empties the rate-limit cache, whose keys are hashes of visitors' IP
  addresses (easy to reverse for IPv4); otherwise each key expires after
  about a minute but its row can stay in the table until overwritten. It
  then tries to rewrite the affected database tables so the deleted rows leave
  the files. That rewrite is best effort: it gives up (and only logs it) if a
  table stays locked for more than 5 seconds, and even when it runs, the
  database's write-ahead log files can still hold the rows until they are
  recycled, and older snapshots hold them until they age out (next point). It
  does not touch Drip: what is in Drip is managed in Drip. The drawings,
  prizes, and counts stay.
- **Backups still hold what you deleted.** Anything saved **before** a
  deletion still contains the deleted people until it ages out or you delete
  it. The server's hourly snapshots, and the one taken before each upgrade,
  keep the newest 48, so deleted entrants can live on there for up to 48
  hours. Backups on your laptop (`backups/`) never age out and are not
  encrypted: delete entrants **before** taking one you mean to keep, and
  delete old backups yourself.
