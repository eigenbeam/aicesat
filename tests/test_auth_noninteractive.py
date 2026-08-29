"""Earthdata login must never be able to prompt.

earthaccess's default strategy "all" tries environment -> netrc -> INTERACTIVE, and the interactive strategy reads a
username off stdin. Every place this code logs in is unattended: the web server, the benchmarks, and the MCP stdio
server — where a stdin prompt does not merely hang, it corrupts the protocol. A missing token must raise the message
that says how to supply one, not block on a prompt nobody is there to answer.
"""
import os
import sys
import types

import pytest

from aicesat import auth


class _FakeAuth:
    authenticated = True


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """A stand-in earthaccess that records the strategy instead of contacting NASA."""
    seen = []
    fake = types.ModuleType("earthaccess")
    fake.login = lambda strategy="all", **kw: (seen.append(strategy), _FakeAuth())[1]
    monkeypatch.setitem(sys.modules, "earthaccess", fake)
    monkeypatch.setattr(auth, "_auth", None)                       # module-level login cache
    monkeypatch.setattr(auth, "EDL_FILE", tmp_path / "absent" / "token.prod")
    monkeypatch.delenv("EARTHDATA_TOKEN", raising=False)
    return seen


def test_missing_token_does_not_fall_back_to_an_interactive_strategy(calls):
    auth.login()
    assert calls == ["netrc"]
    assert "all" not in calls, "earthaccess would chain through to the interactive prompt"


def test_token_file_is_used_when_present(calls, tmp_path, monkeypatch):
    p = tmp_path / "token.prod"
    p.write_text("EARTHDATA_TOKEN=abc123\n")
    monkeypatch.setattr(auth, "EDL_FILE", p)
    auth.login()
    assert calls == ["environment"]
    assert os.environ["EARTHDATA_TOKEN"] == "abc123"


def test_env_token_wins_and_stays_non_interactive(calls, monkeypatch):
    monkeypatch.setenv("EARTHDATA_TOKEN", "tok")
    auth.login()
    assert calls == ["environment"]


def test_failure_raises_the_help_text_rather_than_hanging(calls, monkeypatch):
    fake = sys.modules["earthaccess"]
    monkeypatch.setattr(fake, "login", lambda strategy="all", **kw: (_ for _ in ()).throw(RuntimeError("no creds")))
    with pytest.raises(RuntimeError, match="No working Earthdata Login"):
        auth.login()
