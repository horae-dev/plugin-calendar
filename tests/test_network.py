# SPDX-License-Identifier: Apache-2.0
"""The hosts approved in `horae.lock` (horae-sdk 0.5): a calendar on a host the core did not
approve is skipped without contacting it; `network=None` is an older core, which sent no list
(only the declared hosts and `allowed_hosts` apply)."""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

import anyio
import pytest
from horae_sdk import Plugin
from horae_sdk.testing import Replayed, call

from conftest import ENVIRON, NOW, TOKEN, URLS, FakeFetcher, url
from horae_calendar import build
from test_caldav import COLLECTION, PASSWORD, SHARD, USER, FakeReporter, multistatus

TOOL = "calendar.events"
WORK = {"name": "Work", "index": 1}
SECRETS = f"{COLLECTION} {USER} {PASSWORD}"


def run(plugin: Plugin, network: Sequence[str] | None, **params: Any) -> Replayed:
    async def go() -> Replayed:
        return await call(plugin, TOOL, {"start": "2026-10-22", **params}, network=network)

    return anyio.run(go)


def ics(fetcher: FakeFetcher) -> Plugin:
    return build(fetcher, environ=ENVIRON, clock=lambda: NOW)


def fetcher() -> FakeFetcher:
    from conftest import FEEDS

    return FakeFetcher(dict(FEEDS))


def test_an_approved_host_is_fetched() -> None:
    feeds = fetcher()
    replayed = run(ics(feeds), ["calendar.google.com"], calendars=[WORK])
    assert replayed.error is None
    assert feeds.fetched == [URLS[0]]


@pytest.mark.parametrize("network", [[], ["calendar.proton.me"]])
def test_an_unapproved_host_is_skipped_by_name_without_contact(network: list[str]) -> None:
    feeds = fetcher()
    replayed = run(ics(feeds), network, calendars=[WORK])
    assert replayed.error is not None
    assert "Work: host_not_approved: calendar.google.com" in replayed.error
    assert TOKEN not in replayed.error
    assert feeds.fetched == []


def test_with_partial_the_other_calendars_still_answer() -> None:
    feeds = fetcher()
    calendars = [WORK, {"name": "Family", "index": 2}]
    replayed = run(ics(feeds), ["calendar.proton.me"], calendars=calendars, partial=True)
    assert isinstance(replayed.data, dict)
    summaries = replayed.data["calendars"]
    assert summaries[0] == {
        "name": "Work",
        "ok": False,
        "error": "host_not_approved: calendar.google.com",
    }
    assert summaries[1]["ok"] is True
    assert replayed.data["complete"] is False
    assert feeds.fetched == [url("family", "calendar.proton.me")]


def test_an_older_core_without_a_list_keeps_the_declared_hosts() -> None:
    feeds = fetcher()
    assert run(ics(feeds), None, calendars=[WORK]).error is None
    assert feeds.fetched == [URLS[0]]
    # allowed_hosts still decides for a host that is not declared
    other = {"name": "Other", "index": 6}
    assert run(ics(feeds), None, calendars=[other]).error is not None
    assert run(ics(feeds), None, calendars=[other], allowed_hosts=["ics.example.org"]).error is None


def test_allowed_hosts_do_not_override_the_lock() -> None:
    feeds = fetcher()
    other = {"name": "Other", "index": 6}
    replayed = run(
        ics(feeds), ["calendar.google.com"], calendars=[other], allowed_hosts=["ics.example.org"]
    )
    assert replayed.error is not None
    assert "host_not_approved: ics.example.org" in replayed.error
    assert feeds.fetched == []


# --- CalDAV, with the credentialed redirect ----------------------------------------------

SPEC = {"name": "iCloud", "index": 1, "type": "caldav"}


def caldav_plugin(reporter: FakeReporter) -> Plugin:
    environ: Mapping[str, str] = {"CALENDAR_CALDAV": SECRETS}
    return build(FakeFetcher({}), reporter=reporter, environ=environ, clock=lambda: NOW)


def test_caldav_on_an_approved_host() -> None:
    reporter = FakeReporter(multistatus())
    assert run(caldav_plugin(reporter), ["caldav.icloud.com"], calendars=[SPEC]).error is None
    assert len(reporter.reports) == 1


def test_caldav_on_an_unapproved_host_is_skipped_without_credentials_sent() -> None:
    reporter = FakeReporter(multistatus())
    replayed = run(caldav_plugin(reporter), ["calendar.google.com"], calendars=[SPEC])
    assert replayed.error is not None
    assert "iCloud: host_not_approved: caldav.icloud.com" in replayed.error
    assert PASSWORD not in replayed.error
    assert reporter.reports == []


def follow_of(network: Sequence[str] | None) -> Callable[[str], bool]:
    reporter = FakeReporter(multistatus())
    replayed = run(caldav_plugin(reporter), network, calendars=[SPEC], allowed_hosts=[SHARD])
    assert replayed.error is None
    follow: Callable[[str], bool] = reporter.reports[0]["follow"]
    return follow


def test_a_credentialed_redirect_goes_only_to_an_approved_shard() -> None:
    assert follow_of(["caldav.icloud.com", SHARD])(SHARD) is True
    assert follow_of(["caldav.icloud.com"])(SHARD) is False


def test_an_older_core_keeps_the_shard_rule_alone() -> None:
    assert follow_of(None)(SHARD) is True


# --- Google --------------------------------------------------------------------------------


class FakeApi(FakeFetcher):
    """The token endpoint and `events.list`, in memory."""

    def __init__(self) -> None:
        super().__init__({})
        self.calls: list[str] = []

    async def api(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        self.calls.append(url)
        if "oauth2" in url:
            return 200, b'{"access_token": "ya29.fake", "expires_in": 3599}'
        return 200, b'{"items": []}'


GOOGLE = {"name": "Perso", "index": 1, "type": "google"}
GOOGLE_HOSTS = ["oauth2.googleapis.com", "www.googleapis.com"]


def google_plugin(api: FakeApi) -> Plugin:
    environ = {"CALENDAR_GOOGLE": "id.example.org secret-synthetic refresh-synthetic"}
    return build(FakeFetcher({}), api=api, environ=environ, clock=lambda: NOW)


def test_google_with_both_hosts_approved() -> None:
    api = FakeApi()
    assert run(google_plugin(api), GOOGLE_HOSTS, calendars=[GOOGLE]).error is None
    assert len(api.calls) == 2


@pytest.mark.parametrize(
    ("network", "host"),
    [
        ([], "oauth2.googleapis.com"),
        (["oauth2.googleapis.com"], "www.googleapis.com"),
    ],
)
def test_google_with_a_host_unapproved_is_skipped_by_name(network: list[str], host: str) -> None:
    api = FakeApi()
    replayed = run(google_plugin(api), network, calendars=[GOOGLE])
    assert replayed.error is not None
    assert f"Perso: host_not_approved: {host}" in replayed.error
    assert api.calls == []


def test_google_with_an_older_core() -> None:
    api = FakeApi()
    assert run(google_plugin(api), None, calendars=[GOOGLE]).error is None
    assert len(api.calls) == 2
