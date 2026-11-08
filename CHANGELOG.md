# Changelog

Every change to tools, parameters and permissions. A new permission is called out: users
must approve it.

## Unreleased

- **Google Calendar API source** (#4): `calendars` entries with `type: google` (and an optional
  `calendar_id`, default `primary`) read `events.list` (`singleEvents`, window, paginated),
  authenticating with an OAuth refresh token exchanged for a short-lived access token on each
  call (in memory only). Failures are `auth_failed` (`invalid_grant`, 401 after a refresh),
  `rate_limited` and fixed codes. `python -m horae_calendar google-auth` runs the one-time
  consent locally and prints the secret. **New permissions:** network `www.googleapis.com` and
  `oauth2.googleapis.com`; secret `CALENDAR_GOOGLE` (`client_id client_secret refresh_token`;
  give it a placeholder when unused, secrets cannot be optional yet). Scope requested by the
  helper: `calendar.events.readonly`.
- Repository scaffolded from `template-plugin`.
- **New tool `calendar.events`** (#2): the events of a window of whole days from ICS feeds
  (Proton share links, Google secret addresses), recurrences expanded. Parameters
  `calendars` (`{name, index}`, default: every URL), `start`, `days`, `timezone` (default:
  the instance's), `allowed_hosts`, `partial`. Output with per-calendar `complete`,
  `skipped`, `truncated` and `warnings`; bounded per series, per feed and in time.
- **New permissions:** network `calendar.google.com` and `calendar.proton.me` (the feed
  hosts); secret `CALENDAR_ICS_URLS` (every feed URL, separated by spaces). No paths, no
  intents.
- **New transform `calendar.prep_tasks`** (#5): `task.create` proposals for events that match
  a rule (keywords or regex on the title, calendars, `lead_days`, a title template with
  `${title}`); one task per event occurrence, fingerprint `calendar:prep:<event id>`.
  Parameters `today`, `rules`, `max_tasks`.
- **New permission:** intent `task.create` (no new network, secret or path).
- Runtime dependencies include two LGPL-3.0-or-later packages, `recurring-ical-events` and
  `x-wr-timezone`: see THIRD-PARTY-NOTICES.md.
