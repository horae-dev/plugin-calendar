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
            - { name: Personal, secret: CALENDAR_ICS_URL }
            - { name: Family, secret: CALENDAR_ICS_URL_FAMILY }
          start: "{{date}}"         # the run's date
          timezone: "{{timezone}}"  # the instance's (or the trigger's) time zone
          days: 2
```

| Parameter | Default | Meaning |
|---|---|---|
| `calendars` | (required) | 1 to 16 calendars, each `{name, secret}`: a display name (1 to 64 printable characters, unique) and the name of the secret holding its feed URL, `CALENDAR_ICS_URL` or `CALENDAR_ICS_URL_<SUFFIX>`. An optional `type: ics` is the only type for now. |
| `start` | today in `timezone` | The first day, `YYYY-MM-DD`. Pass `"{{date}}"` so a replayed run reads the same window. |
| `days` | `2` | Whole days, 1 to 62. |
| `timezone` | `UTC` | IANA time zone of the window and of the times returned. Pass `"{{timezone}}"`. |
| `extra_hosts` | none | Feed hosts allowed besides the declared ones (see [Other hosts](#other-hosts)). |
| `partial` | `false` | When a calendar fails, return the others' events (`complete: false`) instead of raising. See [Failures](#failures). |

### Output

```json
{
  "window": {"start": "2026-10-26T00:00:00+01:00", "end": "2026-10-28T00:00:00+01:00",
             "timezone": "Europe/Paris", "days": 2},
  "complete": true,
  "calendars": [{"name": "Personal", "ok": true, "events": 1, "skipped": 0}],
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
  event, `intents.fingerprint("calendar", UID, RECURRENCE-ID)` for an instance of a recurring
  one, the RECURRENCE-ID (the instance's original start) written `20261026T083000Z` (UTC),
  `20261026T093000` (floating) or `20261026` (a date). A moved instance keeps its id. The same
  event in two calendars (an invitation in both) is returned once, from the first calendar
  listed. An event without a UID gets an id from a digest of its content, which changes when
  the event does.
- **Text is untrusted.** Anyone who can invite you can write a title. `title` (at most 300
  characters, `(untitled)` when empty), `location` (300) and `description` (1000, or `null`)
  are plain, single-line text: line breaks, control and format characters (bidi, zero-width)
  removed with the SDK's `escape_title` / `plain_text`. Markdown and Obsidian Tasks syntax
  are **not** escaped, since that depends on where the text goes: a task built from an event
  must take its title through `intents.task_create`, which escapes it, and a note writer must
  escape what it writes. An event's text can also hold instructions aimed at an LLM: it is
  data, never a command.
- **`skipped`** counts the events or instances of a calendar that could not be read (no
  `DTSTART`, a broken rule) or were refused (a rule repeating every second or minute, whose
  expansion would take too long). They never fail the calendar.

### Recurrence

Parsing is [`icalendar`](https://github.com/collective/icalendar) (BSD-2-Clause) and the
expansion [`recurring-ical-events`](https://github.com/niccokunzmann/python-recurring-ical-events)
(LGPL-3.0-or-later, used as an unmodified library), which handle `RRULE` (`UNTIL` in UTC,
local or as a date, `COUNT`, `BYDAY` such as `2TU` or `-1FR`), `RDATE`, `EXDATE`,
`RECURRENCE-ID` overrides (moved, retitled or cancelled instances, `RANGE=THISANDFUTURE`),
`VTIMEZONE` definitions (including non-IANA names such as Outlook's
`W. Europe Standard Time`) and Google's `X-WR-TIMEZONE`. The tests cover each case with a
synthetic feed in `tests/fixtures/ics/`.

### Failures

Each calendar is fetched and read on its own, concurrently. A calendar fails with one fixed
reason, never the URL, the feed's text or an upstream message:

| Reason | What happened |
|---|---|
| `secret_missing` | The secret is not in the plugin's environment (not declared in the installed `plugin.yaml`, or empty). |
| `invalid_url`, `not_https`, `port_not_allowed` | The URL is not an `https://` (or `webcal://`) URL on the default port without user info. |
| `host_not_allowed` | The URL's host is neither declared nor in `extra_hosts`. |
| `timeout`, `connect_failed`, `tls_failed`, `transport_failed` | The service is down, slow or not who it claims to be. |
| `http_<status>` | Any answer other than 200 (`http_404`: the link was revoked). |
| `redirect_refused`, `too_many_redirects` | A redirect to another host, port or scheme, or more than 3. |
| `too_large`, `decode_failed` | Over 10 MiB once decompressed, or a broken compressed body. |
| `not_icalendar`, `parse_failed` | Not an iCalendar feed (a login page) or one that cannot be parsed. |
| `too_many_events` | More than 2000 instances in the window from one feed. |
| `internal_error` | A bug; nothing else is reported. |

When every calendar works the result is `complete: true`. When one fails the call **raises**
(`calendar failed: Family: timeout`): in a `collect` step with other sources, the core marks
the step and the run `degraded`, keeps the other sources' data and skips every `act` step,
so nothing is written from partial data. With `partial: true`, the call instead returns the
working calendars' events with `complete: false` and the failure in `calendars`
(`{"name": "Family", "ok": false, "error": "timeout"}`): the core then sees a successful step,
so use it only where a brief with some events is better than none and the next steps check
`complete`. When every calendar fails, the call always raises. More than 1000 events in all
also raises: ask for fewer days.

Each fetch has a 5 s connect timeout and a 20 s total deadline, retries included (one retry,
at once, after a timeout, a connection error or a 500, 502, 503 or 504; never after a 4xx).
The core's per-call timeout (`plugin_defaults.call_timeout_s`, 30 s by default) bounds the
whole call; raise it with `timeout_s` on the pipeline call for many or large calendars.

## Configure and install

### The feed URL is a secret

A Proton share link (`https://calendar.proton.me/api/calendar/v1/url/…/calendar.ics?…`) or a
Google secret address (`https://calendar.google.com/calendar/ical/…/private-…/basic.ics`)
grants read access to anyone holding it. The plugin handles it as a password: it reads it
from its secret at call time, sends it only to the declared host over HTTPS (no proxy from
the environment, `trust_env=False`), and never logs it, returns it or puts it in an error;
the `httpx` loggers are held at `WARNING` because they log request URLs at `INFO`. The core
also redacts every secret it resolved from the plugin's output, as a last line of defence.
Never put the URL in a pipeline parameter: put it in a secret file.

```yaml
# horae.yaml
secrets:
  dir: /run/secrets     # /run/secrets/CALENDAR_ICS_URL holds the URL, one line
```

`webcal://` links (what calendar apps show) are read as `https://`. Plain `http://` is
refused.

### More calendars

`plugin.yaml` declares one secret, `CALENDAR_ICS_URL`, and the host injects only declared
secrets (every declared secret must resolve, or the plugin does not start). For each further
calendar, add a `CALENDAR_ICS_URL_<SUFFIX>` (A–Z, 0–9) to `permissions.secrets` of the
**installed** `plugin.yaml`, approve it in `horae.lock`, and give it a secret file. The
plugin reads only secrets named this way, so a pipeline cannot make it read another one.

### Other hosts

`plugin.yaml` declares `calendar.google.com` and `calendar.proton.me`. For a feed on another
host (Fastmail, Nextcloud, a school's calendar):

1. add the host to `permissions.network` of the installed `plugin.yaml` and approve it in
   `horae.lock` (from M5, the egress proxy blocks undeclared hosts, and hosts that resolve to
   private addresses);
2. list it in the call's `extra_hosts` (a lowercase DNS name: no scheme, port, IP address or
   `localhost`).

The plugin checks the URL's host against both lists before connecting, and follows a
redirect only to the same scheme, host and port (at most 3), so the token-bearing request
never reaches another host.

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
| secret | `CALENDAR_ICS_URL` | The first calendar's feed URL (it holds the access token) |
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

`horae-sdk` comes from the core repository's `main` branch (`[tool.uv.sources]`), pinned to a
commit by `uv.lock`; dependencies resolve with a 7-day minimum release age
(`exclude-newer`).

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)
