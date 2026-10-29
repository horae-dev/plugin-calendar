# Changelog

Every change to tools, parameters and permissions. A new permission is called out: users
must approve it.

## Unreleased

- Repository scaffolded from `template-plugin`.
- **New tool `calendar.events`** (#2): the events of a window of whole days from ICS feeds
  (Proton share links, Google secret addresses), recurrences expanded. Parameters
  `calendars`, `start`, `days`, `timezone`, `extra_hosts`, `partial`.
- **New permissions:** network `calendar.google.com` and `calendar.proton.me` (the feed
  hosts); secret `CALENDAR_ICS_URL` (the first calendar's feed URL). No paths, no intents.
