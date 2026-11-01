<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-calendar

Horae plugin: calendars from ICS URLs, CalDAV and the Google Calendar API, with preparation tasks.

> 🚧 **In progress.** The ICS source (`calendar.events`) is in. CalDAV (#3), the Google Calendar API (#4) and preparation tasks (#5) are not yet. Follow progress on the [Horae roadmap project](https://github.com/orgs/horae-dev/projects/1).

It declares every host, secret, path and intent it uses in `plugin.yaml`, and it only returns
data: the `calendar.events` source proposes no intent.

## `calendar.events`

The events of a window of whole days, read from each calendar's ICS feed (a Proton Calendar
share link, a Google Calendar "secret address in iCal format", or any other ICS URL on a host
you allow), with recurring events expanded.

```yaml
# pipelines/daily-brief.yaml
steps:
  - collect:
      - calendar.events:
          calendars:
            - { name: Personal, index: 1 }   # the first URL of CALENDAR_ICS_URLS
            - { name: Family, index: 2 }     # the second
          start: "{{date}}"         # the run's date
          timezone: "{{timezone}}"  # the instance's (or the trigger's) time zone
          days: 2
```

Every parameter is optional: `calendar.events: { days: 2 }` reads every URL of the secret,
named `calendar-1`, `calendar-2`..., over today and tomorrow in the instance's time zone.

| Parameter | Default | Meaning |
|---|---|---|
| `calendars` | every URL of the secret | 1 to 16 calendars, each `{name, index}`: a display name (1 to 64 printable characters, unique) and the 1-based place of its feed URL in the `CALENDAR_ICS_URLS` secret (unique). An optional `type: ics` is the only type for now. Without it, every URL is read, named `calendar-<index>`. |
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
- **rules:** each series may take at most 100,000 steps of `dateutil`'s rule iterator to
  reach the window (about 270 years of a daily rule, 11 years of an hourly one, from its
  `DTSTART`). A rule that never matches (`FREQ=DAILY;BYMONTH=2;BYMONTHDAY=30`) would
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
| `timeout`, `connect_failed`, `tls_failed`, `transport_failed` | The service is down, slow or not who it claims to be. |
| `http_<status>` | Any answer other than 200 (`http_404`: the link was revoked). |
| `redirect_refused`, `too_many_redirects` | A redirect to another host, port or scheme, or more than 3. iCloud public calendar links redirect to another host (`pXX-caldav.icloud.com`) and are **not supported**. |
| `too_large`, `decode_failed` | Over 10 MiB once decompressed, or a broken compressed body. |
| `not_icalendar`, `parse_failed` | Not an iCalendar feed (a login page) or one that cannot be parsed. |
| `too_many_events` | More than 50,000 events in the feed. |
| `internal_error` | A bug; nothing else is reported. |

A configuration error fails the whole call before any fetch, without the secret's
content: `secret_missing: CALENDAR_ICS_URLS is not set or empty` (the host refuses to start
the plugin when a declared secret cannot be resolved, so this means an empty value), and
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
   IP address or `localhost`). The plugin cannot read `horae.lock`, so this list must match
   what you approved there; the core enforces the lock from M5.

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

## Permissions

| Kind | Value | Why |
|---|---|---|
| network | `calendar.google.com` | Google Calendar secret addresses (ICS) |
| network | `calendar.proton.me` | Proton Calendar share links (ICS) |
| secret | `CALENDAR_ICS_URLS` | The feed URLs, separated by spaces (each holds an access token) |
| paths | none | |
| intents | none | A pure source: it returns events, it proposes nothing |

## Development

```
uv sync
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest --cov      # 85% gate
uv run python -m horae_calendar --manifest > plugin.yaml
```

- `src/horae_calendar/events.py`: the event model (`Event`, `Window`, ids, text cleaning),
  kept apart from ICS so the CalDAV (#3) and Google (#4) sources and the preparation-tasks
  transform (#5) reuse it.
- `src/horae_calendar/ics.py`: parsing and expansion; `fetch.py`: the bounded HTTPS client
  and the URL checks; `plugin.py`: the `calendar.events` tool.
- Tests never touch the network: a fake fetcher serves the fixtures to the tool, and the
  HTTP client is tested against HTTP and HTTPS servers on `127.0.0.1` (a `trustme` CA).
  `tests/recordings/` replays the tool through MCP; every fixture is synthetic, with
  `example.org` names, and the fake token must never appear in any output, log or error.

Runtime dependencies and their licences, including the two LGPL-3.0-or-later ones and what
a distributor must ship with them, are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

`horae-sdk` comes from the core repository's `main` branch (`[tool.uv.sources]`), pinned to a
commit by `uv.lock`; dependencies resolve with a 7-day minimum release age
(`exclude-newer`).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
