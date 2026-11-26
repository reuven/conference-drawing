# Setting up Drip

The app does not send email. It records events and tags in your
[Drip](https://www.drip.com/) account, and two workflows that you build in
Drip send the messages. This page lists exactly what the app sends, then the
two workflows.

The app only ever adds tags in the scheme below. It never removes or renames
a tag, never sends a subscriber status (so it cannot resubscribe someone who
unsubscribed), and never adds anyone to a Drip campaign or to your
newsletter itself.

## Before you start

1. Put your Drip account id (digits only) and API key in the `.env` of the
   checkout you deploy from, as `DRIP_ACCOUNT_ID` and `DRIP_API_KEY`. `up.sh`
   copies them to the server. Set both, or neither.
2. Drip jobs are queued in the app's database and sent by a background
   worker, so Drip being slow or down never delays a drawing. The conference
   page at `/booth/<slug>/` shows how many are pending or failed and has a
   "Retry failed" button.

## What the app sends

### Event: `Won raffle prize`

Recorded when a **digital** prize is drawn, for the winner's email, at the
time of the pick. Its properties:

| Property | Value |
|---|---|
| `book_title` | The book's title. |
| `coupon_code` | The winner's code from the pool, or the line's shared code; empty if the line has none. |
| `prize_url` | The prize URL with `{code}` replaced by the (percent-encoded) code. |
| `conference` | The conference's name. |

In a Drip email or workflow, read them as `{{ event.book_title }}`,
`{{ event.coupon_code }}`, `{{ event.prize_url }}`, and
`{{ event.conference }}`. See Drip's help article
[Call Event Properties with Liquid](https://help.drip.com/hc/en-us/articles/4424710466957-Call-Event-Properties-with-Liquid).
Event properties are only available in a workflow that an event started, so
use the event as the trigger (workflow A below). Physical prizes send nothing.

### Wrap-up: subscribers, tags, and an event

When you tap "Wrap up", every entrant **except those on the conference's
exclusion list** is created or updated as a subscriber (email and first name,
no status), in batches of up to 1000. Each gets these tags, where `<slug>` is
the conference's slug (for example `pycon-us-2027`):

| Tag | Applied to |
|---|---|
| `<slug>-raffle` | every entrant sent |
| `<slug>-raffle-winner` | winners: a Claimed physical pick, or any digital pick |
| `<slug>-raffle-nonwinner` | everyone else |
| `<slug>-newsletter-optin` | entrants who ticked the newsletter checkbox (the text you set in Install settings) |

Before version 0.3 this tag was `<slug>-bd-optin`. Conferences wrapped up
earlier keep that tag in Drip, and a workflow that branches on it needs the
new name for conferences from 0.3 on.

Only people who ticked that box also carry an EU consent grant, with the
wording they saw. Everyone else carries no consent fields.

Each entrant also gets an `Entered raffle` event, dated when they entered.
Its properties are `conference` (the conference's name) plus one property per
entry question you added, named by the question's key (for example
`want_to_learn`), holding the answer they gave. A conference with no
questions sends none. Use these
to personalize the follow-up: `{{ event.want_to_learn }}`.

## Workflow A: send the prize

Trigger: **Recorded a custom event**, event name `Won raffle prize`.

1. Send an email that uses `{{ event.book_title }}`, `{{ event.coupon_code }}`,
   and `{{ event.prize_url }}`. For example: "You won *{{ event.book_title }}*
   at {{ event.conference }}. Claim it here: {{ event.prize_url }} (your code
   is {{ event.coupon_code }})."

The code travels in the event, not in a subscriber custom field, so it is
never overwritten when someone wins at a later conference.

## Workflow B: the follow-up after the conference

Trigger: **Applied a tag**, tag `<slug>-raffle`. Build one per conference, or
one for each tag you use as a trigger. All the entrants arrive together at
wrap-up, so the tag is applied to everyone at about the same time.

1. **Send the follow-up email.** The entry form promised exactly one.
2. **Branch on `<slug>-raffle-nonwinner`.** If the person has the tag, send
   the consolation message with the consolation code. The app never knows
   that code; it lives in Drip. Winners skip this branch.
3. **Branch on `<slug>-newsletter-optin`.**
   - **Has the tag:** add them to your newsletter now. They asked for it,
     and the app recorded their consent.
   - **Does not have the tag:** send a short notice that you will add them to
     your newsletter and how to remove themselves, wait (a week is
     typical), then add them unless they opted out. If your newsletter has no
     double opt-in, this notice is the opt-out path. An opt-out notice is
     not consent under the GDPR, so you may want to limit this branch to
     entrants outside the EU.

## Checking it worked

- After wrap-up, wait for the conference page to show no pending jobs, then
  look in Drip for the tags on a test subscriber. Your own address is on the
  exclusion list, so it will not appear; enter and wrap up a test conference
  with a second address you control to see the whole path.
- To test workflow A, draw a digital prize in a test conference using a
  second address you control.
