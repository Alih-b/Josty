"""Fetch admits the fake-IP range only when asked (#129). Hermetic: DNS is stubbed."""

from __future__ import annotations

import asyncio
import socket

import pytest

from josty import cli
from josty.engine import Josty
from josty.fetch import validate_public_url


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("JOSTY_ALLOW_FAKE_IP", raising=False)


def _resolving_to(monkeypatch, address: str):
    def getaddrinfo(host, port, **kwargs):
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr("josty.fetch.socket.getaddrinfo", getaddrinfo)


def _check(url: str = "https://example.org/", **kwargs) -> None:
    asyncio.run(validate_public_url(url, timeout=1.0, **kwargs))


@pytest.mark.parametrize("address", ["198.18.0.1", "198.19.255.254"])
def test_fake_ip_is_blocked_by_default_and_the_error_names_the_setting(monkeypatch, address):
    _resolving_to(monkeypatch, address)

    with pytest.raises(ValueError, match="private or reserved network destinations") as info:
        _check()

    assert "JOSTY_ALLOW_FAKE_IP" in str(info.value)
    assert "--allow-fake-ip" in str(info.value)


@pytest.mark.parametrize("address", ["198.18.0.1", "198.19.255.254"])
def test_fake_ip_passes_when_allowed(monkeypatch, address):
    _resolving_to(monkeypatch, address)

    _check(allow_fake_ip=True)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "::ffff:198.18.0.1",
    ],
)
def test_everything_else_stays_blocked_even_when_fake_ip_is_allowed(monkeypatch, address):
    _resolving_to(monkeypatch, address)

    with pytest.raises(ValueError, match="private or reserved network destinations"):
        _check(allow_fake_ip=True)


@pytest.mark.parametrize("address", ["198.17.255.255", "198.20.0.0"])
def test_the_range_edges_are_exact(monkeypatch, address):
    """The neighbours are ordinary public addresses; the guard never looked at them."""
    _resolving_to(monkeypatch, address)

    _check()


def test_other_blocks_do_not_mention_the_setting(monkeypatch):
    _resolving_to(monkeypatch, "10.0.0.1")

    with pytest.raises(ValueError) as info:
        _check()

    assert "JOSTY_ALLOW_FAKE_IP" not in str(info.value)


def test_non_http_schemes_stay_blocked_when_allowed():
    with pytest.raises(ValueError, match="only public HTTP"):
        _check("file:///etc/passwd", allow_fake_ip=True)


def test_a_fake_ip_redirect_hop_is_checked_with_the_same_setting(monkeypatch):
    _resolving_to(monkeypatch, "198.18.0.1")
    engine = Josty(enable_cache=False, allow_fake_ip=True)
    asyncio.run(engine._validate_public_url("https://example.org/next"))
    with pytest.raises(ValueError):
        asyncio.run(Josty(enable_cache=False)._validate_public_url("https://example.org/next"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), ("0", False), ("", False), ("no", False)],
)
def test_the_environment_variable_sets_the_default(monkeypatch, value, expected):
    monkeypatch.setenv("JOSTY_ALLOW_FAKE_IP", value)

    assert Josty(enable_cache=False).allow_fake_ip is expected


def test_an_explicit_argument_beats_the_environment(monkeypatch):
    monkeypatch.setenv("JOSTY_ALLOW_FAKE_IP", "1")

    assert Josty(enable_cache=False, allow_fake_ip=False).allow_fake_ip is False


def test_the_cli_flag_reaches_the_engine_for_fetch_and_search():
    assert cli.fetch_parser().parse_args(["--allow-fake-ip", "https://example.org"]).allow_fake_ip
    assert cli.parser().parse_args(["--allow-fake-ip", "q"]).allow_fake_ip
    assert not cli.parser().parse_args(["q"]).allow_fake_ip
