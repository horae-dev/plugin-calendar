# Third-party notices

`horae-plugin-calendar` is Apache-2.0. It does not vendor or modify any third-party code: the
packages below are installed beside it, as `uv.lock` pins them, and imported at run time.
This file lists the **runtime** dependencies (`uv export --no-dev`) and their licences, as
declared in each package's metadata. Development tools (pytest, ruff, mypy, hypothesis,
trustme...) are not shipped and are not listed.

Regenerate the list when `uv.lock` changes: `uv tree --no-dev`, then each package's
`License-Expression` (or `License` / classifiers) in its installed metadata.

## LGPL-3.0-or-later dependencies

| Package | Version | Why | Source |
|---|---|---|---|
| `recurring-ical-events` | 3.8.2 | Recurrence expansion (RRULE, RDATE, EXDATE, RECURRENCE-ID) | <https://github.com/niccokunzmann/python-recurring-ical-events> |
| `x-wr-timezone` | 2.0.1 | Google's `X-WR-TIMEZONE` (pulled in by `recurring-ical-events`) | <https://github.com/niccokunzmann/x-wr-timezone> |

The plugin uses both **unmodified**, as separately installed Python packages that it imports
(a "Combined Work" that uses the "Library" through its interface, LGPL-3.0 section 4). That
is compatible with distributing the plugin's own code under Apache-2.0. Whoever
**distributes** these packages together with the plugin (a container image, a virtual
environment, a wheelhouse, an installer) must, for each of the two:

1. ship the LGPL-3.0 text and the GPL-3.0 text it builds on (both are in each package's
   distribution metadata, `*.dist-info/licenses/`, keep them), and say that the package is
   used and is under the LGPL;
2. ship the package's source, or a written offer or a link to the exact source of the version
   shipped (the PyPI sdist of that version is enough);
3. let the user replace the package with a modified version: an ordinary installed Python
   package already allows that (`pip install` another version into the same environment);
   do not merge it into a single binary, obfuscate it, or block the replacement.

Installing from PyPI with `uv sync` or `pip` (the user fetches the packages) needs nothing
more. The maintainer has to accept this LGPL runtime dependency (flagged in PR #6); if it is
refused, the expansion has to be rewritten on `python-dateutil` alone.

## Other runtime dependencies

| Package | Version | Licence |
|---|---|---|
| `annotated-types` | 0.8.0 | MIT |
| `anyio` | 4.15.1 | MIT |
| `attrs` | 26.1.0 | MIT |
| `certifi` | 2026.10.19 | MPL-2.0 |
| `cffi` | 2.1.1 | MIT-0 |
| `click` | 8.5.0 | BSD-3-Clause |
| `cryptography` | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| `h11` | 0.16.0 | MIT |
| `horae-sdk` | 0.1.0 (git) | Apache-2.0 |
| `httpcore` | 1.0.9 | BSD-3-Clause |
| `httpcore2` | 2.16.0 | BSD-3-Clause |
| `httpx` | 0.28.1 | BSD-3-Clause |
| `httpx2` | 2.16.0 | BSD-3-Clause |
| `icalendar` | 7.3.0 | BSD-2-Clause |
| `idna` | 3.20 | BSD-3-Clause |
| `jsonschema` | 4.26.0 | MIT |
| `jsonschema-specifications` | 2025.9.1 | MIT |
| `mcp` | 2.3.0 | MIT |
| `mcp-types` | 2.3.0 | MIT |
| `opentelemetry-api` | 1.45.1 | Apache-2.0 |
| `pycparser` | 3.12 | BSD-3-Clause |
| `pydantic` | 2.14.1 | MIT |
| `pydantic-core` | 2.50.1 | MIT |
| `pyjwt` | 2.15.1 | MIT |
| `python-dateutil` | 2.9.0.post0 | Apache-2.0 AND BSD-3-Clause (dual) |
| `python-multipart` | 0.0.32 | Apache-2.0 |
| `pyyaml` | 6.0.3 | MIT |
| `referencing` | 0.37.0 | MIT |
| `rpds-py` | 2026.9.1 | MIT |
| `six` | 1.17.0 | MIT |
| `sse-starlette` | 3.5.0 | BSD-3-Clause |
| `starlette` | 1.9.0 | BSD-3-Clause |
| `truststore` | 0.10.5 | MIT |
| `typing-extensions` | 4.16.0 | PSF-2.0 |
| `typing-inspection` | 0.4.4 | MIT |
| `tzdata` | 2026.5 | Apache-2.0 |
| `uvicorn` | 0.54.0 | BSD-3-Clause |

Platform-only, not installed on Linux or macOS: `pywin32` 312 (Windows, PSF-2.0) and
`httpx2-jsfetch` 1.0 (Emscripten), both through `mcp`.

`certifi` (MPL-2.0) is also used unmodified; MPL-2.0 asks a distributor to make its source
available, which the PyPI sdist does.
