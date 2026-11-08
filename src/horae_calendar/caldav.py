# SPDX-License-Identifier: Apache-2.0
"""CalDAV: the events of a window from a calendar collection, through the ICS path.

One `REPORT` with a `calendar-query` and a `time-range` (RFC 4791, 7.8) on the collection URL
asks the server for the resources (iCalendar objects) that meet the window. The answer, a
207 multistatus, is read with a **hardened XML parser**, the `calendar-data` of each
resource is parsed as iCalendar, and the resources are merged into one calendar that
`horae_calendar.ics.expand` expands, so recurrences, overrides, time zones and the bounds
behave exactly as for an ICS feed. The server filters coarsely (it may return a series that
only starts to repeat in the window); `expand` is the judge.

**The credentials are a secret** (`CALENDAR_CALDAV`, see `Account`): they are sent only as
HTTP Basic over `https` to an allowed host, never logged, returned or put in an error.

**The XML is untrusted.** `parse_multistatus` uses `xml.parsers.expat` directly and refuses
any document type declaration, so no entity (internal or external) is ever declared or
expanded and no DTD is loaded; it also bounds the depth, the number of elements and of
resources, and the size of one `calendar-data`.
"""

import datetime as dt
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, NoReturn
from xml.parsers import expat

import icalendar

from horae_calendar.events import Window
from horae_calendar.ics import IcsError, parse

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
MAX_DEPTH = 24
MAX_ELEMENTS = 200_000
MAX_RESOURCES = 20_000
MAX_DATA = 2 * 1024 * 1024
"""Characters of one resource's `calendar-data`."""
_MARGIN = dt.timedelta(days=1)
_UTC = dt.UTC


class CalDavError(Exception):
    """An answer that cannot be read; `reason` is a fixed code (`xml_refused`,
    `not_multistatus`), never the server's text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Account:
    """One CalDAV calendar of the `CALENDAR_CALDAV` secret: the collection `url` and the
    credentials. Its `repr` hides the password."""

    url: str
    user: str
    password: str

    def __repr__(self) -> str:
        return "Account(<hidden>)"


ICLOUD = "caldav.icloud.com"
_SHARD = re.compile(r"p\d{1,3}-caldav\.icloud\.com")


def shard_follow(account_host: str, allowed: Collection[str]) -> Callable[[str], bool]:
    """Which other hosts a credentialed request to `account_host` may be redirected to: for
    an iCloud account, a shard `pNN-caldav.icloud.com` that is also an allowed host (so the
    one approved in `horae.lock`); for any other account, none (same origin only). The
    global allow-list alone never lets credentials leave their host."""
    if account_host != ICLOUD and not _SHARD.fullmatch(account_host):
        return lambda host: False
    return lambda host: _SHARD.fullmatch(host) is not None and host in allowed


def accounts(secret: str, limit: int) -> list[Account] | None:
    """The accounts of the secret: whitespace-separated `url user password` triples (so a
    user name or a password holds no whitespace, and nothing is percent-decoded). `None`
    when the value is not a whole number of triples, or more than `limit` of them."""
    tokens = secret.split()
    if not tokens or len(tokens) % 3 or len(tokens) // 3 > limit:
        return None
    return [Account(*tokens[n : n + 3]) for n in range(0, len(tokens), 3)]


def query(window: Window) -> bytes:
    """The `calendar-query` body: the `VEVENT`s with their `calendar-data`, in a time range
    that is the window plus a day on each side (expansion margin), in UTC."""
    lower = (window.start - _MARGIN).astimezone(_UTC).strftime("%Y%m%dT%H%M%SZ")
    upper = (window.end + _MARGIN).astimezone(_UTC).strftime("%Y%m%dT%H%M%SZ")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:prop><C:calendar-data/></D:prop>"
        '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT">'
        f'<C:time-range start="{lower}" end="{upper}"/>'
        "</C:comp-filter></C:comp-filter></C:filter></C:calendar-query>"
    ).encode("ascii")


def parse_multistatus(xml: bytes) -> list[str]:
    """The `calendar-data` texts of a multistatus (`CalDavError` if it is not one, or is
    refused). Resources without data (a status only) are left out."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    stack: list[str] = []
    found: list[str] = []
    pieces: list[str] = []
    size = 0
    elements = 0

    def start(name: str, _attributes: dict[str, str]) -> None:
        nonlocal elements
        elements += 1
        if len(stack) >= MAX_DEPTH or elements > MAX_ELEMENTS:
            raise CalDavError("xml_refused")
        if not stack and name != f"{DAV} multistatus":
            raise CalDavError("not_multistatus")
        stack.append(name)
        if name == f"{CALDAV} calendar-data":
            pieces.clear()

    def end(name: str) -> None:
        nonlocal size
        stack.pop()
        if name == f"{CALDAV} calendar-data":
            if len(found) >= MAX_RESOURCES:
                raise CalDavError("xml_refused")
            found.append("".join(pieces))
            pieces.clear()
            size = 0

    def text(data: str) -> None:
        nonlocal size
        if stack and stack[-1] == f"{CALDAV} calendar-data":
            size += len(data)
            if size > MAX_DATA:
                raise CalDavError("xml_refused")
            pieces.append(data)

    def refuse(*_args: Any) -> NoReturn:
        raise CalDavError("xml_refused")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    # No DTD, no entity, no external reference, no processing instruction: refused outright.
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.ExternalEntityRefHandler = refuse
    parser.ProcessingInstructionHandler = refuse
    parser.SkippedEntityHandler = refuse
    try:
        parser.Parse(xml, True)
    except CalDavError:
        raise
    except expat.ExpatError:
        raise CalDavError("xml_refused") from None
    if elements == 0:
        raise CalDavError("not_multistatus")
    return found


def merge(resources: list[str]) -> tuple[bytes, int]:
    """The resources as one iCalendar feed, and how many could not be read (they are
    left out). A `VTIMEZONE` repeated in several resources is kept once, by `TZID`."""
    merged = icalendar.Calendar()
    merged.add("VERSION", "2.0")
    merged.add("PRODID", "-//horae-plugin-calendar//caldav//EN")
    zones: set[str] = set()
    bad = 0
    for resource in resources:
        try:
            calendar = parse(resource.encode("utf-8"))
        except IcsError:
            bad += 1
            continue
        for component in calendar.subcomponents:
            if component.name == "VTIMEZONE":
                tzid = str(component.get("TZID", ""))
                if tzid in zones:
                    continue
                zones.add(tzid)
            merged.add_component(component)
    return merged.to_ical(), bad
