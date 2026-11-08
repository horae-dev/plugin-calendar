# SPDX-License-Identifier: Apache-2.0
"""`python -m horae_calendar google-auth`: the loopback consent, against a fake token endpoint
and a fake browser (a thread that follows the consent URL's redirect). No network."""

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from horae_calendar import google_auth
from horae_calendar.__main__ import main as cli
from horae_calendar.google import SCOPE

CLIENT_ID = "1234-fake.apps.example.org"
CLIENT_SECRET = "GOCSPX-fake-client-secret"  # noqa: S105 (synthetic)


class Token(BaseHTTPRequestHandler):
    reply: tuple[int, dict[str, Any]] = (200, {"refresh_token": "1//fake-refresh"})
    forms: list[dict[str, str]] = []  # noqa: RUF012

    def log_message(self, format: str, *args: object) -> None:
        """Silent."""

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        Token.forms.append({k: v[0] for k, v in parse_qs(body).items()})
        status, data = Token.reply
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def token_url() -> Iterator[str]:
    Token.reply = (200, {"refresh_token": "1//fake-refresh"})
    Token.forms.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Token)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/token"
    httpd.shutdown()
    httpd.server_close()


def browser(answer: Callable[[dict[str, str]], dict[str, str]]) -> Callable[[str], None]:
    """A fake browser: reads the consent URL and calls the redirect URI back with `answer`'s
    query (given the URL's own: `state`...)."""
    seen: dict[str, str] = {}

    def open_url(url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        seen.update(query)
        back = answer(query)
        redirect = query["redirect_uri"]
        threading.Thread(target=httpx.get, args=(redirect,), kwargs={"params": back}).start()

    open_url.seen = seen  # type: ignore[attr-defined]
    return open_url


def test_consent_yields_the_secret_line(token_url: str) -> None:
    opener = browser(lambda q: {"code": "the-code", "state": q["state"]})
    err = StringIO()
    line = google_auth.authorize(
        CLIENT_ID, CLIENT_SECRET, open_url=opener, token_url=token_url, wait_s=10, err=err
    )
    assert line == f"{CLIENT_ID} {CLIENT_SECRET} 1//fake-refresh"
    asked = opener.seen  # type: ignore[attr-defined]
    assert asked["scope"] == SCOPE
    assert asked["access_type"] == "offline"
    assert asked["prompt"] == "consent"
    assert asked["code_challenge_method"] == "S256"
    assert asked["redirect_uri"].startswith("http://127.0.0.1:")
    (form,) = Token.forms
    assert form["code"] == "the-code"
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == asked["redirect_uri"]
    assert form["code_verifier"]
    assert "the-code" not in err.getvalue()


def authorize(token_url: str, answer: Callable[[dict[str, str]], dict[str, str]]) -> str:
    return google_auth.authorize(
        CLIENT_ID,
        CLIENT_SECRET,
        open_url=browser(answer),
        token_url=token_url,
        wait_s=10,
        err=StringIO(),
    )


def test_a_wrong_state_is_refused(token_url: str) -> None:
    with pytest.raises(google_auth.AuthError, match="state"):
        authorize(token_url, lambda q: {"code": "c", "state": "forged"})
    assert not Token.forms


def test_a_refusal_by_the_user(token_url: str) -> None:
    with pytest.raises(google_auth.AuthError, match="access_denied"):
        authorize(token_url, lambda q: {"error": "access_denied", "state": q["state"]})


def test_an_answer_without_a_code(token_url: str) -> None:
    with pytest.raises(google_auth.AuthError, match="no authorisation code"):
        authorize(token_url, lambda q: {"state": q["state"]})


def test_no_refresh_token_explains_how_to_get_one(token_url: str) -> None:
    Token.reply = (200, {"access_token": "x"})
    with pytest.raises(google_auth.AuthError, match="permissions"):
        authorize(token_url, lambda q: {"code": "c", "state": q["state"]})


def test_a_refused_code(token_url: str) -> None:
    Token.reply = (400, {"error": "invalid_grant"})
    with pytest.raises(google_auth.AuthError, match="invalid_grant"):
        authorize(token_url, lambda q: {"code": "c", "state": q["state"]})


def test_timeout_when_nobody_answers(token_url: str) -> None:
    with pytest.raises(google_auth.AuthError, match="timed out"):
        google_auth.authorize(
            CLIENT_ID,
            CLIENT_SECRET,
            open_url=lambda url: None,
            token_url=token_url,
            wait_s=0.2,
            err=StringIO(),
        )


def test_an_unreachable_token_endpoint(token_url: str) -> None:
    with pytest.raises(google_auth.AuthError, match="could not reach"):
        google_auth.authorize(
            CLIENT_ID,
            CLIENT_SECRET,
            open_url=browser(lambda q: {"code": "c", "state": q["state"]}),
            token_url="http://127.0.0.1:9/token",  # noqa: S106 (a port nobody listens on)
            wait_s=10,
            err=StringIO(),
        )


def test_the_command_prints_only_the_secret_on_stdout(
    token_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)
    real = google_auth.authorize
    fake_browser = browser(lambda q: {"code": "c", "state": q["state"]})

    def authorize_here(*args: Any, **kwargs: Any) -> str:
        return real(*args, **{**kwargs, "open_url": fake_browser, "token_url": token_url})

    monkeypatch.setattr(google_auth, "authorize", authorize_here)
    cli(["google-auth", "--no-browser"])
    out, err = capsys.readouterr()
    assert out == f"{CLIENT_ID} {CLIENT_SECRET} 1//fake-refresh\n"
    assert "1//fake-refresh" not in err


def test_the_command_fails_cleanly(token_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", CLIENT_SECRET)

    def refuse(*args: Any, **kwargs: Any) -> str:
        raise google_auth.AuthError("nope")

    monkeypatch.setattr(google_auth, "authorize", refuse)
    with pytest.raises(SystemExit, match="google-auth: nope"):
        cli(["google-auth"])
    with pytest.raises(SystemExit, match="usage"):
        cli(["google-auth", "--what"])
    with pytest.raises(SystemExit, match="usage"):
        cli(["other"])
