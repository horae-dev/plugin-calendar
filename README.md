<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-calendar

Horae plugin: calendars from ICS URLs, CalDAV and the Google Calendar API, with preparation tasks.

> 🚧 **In progress.** The ICS, CalDAV and Google Calendar API sources (`calendar.events`) are in. Preparation tasks (`calendar.prep_tasks`) are in. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

It declares every host, secret, path and intent it uses in `plugin.yaml`, and it only returns
data, except the `calendar.prep_tasks` transform, which proposes `task.create`; the
`calendar.events` source proposes no intent.

## `calendar.events`

The events of a window of whole days, read from each calendar's ICS feed (a Proton Calendar
share link, a Google Calendar "secret address in iCal format", or any other ICS URL on a host
you allow) or read from a **CalDAV** calendar (iCloud with an app-specific password, Fastmail,
Nextcloud...) or from the **Google Calendar API** (OAuth), with recurring events expanded.

```yaml
# pipelines/daily-brief.yaml
steps:
  - collect:
      - calendar.events:
          calendars:
            - { name: Personal, index: 1 }   # the first URL of CALENDAR_ICS_URLS
            - { name: Family, index: 2 }     # the second
            - { name: iCloud, index: 1, type: caldav }   # the first account of CALENDAR_CALDAV
            - { name: Google, index: 1, type: google }   # the first account of CALENDAR_GOOGLE, primary calendar
          start: "{{date}}"         # the run's date
          timezone: "{{timezone}}"  # the instance's (or the trigger's) time zone
          days: 2
```

Every parameter is optional: `calendar.events: { days: 2 }` reads every URL of the secret,
named `calendar-1`, `calendar-2`..., over today and tomorrow in the instance's time zone.

| Parameter | Default | Meaning |
|---|---|---|
| `calendars` | every URL of the secret | 1 to 16 calendars, each `{name, index}`: a display name (1 to 64 printable characters, unique) and the 1-based place of its feed URL in the `CALENDAR_ICS_URLS` secret (unique). `type` is `ics` (the default; `index` is the place in `CALENDAR_ICS_URLS`) or `caldav` (`index` is the place in `CALENDAR_CALDAV`, see [CalDAV](#caldav)); `google` (`index` is the account in `CALENDAR_GOOGLE`, see [Google Calendar API](#google-calendar-api); add `calendar_id`, default `primary`, to read another calendar of the account); each type counts its own indexes. Without `calendars`, every ICS URL is read, named `calendar-<index>`. |
| `start` | today in `timezone` | The first day, `YYYY-MM-DD`. Pass `"{{date}}"`: a replayed run then reads the same window, where the default depends on the day it runs. |
| `days` | `2` | Whole days, 1 to 62. |
| `timezone` | the instance's (`TZ`) | IANA time zone of the window and of the times returned. The host sets the plugin's `TZ` to the instance's time zone; `UTC` when `TZ` is not an IANA name. Pass `"{{timezone}}"` to follow the trigger's. |
| `allowed_hosts` | none | Feed hosts allowed besides the declared ones. They must match what you approved in `horae.lock` (enforced by the core from M5); see [Other hosts](#other-hosts). |
| `partial` | `false` | When a calendar fails, return the others' events (`complete: false`) instead of raising. See [Failures](#failures). |

### Output

```json
{
  "window": {"start": "2026-10-26T00:00:00+01:00", "end": "2026-10-28T00:00:00+01:00",
             "timezone": "Europe/Paris", "days": 2},
  "complete": true,
  "truncated": 0,
  "calendars": [{"name": "Personal", "ok": true, "complete": true, "events": 1,
                 "skipped": 0, "truncated": 0, "warnings": 0}],
  "events": [
    {"id": "calendar:standup-1-example.org:20261026T083000Z::d95bd187c70…",
     "calendar": "Personal", "title": "Team standup",
     "start": "2026-10-26T09:30:00+01:00", "end": "2026-10-26T09:45:00+01:00",
     "all_day": false, "location": "Room 1", "description": null,
     "status": "confirmed", "recurring": true}
  ]
}
```

- **Events** are those that meet the window (an event that started before it and ends in it
  is included), sorted by start, all-day events first on their day. A cancelled instance or
  event (`STATUS:CANCELLED`, an `EXDATE`) is left out; `status` is `confirmed` or
  `tentative`.
- **Times.** A timed event's `start` and `end` are RFC 3339 date-times in the window's time
  zone. A floating time (no time zone in the feed) is read in the feed's `X-WR-TIMEZONE`
  when it has one (Google), else in the window's time zone. In a
  spring-forward gap a local time takes the offset before the gap; in a fall-back overlap it
  is the first of the two times (RFC 5545). An **all-day** event has `YYYY-MM-DD` dates and an
  **exclusive** `end`, as iCalendar has (a one-day event on the 23rd ends on the 24th).
- **Ids.** `id` is stable across runs: `intents.fingerprint("calendar", UID)` for a single
  event, `intents.fingerprint("calendar", UID, original start)` for an instance of a
  recurring one, the original start (its RECURRENCE-ID) written `20261026T083000Z` (UTC),
  `20261026T093000` (floating) or `20261026` (a date). A moved instance keeps its id, and so
  does each instance that a `RANGE=THISANDFUTURE` override moves (its own original start,
  not the override's). Ids are unique within a calendar. The same event in two calendars
  (an invitation in both) is returned once, from the first calendar listed. An event
  without a UID gets an id from a digest of its content, leaving out what a server rewrites
  on each export (`DTSTAMP`, `SEQUENCE`, `LAST-MODIFIED`, `CREATED`): it changes when the
  event does, and two UID-less events with exactly the same content are returned once.
- **Text is untrusted.** Anyone who can invite you can write a title. `title` (at most 300
  characters, `(untitled)` when empty) and `location` (300, or `null`) are one line of plain
  text; `description` (1000 characters and 50 lines, or `null`) keeps its line breaks as
  `\n`. Control and format characters (bidi overrides, zero-width spaces) are dropped,
  except the zero-width joiner and non-joiner some scripts and emoji need, and whitespace
  runs become one space. Nothing else changes: no escaping, no unescaping, no HTML or
  Markdown processing (`Pay &#x1F4C5; 2020-01-01` stays those characters). The caps count
  those characters. **Escaping is the consumer's job**, for its target: a task built from an
  event must take its title through `intents.task_create`, which escapes Tasks and Markdown
  syntax, and a note writer must escape what it writes. An event's text can also hold
  instructions aimed at an LLM: it is data, never a command.
- **`complete`, `skipped`, `truncated`, `warnings`.** A calendar is `complete` when every
  event of its feed in the window is returned. `skipped` counts the events or series that
  could not be read (no `DTSTART`, a broken rule or instance) or were refused (a rule
  repeating every second or minute, or one that cannot reach the window cheaply, see
  [Bounds](#bounds)); `truncated` counts the series cut short by a cap or by the time
  budget. Either makes the calendar, and the call, `complete: false`, without failing it.
  `warnings` counts events returned with a caveat (a time zone the feed names but does not
  define, read as floating); they leave `complete` as it is. The top-level `truncated`
  counts the events left out over the 2000 of a call (the earliest are kept), which also
  makes it `complete: false`. A brief built on `complete: false` data should say so.

### Recurrence

Parsing is [`icalendar`](https://github.com/collective/icalendar) (BSD-2-Clause) and the
expansion [`recurring-ical-events`](https://github.com/niccokunzmann/python-recurring-ical-events)
(LGPL-3.0-or-later, used as an unmodified library), which handle `RRULE` (`UNTIL` in UTC,
local or as a date, `COUNT`, `BYDAY` such as `2TU` or `-1FR`), `RDATE`, `EXDATE`,
`RECURRENCE-ID` overrides (moved, retitled or cancelled instances, `RANGE=THISANDFUTURE`),
`VTIMEZONE` definitions (including non-IANA names such as Outlook's
`W. Europe Standard Time`) and Google's `X-WR-TIMEZONE`. The plugin adds two readings:

- an `UNTIL` given as a date on a timed series (RFC 5545 wants a date-time there) includes
  that whole day, in the series' time zone;
- a `TZID` that is neither an IANA name nor defined in the feed reads the time as floating
  (in the window's time zone) and counts a `warning`.

The tests cover each case with a synthetic feed in `tests/fixtures/ics/`.

### Bounds

A feed is untrusted, so reading it is bounded:

- **events:** at most 500 per series and 2000 per calendar (the earliest are kept; the
  others are counted in `truncated`); cancelled instances and the margin day expanded on
  each side of the window do not count. At most 2000 events in all.
- **time:** each feed is expanded in a worker thread, off the event loop, within 5 s of
  wall-clock time; the series not reached by then are counted in `truncated`.
- **rules:** each series may take at most 20,000 steps of `dateutil`'s rule iterator to
  reach the window (about 55 years of a daily rule, 380 of a weekly one, 2 of an hourly
  one, from its `DTSTART`); an older series is skipped and counted in `skipped`. A rule that never matches (`FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30`) would
  otherwise step to the year 9999; it is skipped and counted in `skipped`. Rules repeating
  every second or minute are skipped too.
- **size:** 10 MiB per feed once decompressed, 50,000 events per feed (`too_many_events`).

### Failures

Each calendar is fetched and read on its own, concurrently. A calendar fails with one fixed
reason, never the URL, the feed's text or an upstream message:

| Reason | What happened |
|---|---|
| `invalid_url`, `not_https`, `port_not_allowed` | The URL is not an `https://` (or `webcal://`) URL on the default port without user info. |
| `host_not_allowed` | The URL's host is neither declared nor in `allowed_hosts`. |
| `auth_failed`, `rate_limited`, `bad_response`, `token_http_<status>` | Google only: see [Failures](#google-failures). |
| `http_401`, `xml_refused`, `not_multistatus` | CalDAV only: wrong credentials; an answer that is not well-formed XML or declares a DTD or entity; an answer that is not a CalDAV multistatus (a login page). |
| `timeout`, `connect_failed`, `tls_failed`, `transport_failed` | The service is down, slow or not who it claims to be. |
| `http_<status>` | Any answer other than 200 (`http_404`: the link was revoked). |
| `redirect_refused`, `too_many_redirects` | A redirect to another host, port or scheme, or more than 3. An ICS feed never follows a redirect to another host (iCloud *public* calendar links redirect to `pXX-caldav.icloud.com`: use [CalDAV](#caldav) for iCloud instead); a CalDAV request follows one only to an allowed host, see [CalDAV](#caldav). |
| `too_large`, `decode_failed` | Over 10 MiB once decompressed, or a broken compressed body. |
| `not_icalendar`, `parse_failed` | Not an iCalendar feed (a login page) or one that cannot be parsed. |
| `too_many_events` | More than 50,000 events in the feed. |
| `internal_error` | A bug; nothing else is reported. |

A configuration error fails the whole call before any fetch, without the secret's
content: `secret_missing: CALENDAR_ICS_URLS is not set or empty` (the secret is optional, so this also means it is absent), and
`config_invalid: ...` for a bad parameter, more than 16 URLs in the secret, or an `index`
past its last URL (`calendars[0].index is 3 but CALENDAR_ICS_URLS holds 2 URL(s)`).

When every calendar works the result is `complete: true` (unless something was skipped or
truncated, above). When one fails the call **raises**
(`calendar failed: Family: timeout`): in a `collect` step with other sources, the core marks
the step and the run `degraded`, keeps the other sources' data and skips every `act` step,
so nothing is written from partial data. With `partial: true`, the call instead returns the
working calendars' events with `complete: false` and the failure in `calendars`
(`{"name": "Family", "ok": false, "error": "timeout"}`): the core then sees a successful step,
so use it only where a brief with some events is better than none and the next steps check
`complete`. When every calendar fails, the call always raises.

Each fetch has a 5 s connect timeout, an 8 s read timeout, a 9 s budget per attempt and a
20 s total deadline, retries included: one retry, after a 0.5 s pause, after a timeout, a
connection error or a 500, 502, 503 or 504, when at least 1 s of the deadline is left; never
after a 4xx. Unexpected errors are logged to stderr by exception type only (`calendar 2:
unexpected KeyError`), never with a message; skip and truncation counts are logged at
`DEBUG`.
The core's per-call timeout (`plugin_defaults.call_timeout_s`, 30 s by default) bounds the
whole call; raise it with `timeout_s` on the pipeline call for many or large calendars.

## CalDAV

A calendar with `type: caldav` is read from a CalDAV **calendar collection** URL. The plugin
sends one `REPORT` (`calendar-query`) with a `time-range` of the window (plus a day on each side)
and Depth 1, with HTTP Basic credentials over HTTPS, then expands the returned iCalendar
objects with the same code as an ICS feed: recurrences (`RRULE`, `RDATE`, `EXDATE`, overrides),
time zones, ids, text cleaning, the bounds and the `complete`/`skipped`/`truncated`/`partial`
semantics are identical. The server's answer is untrusted: it is parsed with a hardened
XML parser that refuses any `DOCTYPE`, entity or processing instruction (so no entity expansion
and no external reference) and bounds the depth, the number of elements and resources, and the
size of one resource (2 MiB); a resource that cannot be read is counted in `skipped`.

### The credentials are a secret

One more declared secret, **`CALENDAR_CALDAV`**: for each CalDAV calendar, three
whitespace-separated words, `url user password`, all on one line (the core accepts no line
break but a final one). A calendar refers to its triple by its 1-based `index`.

```
# /run/secrets/CALENDAR_CALDAV, one line:
https://p12-caldav.icloud.com/1234567890/calendars/home/ me@icloud.com abcd-efgh-ijkl-mnop
```

Nothing is percent-decoded, so the user name and the password hold no whitespace (an iCloud
Apple ID and app-specific password never do). The password is sent only as HTTP Basic over
`https`, on the default port, to an allowed host, and is never logged, returned or put in an
error (errors name the secret and counts only; an `Account` prints as `Account(<hidden>)`).
At most 16 calendars.

**Every secret is optional.** `CALENDAR_CALDAV` is declared like `CALENDAR_ICS_URLS` and
`CALENDAR_GOOGLE` in `permissions.optional_secrets`: the host injects those that resolve, so an
instance gives only the secrets of the calendar types it uses. A call that configures a
calendar of a type whose secret is missing fails with `secret_missing`. This needs a core with
optional secrets (decision 56, unreleased).

### Hosts and redirects

`plugin.yaml` declares `caldav.icloud.com`. Any other CalDAV host (and every iCloud shard) goes
through [Other hosts](#other-hosts): declare it, approve it in `horae.lock` and list it in the
call's `allowed_hosts`. A credentialed CalDAV request follows a redirect only to the **same origin**, with one
exception: an account on `caldav.icloud.com` (or on a shard) may be redirected to a shard host
matching `pNN-caldav.icloud.com` (`p` and 1 to 3 digits) **that is also in `allowed_hosts`**,
over `https` on the default port (at most 3 hops; method, body and credentials kept). The
global allow-list never widens this: a redirect from another account's host to any other host,
even an allowed one, fails with `redirect_refused`.

### iCloud: manual check

iCloud answers at `https://caldav.icloud.com` and redirects each account to a shard host
`pNN-caldav.icloud.com`, where its collections live. The plugin takes the collection URL
directly; finding it takes a few `curl` calls (nothing here is stored by the plugin):

1. Turn on two-factor authentication for the Apple ID, then at
   [account.apple.com](https://account.apple.com) (Sign-In and Security, App-Specific Passwords)
   generate an **app-specific password** (`abcd-efgh-ijkl-mnop`). The Apple ID password does
   not work, and the app password can be revoked there at any time.
2. Find the principal (the answer holds `<current-user-principal>`, `/1234567890/principal/`):
   ```
   curl -s -u "$ICLOUD_USER:$ICLOUD_APP_PASSWORD" -X PROPFIND -H 'Depth: 0' \
     -H 'Content-Type: application/xml' --data \
     '<propfind xmlns="DAV:"><prop><current-user-principal/></prop></propfind>' \
     https://caldav.icloud.com/
   ```
3. Find the calendar home on the principal (`<calendar-home-set>`,
   `https://p12-caldav.icloud.com:443/1234567890/calendars/`; **`p12-caldav.icloud.com` is
   your shard host**):
   ```
   curl -s -u "$ICLOUD_USER:$ICLOUD_APP_PASSWORD" -X PROPFIND -H 'Depth: 0' -H 'Content-Type: application/xml' --data \
     '<propfind xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><prop><c:calendar-home-set/></prop></propfind>' \
     https://caldav.icloud.com/1234567890/principal/
   ```
4. List the calendars with `Depth: 1` on the home (use the shard host) and pick the
   `<displayname>` whose `<resourcetype>` holds `calendar`; its `href` is the collection,
   `https://p12-caldav.icloud.com/1234567890/calendars/home/` for the default calendar.
5. Put `url user password` in `CALENDAR_CALDAV`, add the shard host to `permissions.network` in
   `plugin.yaml` (and regenerate it), approve it in `horae.lock`, and pass it in the call:
   ```yaml
   - calendar.events:
       calendars: [{ name: iCloud, index: 1, type: caldav }]
       allowed_hosts: [p12-caldav.icloud.com]
       days: 7
   ```
6. Expect `complete: true` and your events, with recurring ones expanded and the right
   times. `calendar failed: iCloud: http_401` is a wrong Apple ID or app password;
   `host_not_allowed` / `redirect_refused` is the shard missing from `allowed_hosts`;
   `http_403`/`http_404` a wrong collection URL. Shared and subscribed calendars live in
   other collections of the same home.

The CI proves the protocol against a real CalDAV server (Radicale, below) but cannot reach
iCloud: this check against a real account is the part to do by hand.

## Google Calendar API

A calendar with `type: google` is read with the Google Calendar API v3 (`events.list`), with
`singleEvents=true` (Google expands the recurring events), the window as `timeMin`/`timeMax`,
`orderBy=startTime`, 250 events per page and at most 10 pages. Events become the same
`Event`s as for ICS and CalDAV (all-day or timed, in the window's time zone; cancelled events
left out; `tentative` kept; ids from `iCalUID` plus the original start of an instance, so a
moved instance keeps its id and an invitation in an ICS and a Google calendar is returned
once). At most 2000 events per calendar; a calendar cut by that cap, by the 10 pages or by
the 30 s budget is `complete: false` with `truncated: 1`. Titles, locations and descriptions
are cleaned as plain text, exactly as for ICS.

```yaml
- calendar.events:
    calendars:
      - { name: Perso, type: google }                                   # primary calendar
      - { name: Team, type: google, calendar_id: team@group.calendar.google.com }
      - { name: Other, type: google, index: 2 }                         # a second Google account
```

### Set up the OAuth app (once)

The plugin reads your own calendar with your own OAuth client; nothing is shared with anyone.

1. In the [Google Cloud console](https://console.cloud.google.com/) create a project (or
   pick one) and enable the **Google Calendar API** (APIs and services, Library).
2. **OAuth consent screen** (Google Auth platform, Branding and Audience): user type
   **External** for a personal Gmail account (**Internal** only if you have a Google
   Workspace and are in its organisation). Fill the app name and your e-mail.
3. **Scopes** (Data access): add the read-only
   `https://www.googleapis.com/auth/calendar.events.readonly`. (`calendar.readonly` also
   works and also lets the plugin read the calendars' list, which it does not need.)
   Nothing here can write to a calendar.
4. **Publish the app: set the publishing status to "In production".** This is the step
   that matters. An app left in **"Testing"** issues refresh tokens that **expire after 7
   days**, so the pipeline would break every week with `auth_failed`. Publishing a personal
   app that uses a sensitive scope shows an "unverified app" warning at consent, which you
   accept for your own account (Advanced, Go to the app); Google's verification process is
   for apps used by other people and is not needed for personal use.
   If you add yourself as a test user instead of publishing, you must repeat step 6 every
   week.
5. **Credentials, Create credentials, OAuth client ID**, application type **Desktop app**
   (the installed-app flow with a loopback redirect: no redirect URI to register). Note the
   client id and the client secret (for a Desktop app the secret is not confidential in the
   OAuth sense, but the plugin still treats it as a secret).
6. Get the refresh token with the helper below.

### Get the refresh token

Run this **on your machine, once**; it is not part of the plugin server and is not run by
Horae. It needs the plugin's package (`uv run` in a clone, or the interpreter you pin in
`plugin.yaml`):

```
umask 077
GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python -m horae_calendar google-auth > CALENDAR_GOOGLE
```

(without the variables it asks for the client id and secret, the secret without echo;
`--no-browser` only prints the consent URL instead of opening a browser). It starts a server
on `127.0.0.1` on a free port, opens Google's consent page (`accounts.google.com`, which only
this helper talks to), receives the answer on that port with a one-time `state` and a PKCE
challenge, exchanges the code, and prints **one line on stdout**, `client_id client_secret
refresh_token`: the value of the secret. Everything else (the consent URL, errors) goes to
stderr, and nothing is stored by the command. If Google returns no refresh token (the app
had already been authorised), remove its access at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) and run it again.
Move the file to the secrets directory (`/run/secrets/CALENDAR_GOOGLE`, mode 600) and delete
your copy.

### The credentials are a secret

One more declared secret, **`CALENDAR_GOOGLE`**: for each Google account, three
whitespace-separated words, `client_id client_secret refresh_token`, on one line (at most 16
accounts; a calendar's `index` is the account's place). Like `CALENDAR_CALDAV`, it is an optional secret: an instance that does not use Google
simply leaves it out.

At each call the plugin exchanges the refresh token at `oauth2.googleapis.com/token` for an
**access token (about an hour)**, used for every calendar of that account in the call and
then dropped: it is kept in memory only, never written, logged, returned or put in an
error, and the refresh token is sent only to that endpoint (over `https`, same-origin
redirects only, like the bearer token to `www.googleapis.com`). If Google answers 401 to an
access token the plugin exchanges once more and retries once. There is no state between
calls, so there is nothing to refresh in the background.

**Rotation.** Google keeps the refresh token valid until it is revoked, so there is no
periodic rotation. To rotate or revoke: remove the app at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions), run
`google-auth` again and replace the secret. Also reset the client secret in the Cloud
console if it may have leaked. The refresh token stops working (`auth_failed`) if the user
revokes it, leaves it unused for six months, hits Google's per-account limit on refresh
tokens (the oldest are dropped), or the app is in "Testing" for more than 7 days.

### Hosts

`plugin.yaml` declares `oauth2.googleapis.com` (the token exchange) and `www.googleapis.com`
(the Calendar API). The plugin sends the tokens to these two hosts only; nothing goes through
`allowed_hosts`.

### Google failures

<a id="google-failures"></a>

| Reason | What happened |
|---|---|
| `auth_failed` | Google refused the refresh token (`invalid_grant`: revoked, expired, or the app is in **Testing** and the token is older than 7 days; or `invalid_client`: wrong client id or secret), or still answered 401 after a fresh token. Run `google-auth` again, after publishing the app ("In production"). |
| `rate_limited` | HTTP 429, or 403 with `rateLimitExceeded`, `userRateLimitExceeded` or `quotaExceeded`. Not retried; the next run tries again. |
| `http_403` | The API is not enabled in the project, or the scope does not allow this calendar. `http_404`: unknown `calendar_id`. |
| `bad_response`, `token_http_<status>` | An answer that is not what Google documents, or another refusal by the token endpoint. |
| `timeout`, `connect_failed`, `tls_failed`, `transport_failed`, `http_5xx` | Google is down or slow (retried once). |

Google's own message is never shown: it can quote what was sent.

To check against a real account (CI cannot): create the OAuth client and the token as above,
put the secret in place, and run `calendar.events: { calendars: [{ name: Perso, type: google }],
days: 7 }`. Expect `complete: true`, your events with recurring ones expanded, and the
right times; `auth_failed` right after set-up points to the consent screen status or to a
token from a previous client.

## Configure and install

### The feed URLs are a secret

A Proton share link (`https://calendar.proton.me/api/calendar/v1/url/…/calendar.ics?…`) or a
Google secret address (`https://calendar.google.com/calendar/ical/…/private-…/basic.ics`)
grants read access to anyone holding it. The plugin handles it as a password: it reads it
from its secret at call time, sends it only to an allowed host over HTTPS (no proxy from
the environment, `trust_env=False`), and never logs it, returns it or puts it in an error;
the `httpx` loggers are held at `WARNING` because they log request URLs at `INFO`. The core
also redacts every secret it resolved from the plugin's output, as a last line of defence.
Never put a URL in a pipeline parameter: put it in the secret.

All the feeds go in **one** declared secret, `CALENDAR_ICS_URLS`: one or more URLs separated
by spaces (the core accepts 8 B to 64 KiB with no line break but a final one, so keep them
on one line). A calendar refers to its URL by its 1-based `index`; without `calendars`,
every URL is read.

```yaml
# horae.yaml
secrets:
  dir: /run/secrets
# /run/secrets/CALENDAR_ICS_URLS, one line:
# https://calendar.proton.me/api/calendar/v1/url/…/calendar.ics?… https://calendar.google.com/calendar/ical/…/basic.ics
```

`webcal://` links (what calendar apps show) are read as `https://`. Plain `http://` is
refused. Adding a calendar is adding a URL to the secret: no new permission to approve.

### Other hosts

`plugin.yaml` declares `calendar.google.com` and `calendar.proton.me`. For a feed on another
host (Fastmail, Nextcloud, a school's calendar):

1. add the host to `permissions.network` and approve it in `horae.lock` (from M5, the
   core's egress proxy allows only the hosts locked there, and refuses hosts that resolve
   to private addresses);
2. list the same host in the call's `allowed_hosts` (a lowercase DNS name: no scheme, port,
   IP address or `localhost`). Since horae-sdk 0.5 the core also sends the hosts approved in
   `horae.lock` with every call, and a calendar whose host is not among them (ICS, CalDAV
   or Google, and a credentialed CalDAV redirect to an iCloud shard) is skipped with
   `host_not_approved: <host>` before it is contacted. With an older core that sends no
   list, `allowed_hosts` must match what you approved there; the core enforces the lock
   itself from M5.

The plugin checks the URL's host against the declared hosts and `allowed_hosts` before
connecting, and follows a redirect only to the same scheme, host and port (at most 3), so
the token-bearing request never reaches another host.

**Open question (M5).** The client ignores proxy variables from the environment
(`trust_env=False`), so a proxy variable cannot see or divert the URL. If the M5 egress
proxy is reached through `HTTPS_PROXY` (the core refuses secrets named `*_PROXY`, so it
would be the host's own variable) rather than transparently, the plugin will need to accept
that one proxy explicitly.

### Install

The host starts `python3 -m horae_calendar` (`runtime.command`). Until Horae installs plugins
into their own environments (M4), pin the command to an **absolute** interpreter that has this
package, as the [SDK README](https://github.com/horae-dev/horae/blob/main/sdk/README.md#manifest)
explains. It needs network access to the feed hosts and nothing else: no path, no intent.

## `calendar.prep_tasks`

A transform: the `calendar.events` data of a collect step in, `task.create` proposals out, with
no LLM and no clock (`today` is the pipeline's `{{date}}`). Rules decide which events need
preparation; each matching event occurrence gets one task, due `lead_days` before it.

```yaml
- transform: calendar.prep_tasks
  params:
    today: "{{date}}"
    max_tasks: 50          # default 50, at most 200
    rules:                 # the first matching rule decides
      - keywords: [review, trip]       # in the title, ignoring case
        regex: '^board\b'              # or a pattern (case-insensitive)
        calendars: [Work]              # optional: only these calendars
        lead_days: 2                   # default 1: the day before
        title: "Prepare: ${title}"     # default; ${title} is the event's title
```

- One task per occurrence: fingerprint `calendar:prep:<event id>`, so each instance of a
  recurring event has its own task and a later run proposes the same fingerprint.
- Due date: the event's date in its own time zone minus `lead_days` calendar days (daylight
  saving and month ends do not move it); an ended event gets no task and a due date in the past
  becomes `today`. Cancelled events are not in `calendar.events`.
- Titles are untrusted: `intents.task_create` escapes the whole title.
- Bounds: up to 20 rules, 20 keywords per rule, a 200-character regex (a repeated group that
  repeats is refused), 200 tasks; `data` counts `events`, `matched`, `past`, `skipped`,
  `tasks` and `left_out`.

## Permissions

| Kind | Value | Why |
|---|---|---|
| network | `calendar.google.com` | Google Calendar secret addresses (ICS) |
| network | `calendar.proton.me` | Proton Calendar share links (ICS) |
| network | `www.googleapis.com` | Google Calendar API (`events.list`, read-only scope) |
| network | `oauth2.googleapis.com` | Google's token endpoint (refresh token to access token) |
| network | `caldav.icloud.com` | iCloud CalDAV (a shard `pNN-caldav.icloud.com` is added per instance) |
| secret | `CALENDAR_ICS_URLS` | The feed URLs, separated by spaces (each holds an access token) |
| secret | `CALENDAR_CALDAV` | CalDAV `url user password` triples, separated by whitespace (the credentials) |
| secret | `CALENDAR_GOOGLE` | Google `client_id client_secret refresh_token` triples, separated by whitespace (the OAuth credentials) |
| paths | none | |
| intents | `task.create` | `calendar.prep_tasks` proposes preparation tasks (a task sink applies them) |

## Development

```
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov      # 85% gate; the Radicale tests skip unless CALDAV_TEST_URL is set
uv run python -m horae_calendar --manifest > plugin.yaml
```

- `src/horae_calendar/caldav.py`: the `calendar-query`, the hardened multistatus parser and the
  merge of resources into one feed for `ics.expand`.
- `src/horae_calendar/google.py`: the token exchange, `events.list` and the mapping;
  `google_auth.py`: the one-time consent helper (`python -m horae_calendar google-auth`).
- `src/horae_calendar/events.py`: the event model (`Event`, `Window`, ids, text cleaning),
  kept apart from ICS so the CalDAV (#3) and Google (#4) sources and the preparation-tasks
  transform (#5) reuse it.
- `src/horae_calendar/ics.py`: parsing and expansion; `fetch.py`: the bounded HTTPS client
  and the URL checks; `plugin.py`: the `calendar.events` tool.
- **CalDAV integration test**: CI's `CalDAV (Radicale)` job starts Radicale (image pinned by
  digest, htpasswd user with a synthetic password, loopback only) and runs
  `tests/test_integration_caldav.py`, which seeds a collection with a recurring series (an
  `EXDATE`, a moved instance), an all-day event and an old one, and checks the tool returns
  them expanded. Locally: `docker run -d -p 127.0.0.1:5232:5232 -v $PWD/users:/users:ro
  ghcr.io/kozea/radicale:3.5.7 --hosts 0.0.0.0:5232 --auth-type htpasswd --auth-htpasswd-filename
  /users --auth-htpasswd-encryption plain` with `alice:synthetic-test-pass` in `users`, then
  `CALDAV_TEST_URL=http://127.0.0.1:5232 uv run pytest tests/test_integration_caldav.py`.
- **Google tests** run a fake token endpoint and `events.list` server on `127.0.0.1`
  (refresh, `invalid_grant`, 401 then refresh, pagination, 429 and `rateLimitExceeded`, tokens
  never in output, errors or logs).
- Other tests never touch the network: a fake fetcher serves the fixtures to the tool, and the
  HTTP client is tested against HTTP and HTTPS servers on `127.0.0.1` (a `trustme` CA).
  `tests/recordings/` replays the tool through MCP; every fixture is synthetic, with
  `example.org` names, and the fake token must never appear in any output, log or error.

Runtime dependencies and their licences, including the two LGPL-3.0-or-later ones and what
a distributor must ship with them, are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

`horae-sdk` comes from PyPI, pinned with hashes by `uv.lock`; Renovate waits 7 days before
proposing a new release (`minimumReleaseAge`).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
