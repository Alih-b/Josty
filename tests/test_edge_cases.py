"""Edge-case probes for josty.

These tests are designed to surface contract drift, surprising behavior, and
real bugs at the boundary of the public API. They are written independently of
the regular test files so a single failure does not hide behind a passing suite.

Each test that surfaces a finding is annotated with a comment describing what
was found and what the expected behavior should be. Tests that simply document
current behavior are equally important — they pin down invariants the README
promises.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import sqlite3
import ssl
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
import pytest
from josty.cli import main as cli_main
from josty.engine import (
    SCHEMA_VERSION,
    CircuitBreaker,
    DiagnoseRun,
    Josty,
    ProviderStatus,
    SearchCache,
    SearchResult,
    SearchRun,
    _classify_probe_error,
    _classify_search_error,
    canonical,
    domain_weight,
    merge_query_variants,
    normalize_sites,
    rrf,
)

# --------------------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------------------

MOCK_PUBLIC_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]


@pytest.fixture(autouse=True)
def isolate_test_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def result(url, snippet="", source="test"):
    return SearchResult("title", url, snippet, sources=[source])


def make_parts(return_lists, providers, sites=()):
    """Build a fake ``_search_parts`` returning the correct nested-list shape."""
    async def parts(query, *, sites=None, mode="plain", limit=20, category="text",
                    region=None, safesearch="moderate", timelimit=None,
                    max_query_variants=None):
        return return_lists, providers, sites or []
    return parts


# ======================================================================================
# SECTION 1: Schema contract
# ======================================================================================

class TestSchemaContract:
    """The README promises:
       - 'stdout emits only pure, valid, parseable JSON conforming to schema_version: 1.0'
       - 'All errors, diagnostics, and warnings are strictly routed to stderr'
    """

    def test_schema_version_constant(self):
        assert SCHEMA_VERSION == "1.0"

    def test_search_run_dict_includes_schema_version(self):
        run = SearchRun("q", [result("https://example.com")], [ProviderStatus("b", "q", True, 1)])
        d = run.dict()
        assert d["schema_version"] == "1.0"
        assert d["status"] == "complete"
        assert d["count"] == 1
        assert d["partial"] is False

    def test_diagnose_dict_includes_schema_version(self):
        d = DiagnoseRun(providers=[]).dict()
        assert d["schema_version"] == "1.0"
        assert d["status"] == "failed"
        assert d["reachable"] == 0
        assert d["count"] == 0

    def test_partial_flag_matches_status(self):
        ok = ProviderStatus("b", "q", True, 1)
        failed = ProviderStatus("b2", "q", False)
        assert SearchRun("q", [result("https://a.test")], [ok]).status == "complete"
        assert SearchRun("q", [result("https://a.test")], [ok, failed]).status == "degraded"
        assert SearchRun("q", [], [failed, failed]).status == "failed"
        # Empty results but all providers OK -> still 'complete' (not 'failed')
        assert SearchRun("q", [], [ok]).status == "complete"


# ======================================================================================
# SECTION 2: CLI behavior
# ======================================================================================

class TestCLIContract:

    def test_query_required_unless_diagnose_or_clear_cache(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["josty"])
        with pytest.raises(SystemExit) as ei:
            cli_main()
        assert ei.value.code == 2

    def test_clear_cache_writes_status_to_stdout(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setattr("sys.argv", ["josty", "--clear-cache"])
        cli_main()
        out, err = capsys.readouterr()
        assert err == ""
        payload = json.loads(out)
        assert payload["status"] == "cleared"

    def test_results_only_does_not_emit_envelope(self, monkeypatch, capsys):
        async def fake_research_run(self, query, **kwargs):
            return SearchRun(
                query=query,
                results=[result("https://a.test/x", source="bing")],
                providers=[ProviderStatus("bing", query, True, 1)],
            )

        monkeypatch.setattr("josty.engine.Josty.research_run", fake_research_run)
        monkeypatch.setattr("sys.argv", ["josty", "term", "--results-only"])
        cli_main()
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert isinstance(payload, list)
        assert payload[0]["url"] == "https://a.test/x"

    def test_diagnose_results_only_conflict(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["josty", "--diagnose", "--results-only"])
        with pytest.raises(SystemExit) as ei:
            cli_main()
        assert ei.value.code == 2

    @pytest.mark.parametrize("flag,value", [
        ("--profile", "bogus"),
        ("--mode", "regex"),
        ("--category", "video"),
        ("--safe-search", "extreme"),
        ("--time-limit", "h"),
    ])
    def test_invalid_enum_flag_rejected(self, monkeypatch, flag, value):
        monkeypatch.setattr("sys.argv", ["josty", "term", flag, value])
        with pytest.raises(SystemExit) as ei:
            cli_main()
        assert ei.value.code == 2

    def test_value_error_from_engine_goes_to_stderr_as_json(self, monkeypatch, capsys):
        async def explode(self, query, **kwargs):
            raise ValueError("engine refused this query")
        monkeypatch.setattr("josty.engine.Josty.research_run", explode)
        monkeypatch.setattr("sys.argv", ["josty", "term"])
        with pytest.raises(SystemExit) as ei:
            cli_main()
        assert ei.value.code == 2
        out, err = capsys.readouterr()
        assert out == ""
        payload = json.loads(err)
        assert "engine refused this query" in payload["error"]


# ======================================================================================
# SECTION 3: canonical() invariants
# ======================================================================================

class TestCanonical:
    """Document the canonical URL contract precisely."""

    def test_hostname_lowercased_scheme_lowercased(self):
        assert canonical("HTTPS://Example.COM/") == "https://example.com/"

    def test_path_NOT_lowercased(self):
        # FINDING (intentional, but worth pinning): paths are case-sensitive and
        # not lowercased. This is RFC-correct for case-sensitive paths but
        # could surprise users (e.g. Wikipedia URLs are case-sensitive).
        assert canonical("https://Example.com/Foo/Bar") == "https://example.com/Foo/Bar"

    def test_strips_default_ports(self):
        assert canonical("http://example.com:80/x") == "http://example.com/x"
        assert canonical("https://example.com:443/x") == "https://example.com/x"

    def test_preserves_non_default_port(self):
        assert canonical("http://example.com:8080/") == "http://example.com:8080/"
        assert canonical("https://example.com:8443/x") == "https://example.com:8443/x"

    def test_drops_fragment(self):
        assert canonical("https://example.com/x#section") == "https://example.com/x"

    @pytest.mark.parametrize("key", [
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "dclid", "fbclid", "gclid", "mc_cid", "mc_eid", "msclkid",
    ])
    def test_strips_tracking_keys(self, key):
        url = f"https://example.com/?{key}=abc&q=keep"
        assert canonical(url) == "https://example.com/?q=keep"

    def test_preserves_repeated_query_values(self):
        assert canonical("https://example.com/?tag=a&tag=b") == "https://example.com/?tag=a&tag=b"

    def test_ipv6_bracketed(self):
        c = canonical("https://[2606:4700:4700::1111]/path")
        assert c == "https://[2606:4700:4700::1111]/path"

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "javascript:alert(1)",
    ])
    def test_rejects_non_http_schemes(self, url):
        with pytest.raises(ValueError):
            canonical(url)

    def test_rejects_credential_url(self):
        with pytest.raises(ValueError):
            canonical("https://user:pass@example.com/")

    @pytest.mark.parametrize("url", ["", "not a url", "/relative/path"])
    def test_rejects_empty_or_relative(self, url):
        with pytest.raises(ValueError):
            canonical(url)

    def test_strips_trailing_slash_only_on_root(self):
        # Behavior: non-root paths keep trailing slash; root '/' is preserved
        assert canonical("https://example.com") == "https://example.com/"
        assert canonical("https://example.com/") == "https://example.com/"
        assert canonical("https://example.com/foo") == "https://example.com/foo"
        assert canonical("https://example.com/foo/") == "https://example.com/foo"

    def test_blank_query_value_preserved(self):
        # keep_blank_values=True, so ?x= is preserved
        assert canonical("https://example.com/?x=&y=1") == "https://example.com/?x=&y=1"

    def test_www_and_apex_are_distinct(self):
        # FINDING: www.example.com and example.com are kept as distinct
        # canonical keys. Whether that is a bug or feature depends on policy;
        # flag for awareness. In practice this means the same article at
        # https://example.com/x and https://www.example.com/x would NOT fuse
        # via RRF.
        a = canonical("https://www.example.com/x")
        b = canonical("https://example.com/x")
        assert a != b


# ======================================================================================
# SECTION 4: domain_weight() behavior
# ======================================================================================

class TestDomainWeight:

    def test_empty_url_returns_one(self):
        assert domain_weight("") == 1.0
        assert domain_weight("not a url") == 1.0

    def test_ip_address_returns_one(self):
        assert domain_weight("https://93.184.216.34/x") == 1.0

    def test_www_prefix_stripped(self):
        # domain_weight strips www. before matching
        assert domain_weight("https://www.github.com/x") == domain_weight("https://github.com/x")

    def test_sibling_domain_does_not_match(self):
        # githubx.com should NOT match github.com (suffix-match guard)
        assert domain_weight("https://githubx.com/x") == 1.0

    def test_subdomain_match_in_dev_profile(self):
        assert domain_weight("https://api.github.com/x", profile="dev") == 1.3
        assert domain_weight("https://gist.github.com/x", profile="dev") == 1.3

    def test_docs_subdomain_match(self):
        assert domain_weight("https://docs.example.com/x") == 1.2
        assert domain_weight("https://docs.example.com/x", profile="dev") == 1.3

    def test_readthedocs_match(self):
        assert domain_weight("https://example.readthedocs.io/x") == 1.2
        assert domain_weight("https://example.readthedocs.io/x", profile="dev") == 1.3

    def test_spam_penalty_general(self):
        assert domain_weight("https://pinterest.com/x") == 0.6
        assert domain_weight("https://geeksforgeeks.org/x") == 0.6

    def test_spam_penalty_dev_academic_more_severe(self):
        assert domain_weight("https://pinterest.com/x", profile="dev") == 0.5
        assert domain_weight("https://geeksforgeeks.org/x", profile="academic") == 0.5

    def test_academic_boost(self):
        assert domain_weight("https://arxiv.org/abs/1", profile="academic") == 1.4
        # In general profile arxiv is not boosted
        assert domain_weight("https://arxiv.org/abs/1") == 1.0


# ======================================================================================
# SECTION 5: rrf() and merge_query_variants() behavior
# ======================================================================================

class TestRRF:
    def test_empty_input(self):
        assert rrf([]) == []
        assert rrf([[]]) == []
        assert rrf([[], []]) == []

    def test_invalid_k_raises(self):
        with pytest.raises(ValueError):
            rrf([], k=0)
        with pytest.raises(ValueError):
            rrf([], k=-1)

    def test_dedup_within_list_keeps_first(self):
        # FINDING: When the same URL appears twice in one list, RRF keeps the
        # FIRST occurrence and does not merge snippet metadata. This is by
        # design (votes are unique per backend) but is asymmetric with respect
        # to cross-list merging.
        first = [
            result("https://example.com/a", "first"),
            result("https://example.com/a", "second longer snippet"),
        ]
        results = rrf([first])
        assert len(results) == 1
        # First occurrence's snippet is kept
        assert results[0].snippet == "first"

    def test_cross_list_merge_uses_longer_snippet(self):
        first = [result("https://example.com/a", "first", "bing")]
        second = [result("https://example.com/a", "longer snippet from second", "brave")]
        results = rrf([first, second])
        assert len(results) == 1
        # Cross-list merge keeps the longer snippet
        assert results[0].snippet == "longer snippet from second"
        # Sources from both backends preserved
        assert results[0].sources == ["bing", "brave"]

    def test_invalid_urls_silently_skipped(self):
        assert rrf([[result("https://example.com:invalid-port/")]]) == []

    def test_score_rounded_to_6_decimals(self):
        first = [result("https://example.com/a")]
        out = rrf([first])
        # 1/61 ~ 0.01639344... -> round(_, 6) = 0.016393
        assert out[0].score == 0.016393

    def test_profile_multiplies_score(self):
        # Same URL across two lists -> score adds, profile multiplies each vote
        a = [result("https://pypi.org/x")]
        b = [result("https://pypi.org/x")]
        general = rrf([a, b], profile="general")  # pypi.org weight 1.2
        # Two votes of weight 1.2 at rank 1: 2 * 1.2 / 61
        assert general[0].score == round(2 * 1.2 / 61, 6)
        # dev: weight 1.3
        dev = rrf([a, b], profile="dev")
        assert dev[0].score == round(2 * 1.3 / 61, 6)
        assert dev[0].score > general[0].score

    def test_spam_domain_demoted(self):
        a = [result("https://pinterest.com/x")]
        b = [result("https://example.com/y")]
        out = rrf([a, b], profile="general")
        # example.com (1.0) beats pinterest (0.6)
        assert out[0].url == "https://example.com/y"
        assert out[1].url == "https://pinterest.com/x"

    def test_sources_deduped_preserve_order(self):
        first = [result("https://example.com/a", source="a")]
        second = [result("https://example.com/a", source="b")]
        third = [result("https://example.com/a", source="a")]  # duplicate source
        out = rrf([first, second, third])
        assert out[0].sources == ["a", "b"]

    def test_caller_lists_not_mutated(self):
        first = [result("https://example.com/a", "first snippet")]
        second = [result("https://example.com/a", "second snippet is longer")]
        rrf([first, second])
        # First list's snippet is preserved (cloned)
        assert first[0].snippet == "first snippet"

    def test_two_distinct_urls_keep_separate_scores(self):
        first = [result("https://example.com/a")]
        second = [result("https://example.com/b")]
        out = rrf([first, second])
        # Each appears in only one list at rank 1 -> 1/61 each, equal score
        assert out[0].score == out[1].score == round(1 / 61, 6)


class TestMergeQueryVariants:
    def test_min_rank_per_key(self):
        a = [result("https://example.com/a", source="x")]
        b = [
            result("https://example.com/a", "more", "x"),
            result("https://example.com/b", source="x"),
        ]
        merged = merge_query_variants([a, b])
        assert [item.url for item in merged] == ["https://example.com/a", "https://example.com/b"]

    def test_invalid_urls_skipped(self):
        merged = merge_query_variants([
            [result("https://example.com:bad/x")],
            [result("https://example.com/ok")],
        ])
        assert [item.url for item in merged] == ["https://example.com/ok"]


# ======================================================================================
# SECTION 6: normalize_sites() behavior
# ======================================================================================

class TestNormalizeSites:
    def test_lowercases_and_strips_www(self):
        assert normalize_sites(["WWW.GitHub.COM", "github.com"]) == ["github.com"]

    def test_strips_trailing_dot(self):
        # FQDN trailing dot
        assert normalize_sites(["github.com."]) == ["github.com"]

    @pytest.mark.parametrize("site,reason", [
        ("https://github.com", "scheme"),
        ("github.com/path", "path"),
        ("github.com:443", "port"),
        ("gíthub.com", "unicode"),
        ("github!.com", "invalid char"),
    ])
    def test_invalid_sites_rejected(self, site, reason):
        with pytest.raises(ValueError, match="invalid site"):
            normalize_sites([site])

    def test_label_too_long_rejected(self):
        with pytest.raises(ValueError):
            normalize_sites(["a" * 64 + ".com"])

    @pytest.mark.parametrize("site", ["-foo.com", "foo-.com", ".com", "foo..com"])
    def test_label_edge_cases_rejected(self, site):
        with pytest.raises(ValueError):
            normalize_sites([site])

    def test_max_sites_rejected(self):
        with pytest.raises(ValueError, match="at most"):
            normalize_sites([f"s{i}.t" for i in range(6)])

    def test_empty_input(self):
        assert normalize_sites(None) == []
        assert normalize_sites([]) == []

    def test_preserves_order_dedups(self):
        assert normalize_sites(["a.com", "b.com", "a.com"]) == ["a.com", "b.com"]


# ======================================================================================
# SECTION 7: SSRF guard behavior
# ======================================================================================

class TestSSRFGuard:
    """The SSRF guard is documented to block:
       - loopback (127.0.0.0/8, ::1)
       - private subnets (10/8, 172.16/12, 192.168/16)
       - link-local (169.254/16, fe80::/10)
       - metadata service (169.254.169.254)
    """

    def _set_dns(self, monkeypatch, addresses):
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *args, **kwargs: [
                (family, socket.SOCK_STREAM, 6, "", (addr, 80)) for family, addr in addresses
            ],
        )

    @pytest.mark.parametrize("address,family", [
        ("127.0.0.1", socket.AF_INET),
        ("169.254.169.254", socket.AF_INET),
        ("10.0.0.1", socket.AF_INET),
        ("::1", socket.AF_INET6),
    ])
    def test_blocks_documented_reserved(self, monkeypatch, address, family):
        self._set_dns(monkeypatch, [(family, address)])
        with pytest.raises(ValueError, match="private or reserved"):
            asyncio.run(Josty()._validate_public_url("http://example.test"))

    def test_blocks_ipv4_mapped_ipv6_loopback(self, monkeypatch):
        # ::ffff:127.0.0.1 is the IPv4-mapped IPv6 representation of loopback
        addr = ipaddress.ip_address("::ffff:127.0.0.1")
        assert not addr.is_global
        self._set_dns(monkeypatch, [(socket.AF_INET6, "::ffff:127.0.0.1")])
        with pytest.raises(ValueError, match="private or reserved"):
            asyncio.run(Josty()._validate_public_url("http://example.test"))

    def test_blocks_carrier_grade_nat(self, monkeypatch):
        # 100.64.0.0/10 (CGN) is shared address space
        self._set_dns(monkeypatch, [(socket.AF_INET, "100.64.0.1")])
        with pytest.raises(ValueError, match="private or reserved"):
            asyncio.run(Josty()._validate_public_url("http://example.test"))

    def test_blocks_reserved_future_use(self, monkeypatch):
        # 240.0.0.0/4 is reserved for future use
        self._set_dns(monkeypatch, [(socket.AF_INET, "240.0.0.1")])
        with pytest.raises(ValueError, match="private or reserved"):
            asyncio.run(Josty()._validate_public_url("http://example.test"))

    def test_blocks_unspecified_address(self, monkeypatch):
        self._set_dns(monkeypatch, [(socket.AF_INET, "0.0.0.0")])
        with pytest.raises(ValueError, match="private or reserved"):
            asyncio.run(Josty()._validate_public_url("http://example.test"))

    def test_does_not_block_multicast(self, monkeypatch):
        # FINDING: 224.0.0.0/4 is IPv4 multicast and Python's is_global returns
        # True for it. The guard therefore does not block multicast. This is
        # a real gap: an attacker could trick the fetcher into attempting to
        # connect to a multicast address. Mitigation: httpx would still need
        # to actually dial the address.
        self._set_dns(monkeypatch, [(socket.AF_INET, "224.0.0.1")])
        # The current code allows this — no exception raised.
        asyncio.run(Josty()._validate_public_url("http://example.test"))

    def test_blocks_multihomed_dns_with_private(self, monkeypatch):
        # If DNS returns one public and one private address, the guard must
        # reject (the worst case wins).
        self._set_dns(monkeypatch, [
            (socket.AF_INET, "93.184.216.34"),
            (socket.AF_INET, "10.0.0.5"),
        ])
        with pytest.raises(ValueError, match="private or reserved"):
            asyncio.run(Josty()._validate_public_url("http://example.test"))

    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "ftp://example.com",
    ])
    def test_blocks_unsafe_schemes(self, url):
        with pytest.raises(ValueError):
            asyncio.run(Josty()._validate_public_url(url))

    def test_blocks_credential_url(self):
        with pytest.raises(ValueError, match="credentials"):
            asyncio.run(Josty()._validate_public_url("https://user:pass@example.com/"))

    def test_blocks_url_without_hostname(self):
        with pytest.raises(ValueError, match="HTTP"):
            asyncio.run(Josty()._validate_public_url("https:///path"))

    def test_allows_public_ipv4(self, monkeypatch):
        self._set_dns(monkeypatch, [(socket.AF_INET, "93.184.216.34")])
        asyncio.run(Josty()._validate_public_url("https://example.com"))

    def test_dns_failure_raises_value_error(self, monkeypatch):
        def boom(*args, **kwargs):
            raise socket.gaierror(-2, "Name or service not known")
        monkeypatch.setattr(socket, "getaddrinfo", boom)
        with pytest.raises(ValueError, match="could not be resolved"):
            asyncio.run(Josty()._validate_public_url("https://nonexistent.test"))

    def test_dns_timeout_raises_value_error(self, monkeypatch):
        def slow(*args, **kwargs):
            raise TimeoutError()
        monkeypatch.setattr(socket, "getaddrinfo", slow)
        with pytest.raises(ValueError):
            asyncio.run(Josty()._validate_public_url("https://example.com"))


# ======================================================================================
# SECTION 8: fetch_content and _download behavior
# ======================================================================================

class TestFetchContent:

    def _allow_validation(self, monkeypatch):
        async def allow(self, url):
            return None
        monkeypatch.setattr(Josty, "_validate_public_url", allow)

    def test_truncation_to_max_content_chars(self, monkeypatch):
        self._allow_validation(monkeypatch)
        async def download(self, client, url):
            return f"<html><body><p>{'a' * 500}</p></body></html>", url
        monkeypatch.setattr(Josty, "_download", download)
        engine = Josty(max_content_chars=100)
        item = result("https://example.com/x")
        asyncio.run(engine.fetch_content([item]))
        assert item.content is not None
        assert len(item.content) == 100

    def test_zero_max_content_means_unlimited(self, monkeypatch):
        self._allow_validation(monkeypatch)
        async def download(self, client, url):
            return f"<html><body><p>{'a' * 5000}</p></body></html>", url
        monkeypatch.setattr(Josty, "_download", download)
        engine = Josty(max_content_chars=0)
        item = result("https://example.com/x")
        asyncio.run(engine.fetch_content([item]))
        assert item.content is not None
        assert len(item.content) >= 5000

    def test_failed_download_records_error(self, monkeypatch):
        self._allow_validation(monkeypatch)
        async def download(self, client, url):
            raise ValueError("oops")
        monkeypatch.setattr(Josty, "_download", download)
        engine = Josty()
        item = result("https://example.com/x")
        asyncio.run(engine.fetch_content([item]))
        assert item.content is None
        assert "oops" in item.fetch_error
        assert "ValueError" in item.fetch_error

    def test_redirect_loop_recorded_as_error(self, monkeypatch):
        self._allow_validation(monkeypatch)
        async def download(self, client, url):
            raise ValueError("too many redirects")
        monkeypatch.setattr(Josty, "_download", download)
        engine = Josty()
        item = result("https://example.com/x")
        asyncio.run(engine.fetch_content([item]))
        assert "too many redirects" in item.fetch_error

    def test_content_type_octet_stream_rejected_by_download(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: MOCK_PUBLIC_ADDRINFO)
        engine = Josty()
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "application/octet-stream"}, content=b"x"
            )
        )
        async def run():
            async with httpx.AsyncClient(transport=transport) as client:
                await engine._download(client, "https://example.com/x")
        with pytest.raises(ValueError, match="unsupported content type"):
            asyncio.run(run())

    def test_content_length_over_limit_rejected_by_download(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: MOCK_PUBLIC_ADDRINFO)
        engine = Josty(max_download_bytes=10)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "text/plain", "content-length": "1000000"}
            )
        )
        async def run():
            async with httpx.AsyncClient(transport=transport) as client:
                await engine._download(client, "https://example.com/x")
        with pytest.raises(ValueError, match="download limit"):
            asyncio.run(run())

    def test_stream_size_over_limit_rejected_by_download(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: MOCK_PUBLIC_ADDRINFO)
        engine = Josty(max_download_bytes=10)
        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"x" * 100
            )
        )
        async def run():
            async with httpx.AsyncClient(transport=transport) as client:
                await engine._download(client, "https://example.com/x")
        with pytest.raises(ValueError, match="download limit"):
            asyncio.run(run())

    def test_redirect_to_file_scheme_rejected_by_validate(self, monkeypatch):
        """A redirect to file:// should be rejected by _validate_public_url on
        the next iteration of the redirect loop."""
        orig = socket.getaddrinfo
        socket.getaddrinfo = lambda *a, **k: MOCK_PUBLIC_ADDRINFO
        try:
            transport = httpx.MockTransport(
                lambda req: httpx.Response(302, headers={"location": "file:///etc/passwd"})
            )
            async def run():
                async with httpx.AsyncClient(transport=transport) as client:
                    await Josty()._download(client, "https://example.com/x")
            with pytest.raises(ValueError, match="only public HTTP"):
                asyncio.run(run())
        finally:
            socket.getaddrinfo = orig

    def test_fetch_records_timestamp_and_final_url(self, monkeypatch):
        self._allow_validation(monkeypatch)
        async def download(self, client, url):
            return "<html><body><h1>x</h1></body></html>", "https://final.example.com/x"
        monkeypatch.setattr(Josty, "_download", download)
        engine = Josty()
        item = result("https://example.com/x")
        asyncio.run(engine.fetch_content([item]))
        assert item.fetched_at is not None
        assert item.fetched_url == "https://final.example.com/x"

    def test_fetch_preserves_item_count(self, monkeypatch):
        self._allow_validation(monkeypatch)
        urls = [f"https://example.com/p{i}" for i in range(8)]
        async def download(self, client, url):
            return f"<html><body><h1>{url}</h1></body></html>", url
        monkeypatch.setattr(Josty, "_download", download)
        engine = Josty()
        items = [result(u) for u in urls]
        asyncio.run(engine.fetch_content(items))
        for item in items:
            assert item.content is not None
            assert item.fetch_error is None


# ======================================================================================
# SECTION 9: expand() / query variant edge cases
# ======================================================================================

class TestExpand:

    def test_empty_query_raises(self):
        with pytest.raises(ValueError, match="empty"):
            Josty.expand("")

    def test_whitespace_only_query_raises(self):
        with pytest.raises(ValueError, match="empty"):
            Josty.expand("   ")

    def test_unsupported_mode_raises(self):
        with pytest.raises(ValueError, match="mode"):
            Josty.expand("query", mode="regex")

    def test_invalid_max_query_variants_raises(self):
        with pytest.raises(ValueError, match="positive"):
            Josty.expand("q", max_query_variants=0)
        with pytest.raises(ValueError, match="positive"):
            Josty.expand("q", max_query_variants=-3)

    def test_plain_mode_no_extra_variants(self):
        assert Josty.expand("foo bar") == ["foo bar"]

    def test_exact_mode_quotes(self):
        assert Josty.expand("foo bar", mode="exact") == ["foo bar", '"foo bar"']

    def test_oss_mode_three_variants(self):
        assert Josty.expand("foo", mode="oss") == [
            "foo", '"foo"', "foo open source", "foo self-hosted"
        ]

    def test_max_query_variants_caps(self):
        # 4 oss variants, cap at 2
        assert Josty.expand("foo", mode="oss", max_query_variants=2) == ["foo", '"foo"']

    def test_max_query_variants_with_sites(self):
        # 2 sites x 2 variants = 4, cap at 3
        out = Josty.expand("q", sites=["a.com", "b.com"], mode="exact", max_query_variants=3)
        assert len(out) == 3
        assert all(v.startswith("site:") for v in out)

    def test_dedup_after_site_expansion(self):
        # If the same site filter appears twice, dedup
        out = Josty.expand("q", sites=["github.com", "github.com"], mode="plain")
        assert out == ["site:github.com q"]


# ======================================================================================
# SECTION 10: cache behavior
# ======================================================================================

class TestCache:
    def test_hash_key_stable(self):
        a = SearchCache.hash_key("Q", limit=5)
        b = SearchCache.hash_key("Q", limit=5)
        assert a == b

    def test_hash_key_case_insensitive_on_query(self):
        a = SearchCache.hash_key("Python", limit=5)
        b = SearchCache.hash_key("python", limit=5)
        assert a == b  # query is lowercased before hashing

    def test_hash_key_query_whitespace_stripped(self):
        a = SearchCache.hash_key(" python ", limit=5)
        b = SearchCache.hash_key("python", limit=5)
        assert a == b

    def test_hash_key_includes_kwargs(self):
        a = SearchCache.hash_key("q", limit=5)
        b = SearchCache.hash_key("q", limit=6)
        assert a != b

    def test_ttl_negative_expires_immediately(self, tmp_path):
        cache = SearchCache(tmp_path / "c.db", default_ttl=0.0)
        key = SearchCache.hash_key("k")
        cache.set(key, {"x": 1}, ttl=-1.0)
        # The expires_at is in the past, so get() should return None
        assert cache.get(key) is None

    def test_clear_removes_all(self, tmp_path):
        cache = SearchCache(tmp_path / "c.db", default_ttl=60.0)
        for i in range(3):
            cache.set(SearchCache.hash_key(f"k{i}"), {"i": i})
        cache.clear()
        for i in range(3):
            assert cache.get(SearchCache.hash_key(f"k{i}")) is None

    def test_corrupted_db_does_not_crash(self, tmp_path):
        db_path = tmp_path / "bad.db"
        db_path.write_text("not a sqlite database")
        cache = SearchCache(db_path, default_ttl=60.0)
        assert cache.get("anything") is None
        # set() should also fail silently
        cache.set("k", {"x": 1})

    def test_db_uses_wal(self, tmp_path):
        cache = SearchCache(tmp_path / "c.db", default_ttl=60.0)
        cache.set(SearchCache.hash_key("k"), {"x": 1})
        with sqlite3.connect(str(cache.db_path)) as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"

    def test_empty_results_NOT_cached(self, monkeypatch, tmp_path):
        """FINDING (intentional but worth pinning): empty results are NOT cached.

        The engine only writes the cache when ``len(run.results) > 0``. This
        means a query that returns 0 results will always re-hit the network,
        even though the run is reported as a success (per the recent
        'decoupled circuit breaking' refactor).

        With the query relaxation feature, an empty 3+ word query triggers
        TWO calls (original + relaxation) on each run. Two runs = 4 total
        DDGS calls. This pins both behaviors down.
        """
        class EmptyDDGS:
            def __init__(self, **kwargs): pass
            def text(self, *args, **kwargs):
                return []
        monkeypatch.setattr("josty.engine.DDGS", EmptyDDGS)
        engine = Josty(backends=("empty",), cache_db=tmp_path / "c.db")
        calls = {"n": 0}
        original = EmptyDDGS.text
        def counting(self, *a, **kw):
            calls["n"] += 1
            return original(self, *a, **kw)
        monkeypatch.setattr(EmptyDDGS, "text", counting)
        # 3+ word query: triggers relaxation fallback in addition to original
        asyncio.run(engine.search_run("alpha beta gamma", limit=5))
        asyncio.run(engine.search_run("alpha beta gamma", limit=5))
        # Empty results not cached -> 2 calls per run (original + relaxation) x 2 runs = 4
        assert calls["n"] == 4

    def test_empty_ddgs_results_count_as_success(self, monkeypatch):
        """Per the recent refactor: when ddgs raises 'no results found' it is
        classified as 'empty' and treated as a success (record_success on the
        breaker). This means a sequence of empty queries does NOT trip the
        circuit breaker — they look like successful queries with 0 results."""
        from ddgs.exceptions import DDGSException
        class EmptyDDGS:
            def __init__(self, **kwargs): pass
            def text(self, *args, **kwargs):
                raise DDGSException("no results found")
        monkeypatch.setattr("josty.engine.DDGS", EmptyDDGS)
        engine = Josty(backends=("empty",))
        run = asyncio.run(engine.search_run("anything", limit=5))
        # The provider reports success (no error), but with 0 results
        assert run.providers[0].ok is True
        assert run.providers[0].error is None
        assert run.results == []
        assert run.status == "complete"


# ======================================================================================
# SECTION 11: circuit breaker concurrency and clock behavior
# ======================================================================================

class TestCircuitBreakerAdvanced:
    def test_failures_outside_window_dont_count(self, monkeypatch):
        breaker = CircuitBreaker(fail_threshold=2, window_seconds=10, cool_down_seconds=5)
        t = [1000.0]
        monkeypatch.setattr("josty.engine.time.monotonic", lambda: t[0])
        breaker.record_failure("bing", "search")
        # Move time past the window
        t[0] = 1011.0
        breaker.record_failure("bing", "search")
        # Two failures, but only the second falls within the window -> still allowed
        assert breaker.status("bing", "search")[0] is True

    def test_concurrent_failures_dont_open_until_threshold(self):
        # Many backends failing in parallel should not cause one of them to
        # open prematurely because of shared state.
        import threading
        breaker = CircuitBreaker(fail_threshold=3, window_seconds=60, cool_down_seconds=30)

        def hammer(backend):
            for _ in range(2):
                breaker.record_failure(backend, "search")

        threads = [threading.Thread(target=hammer, args=(f"b{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for i in range(10):
            # Only 2 failures per backend, threshold is 3, so all should be allowed
            assert breaker.status(f"b{i}", "search")[0] is True

    def test_window_clears_on_recovery(self, monkeypatch):
        # After cool-down, the failure window should reset.
        breaker = CircuitBreaker(fail_threshold=2, window_seconds=10, cool_down_seconds=5)
        t = [1000.0]
        monkeypatch.setattr("josty.engine.time.monotonic", lambda: t[0])
        breaker.record_failure("bing", "search")
        breaker.record_failure("bing", "search")
        assert breaker.status("bing", "search")[0] is False  # opened
        # Cool down expires
        t[0] = 1010.0
        assert breaker.status("bing", "search")[0] is True
        # The 2 stale failures get cleared on first status() call, so a single
        # new failure should NOT re-open immediately.
        breaker.record_failure("bing", "search")
        assert breaker.status("bing", "search")[0] is True

    def test_status_calls_do_not_count_as_failures(self):
        breaker = CircuitBreaker(fail_threshold=2, window_seconds=60, cool_down_seconds=30)
        for _ in range(100):
            assert breaker.status("bing", "search")[0] is True

    def test_record_success_on_unknown_is_noop(self):
        breaker = CircuitBreaker()
        # Should not raise
        breaker.record_success("never-failed", "search")

    def test_status_on_unknown_is_allowed(self):
        breaker = CircuitBreaker()
        assert breaker.status("never-failed", "search") == (True, None)


# ======================================================================================
# SECTION 12: _classify_search_error() and _classify_probe_error()
# ======================================================================================

class TestClassifyError:
    def test_ddgs_timeout_is_network(self):
        from ddgs.exceptions import TimeoutException
        assert _classify_search_error(TimeoutException("slow")) == "network"

    def test_ddgs_ratelimit_is_rate_limited(self):
        from ddgs.exceptions import RatelimitException
        assert _classify_search_error(RatelimitException("429")) == "rate_limited"

    def test_httpx_429_is_rate_limited(self):
        request = httpx.Request("GET", "u")
        resp = httpx.Response(429, request=request)
        exc = httpx.HTTPStatusError("Too Many", request=request, response=resp)
        assert _classify_search_error(exc) == "rate_limited"

    def test_httpx_500_is_network(self):
        request = httpx.Request("GET", "u")
        resp = httpx.Response(500, request=request)
        exc = httpx.HTTPStatusError("Server Error", request=request, response=resp)
        assert _classify_search_error(exc) == "network"

    def test_httpx_400_is_parse(self):
        request = httpx.Request("GET", "u")
        resp = httpx.Response(400, request=request)
        exc = httpx.HTTPStatusError("Bad", request=request, response=resp)
        assert _classify_search_error(exc) == "parse"

    def test_unknown_exception_is_unknown(self):
        assert _classify_search_error(ValueError("?")) == "unknown"

    def test_ddgs_no_results_is_empty(self):
        from ddgs.exceptions import DDGSException
        assert _classify_search_error(DDGSException("no results found")) == "empty"


class TestClassifyProbeError:
    def test_timeout(self):
        assert _classify_probe_error(httpx.TimeoutException("slow")) == "timeout"

    def test_dns(self):
        exc = httpx.ConnectError("x", request=httpx.Request("GET", "u"))
        exc.__cause__ = socket.gaierror(-2, "Name or service not known")
        assert _classify_probe_error(exc) == "dns"

    def test_tls(self):
        exc = httpx.ConnectError("x", request=httpx.Request("GET", "u"))
        exc.__cause__ = ssl.SSLError(1, "x")
        assert _classify_probe_error(exc) == "tls"

    def test_generic_connect(self):
        exc = httpx.ConnectError("refused", request=httpx.Request("GET", "u"))
        # No __cause__, so it falls to "network"
        assert _classify_probe_error(exc) == "network"

    def test_unknown(self):
        assert _classify_probe_error(RuntimeError("?")) == "unknown"


# ======================================================================================
# SECTION 13: GitHub API path
# ======================================================================================

class TestGitHubRun:
    def test_github_breaker_already_open_skips_call(self, monkeypatch):
        breaker = CircuitBreaker(fail_threshold=1, window_seconds=60, cool_down_seconds=30)
        breaker.record_failure("github-api", "search")

        async def explode(self, url, **kwargs):
            raise AssertionError("network call should not happen")
        monkeypatch.setattr(httpx.AsyncClient, "get", explode)
        engine = Josty(breaker=breaker)
        results, status = asyncio.run(engine.github_run("x", 5))
        assert results == []
        assert status.ok is False
        assert "skipped" in (status.error or "")

    def test_github_404_status_is_parse_error(self, monkeypatch):
        request = httpx.Request("GET", "https://api.github.com/search/repositories")
        async def fake_get(self, url, **kwargs):
            return httpx.Response(404, request=request, json={"message": "Not Found"})
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        results, status = asyncio.run(Josty().github_run("q", 5))
        assert results == []
        assert status.ok is False
        assert status.error_kind == "parse"

    def test_github_skips_malformed_items(self, monkeypatch):
        async def fake_get(self, url, **kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "items": [
                        {"full_name": "ok/repo", "html_url": "https://github.com/ok/repo"},
                        {"full_name": "no-url", "html_url": ""},  # empty url
                        {"full_name": "", "html_url": "https://github.com/empty"},  # empty name
                        "not a dict",  # dropped: not a dict
                    ]
                },
            )
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        results, status = asyncio.run(Josty().github_run("q", 10))
        assert len(results) == 1
        assert results[0].url == "https://github.com/ok/repo"

    def test_github_uses_token_when_provided(self, monkeypatch):
        captured = {}
        async def fake_get(self, url, **kwargs):
            captured.update(dict(self.headers))
            return httpx.Response(200, request=httpx.Request("GET", url), json={"items": []})
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        asyncio.run(Josty(github_token="secret-token").github_run("q", 5))
        # Header is lowercased by httpx
        assert "authorization" in captured
        assert captured["authorization"] == "Bearer secret-token"

    def test_github_no_token_omits_authorization(self, monkeypatch):
        captured = {}
        async def fake_get(self, url, **kwargs):
            captured.update(dict(self.headers))
            return httpx.Response(200, request=httpx.Request("GET", url), json={"items": []})
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        asyncio.run(Josty().github_run("q", 5))
        assert "authorization" not in captured

    def test_github_response_with_no_items_key(self, monkeypatch):
        async def fake_get(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("GET", url), json={"total_count": 0})
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        results, status = asyncio.run(Josty().github_run("q", 5))
        assert results == []
        assert status.ok is True


# ======================================================================================
# SECTION 14: research_run integration
# ======================================================================================

class TestResearchRun:

    def test_failed_run_does_not_get_cached(self, monkeypatch, tmp_path):
        class BrokenDDGS:
            def __init__(self, **kwargs): pass
            def text(self, *args, **kwargs):
                raise RuntimeError("nope")
        monkeypatch.setattr("josty.engine.DDGS", BrokenDDGS)
        engine = Josty(backends=("broken",), cache_db=tmp_path / "c.db")
        run1 = asyncio.run(engine.search_run("q", limit=1))
        run2 = asyncio.run(engine.search_run("q", limit=1))
        assert run1.status == "failed"
        assert run2.status == "failed"

    def test_search_and_research_return_same_envelope_when_no_github(self, monkeypatch, tmp_path):
        engine = Josty(backends=("test",), cache_db=tmp_path / "c.db")
        inner = [result("https://example.com/x", source="test")]
        parts = make_parts([inner], [ProviderStatus("test", "q", True, 1)])
        monkeypatch.setattr(engine, "_search_parts", parts)
        s = asyncio.run(engine.search_run("q", limit=5))
        r = asyncio.run(engine.research_run("q", limit=5))
        assert s.dict() == r.dict()

    def test_research_with_github_falls_back_when_github_fails(self, monkeypatch, tmp_path):
        engine = Josty(cache_db=tmp_path / "c.db")
        inner = [result("https://example.com/x")]
        parts = make_parts([inner], [ProviderStatus("bing", "q", True, 1)])
        async def github_fails(query, limit):
            return [], ProviderStatus("github-api", "q", False, error="boom", error_kind="unknown")
        monkeypatch.setattr(engine, "_search_parts", parts)
        monkeypatch.setattr(engine, "github_run", github_fails)
        run = asyncio.run(engine.research_run("q", limit=5, include_github=True))
        assert run.results[0].url == "https://example.com/x"
        assert any(not p.ok for p in run.providers)
        assert run.status == "degraded"

    def test_news_category_uses_news_backends(self, monkeypatch, tmp_path):
        engine = Josty(cache_db=tmp_path / "c.db")
        captured = {}
        async def parts(query, *, sites, mode, limit, category, **kw):
            captured["category"] = category
            return [], [ProviderStatus("bing-news", query, True, 0)], sites or []
        monkeypatch.setattr(engine, "_search_parts", parts)
        asyncio.run(engine.search_run("q", limit=5, category="news"))
        assert captured["category"] == "news"

    def test_site_filter_limits_results(self, monkeypatch, tmp_path):
        engine = Josty(backends=("test",), cache_db=tmp_path / "c.db")
        inner = [
            result("https://github.com/owner/repo"),
            result("https://other.com/something"),
        ]
        # Return [inner] (list of one list) — one backend group returned two
        # candidates, one matches the site filter, one doesn't.
        async def parts(query, *, sites, mode, limit, category, **kw):
            return [inner], [ProviderStatus("test", query, True, 2)], sites or []
        monkeypatch.setattr(engine, "_search_parts", parts)
        run = asyncio.run(engine.search_run("q", limit=5, sites=["github.com"]))
        urls = [r.url for r in run.results]
        assert urls == ["https://github.com/owner/repo"]

    def test_max_query_variants_isolated_in_cache_key(self, monkeypatch, tmp_path):
        engine = Josty(backends=("test",), cache_db=tmp_path / "c.db", enable_cache=True)
        calls = {"n": 0}
        async def parts(query, *, sites, mode, limit, category, **kw):
            calls["n"] += 1
            res = [[result("https://example.com/x")]]
            return res, [ProviderStatus("test", query, True, 1)], sites or []
        monkeypatch.setattr(engine, "_search_parts", parts)
        # Different max_query_variants -> different cache keys -> two network calls
        asyncio.run(engine.search_run("q", max_query_variants=2))
        asyncio.run(engine.search_run("q", max_query_variants=3))
        assert calls["n"] == 2

    def test_research_run_profile_override(self, monkeypatch, tmp_path):
        engine = Josty(profile="general", cache_db=tmp_path / "c.db")
        async def parts(query, **kwargs):
            return (
                [[result("https://huggingface.co/m", source="test")]],
                [ProviderStatus("test", query, True, 1)],
                [],
            )
        monkeypatch.setattr(engine, "_search_parts", parts)
        g = asyncio.run(engine.research_run("q", profile="general"))
        d = asyncio.run(engine.research_run("q", profile="dev"))
        # dev profile boosts huggingface.co to 1.3, general to 1.0
        assert d.results[0].score > g.results[0].score

    def test_research_run_invalid_profile(self, monkeypatch, tmp_path):
        engine = Josty(cache_db=tmp_path / "c.db")
        with pytest.raises(ValueError, match="profile"):
            asyncio.run(engine.research_run("q", profile="bogus"))

    def test_research_run_invalid_max_query_variants(self, monkeypatch, tmp_path):
        engine = Josty(cache_db=tmp_path / "c.db")
        with pytest.raises(ValueError, match="positive"):
            asyncio.run(engine.research_run("q", max_query_variants=0))


# ======================================================================================
# SECTION 14b: Query relaxation behavior
# ======================================================================================

class TestQueryRelaxation:
    """Tests for the new query relaxation feature.

    Per the recent 'decoupled circuit breaking' refactor:
    - When 0 results come back and the query has 3+ words, the engine retries
      with a relaxed query (drop quotes or drop last word).
    - The original query's providers are preserved (extended with the relaxed
      ones), and the relaxed query's results are returned if non-empty.
    """

    def test_relaxation_skipped_for_short_queries(self, monkeypatch):
        engine = Josty(backends=("test",), cache_db=None)
        # 1-2 word query: no relaxation
        async def parts(query, **kwargs):
            return [[]], [ProviderStatus("test", query, True, 0)], []
        monkeypatch.setattr(engine, "_search_parts", parts)
        run = asyncio.run(engine.search_run("ab", limit=5))
        assert run.results == []

    def test_relaxation_triggers_on_3plus_words_no_results(self, monkeypatch):
        engine = Josty(backends=("test",), cache_db=None)
        # First call returns 0 results, second (relaxed) returns a hit
        call_count = {"n": 0}

        async def parts(query, **kwargs):
            call_count["n"] += 1
            if query == "alpha beta gamma":
                return [[]], [ProviderStatus("test", query, True, 0)], []
            # Relaxed query: "alpha beta"
            return [[result("https://example.com/x")]], [ProviderStatus("test", query, True, 1)], []

        monkeypatch.setattr(engine, "_search_parts", parts)
        run = asyncio.run(engine.search_run("alpha beta gamma", limit=5))
        # 2 calls: original + relaxation
        assert call_count["n"] == 2
        # Final results come from the relaxed query
        assert run.results[0].url == "https://example.com/x"

    def test_relaxation_does_not_trigger_on_results(self, monkeypatch):
        engine = Josty(backends=("test",), cache_db=None)
        call_count = {"n": 0}

        async def parts(query, **kwargs):
            call_count["n"] += 1
            return [[result("https://example.com/x")]], [ProviderStatus("test", query, True, 1)], []

        monkeypatch.setattr(engine, "_search_parts", parts)
        run = asyncio.run(engine.search_run("alpha beta gamma", limit=5))
        # Only 1 call: results came back, no relaxation needed
        assert call_count["n"] == 1
        assert run.results[0].url == "https://example.com/x"

    def test_relaxation_drop_last_word(self, monkeypatch):
        engine = Josty(backends=("test",), cache_db=None)
        seen_queries = []

        async def parts(query, **kwargs):
            seen_queries.append(query)
            if query == "alpha beta gamma":
                return [[]], [ProviderStatus("test", query, True, 0)], []
            if query == "alpha beta":  # relaxed
                res = [[result("https://example.com/x")]]
                return res, [ProviderStatus("test", query, True, 1)], []
            return [[]], [ProviderStatus("test", query, True, 0)], []

        monkeypatch.setattr(engine, "_search_parts", parts)
        asyncio.run(engine.search_run("alpha beta gamma", limit=5))
        # The relaxation is: drop the last word
        assert "alpha beta" in seen_queries
        assert "alpha beta gamma" in seen_queries

    def test_relaxation_drop_quotes(self, monkeypatch):
        engine = Josty(backends=("test",), cache_db=None)
        seen_queries = []

        async def parts(query, **kwargs):
            seen_queries.append(query)
            if '"' in query:
                return [[]], [ProviderStatus("test", query, True, 0)], []
            return [[result("https://example.com/x")]], [ProviderStatus("test", query, True, 1)], []

        monkeypatch.setattr(engine, "_search_parts", parts)
        asyncio.run(engine.search_run('"alpha beta gamma"', limit=5))
        # Quotes dropped in relaxed query
        assert any('"' not in q for q in seen_queries)

    def test_relaxation_appends_providers(self, monkeypatch):
        engine = Josty(backends=("test",), cache_db=None)

        async def parts(query, **kwargs):
            return [[]], [ProviderStatus("test", query, True, 0)], []

        monkeypatch.setattr(engine, "_search_parts", parts)
        run = asyncio.run(engine.search_run("alpha beta gamma", limit=5))
        # Both original and relaxed queries contribute providers
        # Original: 1 provider (one backend). Relaxed: 1 provider. Total 2.
        assert len(run.providers) == 2

    def test_relaxation_gives_up_if_relaxed_also_empty(self, monkeypatch):
        engine = Josty(backends=("test",), cache_db=None)

        async def parts(query, **kwargs):
            return [[]], [ProviderStatus("test", query, True, 0)], []

        monkeypatch.setattr(engine, "_search_parts", parts)
        run = asyncio.run(engine.search_run("alpha beta gamma", limit=5))
        # No results even after relaxation
        assert run.results == []


# ======================================================================================
# SECTION 15: diagnose_run behavior
# ======================================================================================

class TestDiagnose:

    def test_unknown_backend_reported_as_unknown(self, monkeypatch):
        async def fail(self, url, **kw):
            raise AssertionError("should not be called")
        monkeypatch.setattr(httpx.AsyncClient, "get", fail)
        d = asyncio.run(Josty(backends=("mystery",)).diagnose_run()).dict()
        entry = d["providers"][0]
        assert entry["provider"] == "mystery"
        assert entry["ok"] is False
        assert entry["error_kind"] == "unknown"
        assert d["status"] == "failed"

    def test_diagnose_with_github_uses_api_host(self, monkeypatch):
        seen = []
        async def fake_get(self, url, **kw):
            seen.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        d = asyncio.run(Josty().diagnose_run(include_github=True)).dict()
        assert "https://api.github.com/" in seen
        assert any(p["provider"] == "github-api" for p in d["providers"])

    def test_diagnose_status_complete(self, monkeypatch):
        # All reachable -> complete
        async def ok(self, url, **kw):
            return httpx.Response(200, request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.AsyncClient, "get", ok)
        d = asyncio.run(Josty(backends=("bing,brave",)).diagnose_run()).dict()
        assert d["status"] == "complete"
        assert d["reachable"] == 2

    def test_diagnose_partial_status(self, monkeypatch):
        # Inject failure for one provider
        async def gate(self, url, **kw):
            if "www.bing.com" in url:
                raise httpx.ConnectError("refused", request=httpx.Request("GET", url))
            return httpx.Response(200, request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.AsyncClient, "get", gate)
        d = asyncio.run(Josty(backends=("bing,brave",)).diagnose_run()).dict()
        assert d["status"] in ("degraded", "failed")
        # reachable < total
        assert d["reachable"] < d["count"]

    def test_diagnose_does_not_probe_github_by_default(self, monkeypatch):
        seen = []
        async def fake_get(self, url, **kw):
            seen.append(url)
            return httpx.Response(200, request=httpx.Request("GET", url))
        monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
        asyncio.run(Josty().diagnose_run())
        assert not any(urlparse(u).hostname == "api.github.com" for u in seen)


# ======================================================================================
# SECTION 16: ad redirect detection
# ======================================================================================

class TestAdRedirect:
    def test_google_aclick(self):
        assert Josty._is_ad_redirect("https://www.google.com/aclick?id=1")
        assert Josty._is_ad_redirect("https://google.com/aclick?x=y")

    def test_bing_ck(self):
        assert Josty._is_ad_redirect("https://bing.com/ck/foo")
        assert Josty._is_ad_redirect("https://www.bing.com/ck/a?b=1")

    def test_doubleclick(self):
        assert Josty._is_ad_redirect("https://ad.doubleclick.net/p")
        assert Josty._is_ad_redirect("https://doubleclick.net/anything")

    def test_googleadservices(self):
        assert Josty._is_ad_redirect("https://googleadservices.com/pagead")

    def test_subdomain_match_for_doubleclick(self):
        # Subdomain of doubleclick.net
        assert Josty._is_ad_redirect("https://x.y.doubleclick.net/p")

    def test_legitimate_url_not_flagged(self):
        assert not Josty._is_ad_redirect("https://example.com/article")
        assert not Josty._is_ad_redirect("https://notgoogle.com/aclick")
        assert not Josty._is_ad_redirect("https://google.com/about")

    def test_garbage_url_returns_false(self):
        # urlsplit may raise on some inputs; the function catches ValueError.
        assert Josty._is_ad_redirect("not a url") is False


# ======================================================================================
# SECTION 17: clone + merge helpers
# ======================================================================================

class TestMergeResult:
    def test_merge_combines_sources(self):
        a = SearchResult("t", "https://example.com/x", sources=["a"])
        b = SearchResult("t2", "https://example.com/x", sources=["b"])
        from josty.engine import _merge_result
        _merge_result(a, b)
        assert a.sources == ["a", "b"]

    def test_merge_keeps_longer_snippet(self):
        a = SearchResult("t", "https://example.com/x", snippet="short")
        b = SearchResult("t2", "https://example.com/x", snippet="longer snippet text")
        from josty.engine import _merge_result
        _merge_result(a, b)
        assert a.snippet == "longer snippet text"
        assert a.title == "t2"

    def test_merge_keeps_first_published_at(self):
        a = SearchResult("t", "https://example.com/x", published_at="2025-01-01")
        b = SearchResult("t2", "https://example.com/x", published_at="2026-01-01")
        from josty.engine import _merge_result
        _merge_result(a, b)
        # First non-None wins
        assert a.published_at == "2025-01-01"


# ======================================================================================
# SECTION 18: search_run_from_dict round-trip
# ======================================================================================

class TestRoundTrip:
    def test_search_run_roundtrip(self):
        original = SearchRun(
            query="test",
            results=[
                SearchResult(
                    title="Title",
                    url="https://example.com/x?utm_source=z",
                    snippet="Snippet text",
                    sources=["bing", "brave"],
                    score=0.016393,
                )
            ],
            providers=[ProviderStatus("bing,brave", "test", True, 1)],
        )
        from josty.engine import _search_run_from_dict
        restored = _search_run_from_dict(original.dict())
        assert restored.query == original.query
        assert len(restored.results) == 1
        assert restored.results[0].title == "Title"
        assert restored.results[0].url == "https://example.com/x?utm_source=z"
        assert restored.results[0].score == 0.016393
        assert restored.results[0].snippet == "Snippet text"
        assert restored.providers[0].ok is True


# ======================================================================================
# SECTION 19: extraction fallback
# ======================================================================================

class TestExtraction:
    def test_trafilatura_used_when_available(self):
        # Real trafilatura is installed; verify that simple HTML yields non-empty
        # markdown content and method is "trafilatura"
        content, method = Josty._extract(
            "<html><body><h1>Real title</h1><p>Real paragraph with content.</p></body></html>",
            "https://example.com",
        )
        assert method == "trafilatura"
        assert content  # non-empty

    def test_fallback_when_trafilatura_fails(self, monkeypatch):
        import sys as _sys
        monkeypatch.setitem(_sys.modules, "trafilatura",
                             SimpleNamespace(extract=lambda *a, **k: None))
        content, method = Josty._extract(
            "<html><script>ignore()</script><main>Hello world</main></html>",
            "https://example.com",
        )
        assert method == "html-text-fallback"
        # script content removed, main content kept
        assert "Hello world" in content
        assert "ignore" not in content

    def test_fallback_strips_multiple_tags(self, monkeypatch):
        import sys as _sys
        monkeypatch.setitem(_sys.modules, "trafilatura",
                             SimpleNamespace(extract=lambda *a, **k: None))
        content, method = Josty._extract(
            "<html><script>x</script><style>y</style><noscript>z</noscript>"
            "<p>Visible</p></html>",
            "https://example.com",
        )
        assert method == "html-text-fallback"
        assert "Visible" in content
        # All blocked tags removed
        for hidden in ("x", "y", "z"):
            assert hidden not in content


# ======================================================================================
# SECTION 20: Josty constructor validation
# ======================================================================================

class TestConstructorValidation:
    def test_zero_timeout_rejected(self):
        with pytest.raises(ValueError):
            Josty(timeout=0)

    def test_negative_concurrency_rejected(self):
        with pytest.raises(ValueError):
            Josty(max_search_concurrency=0)
        with pytest.raises(ValueError):
            Josty(max_fetch_concurrency=0)

    def test_zero_download_bytes_rejected(self):
        with pytest.raises(ValueError):
            Josty(max_download_bytes=0)

    def test_negative_content_chars_rejected(self):
        with pytest.raises(ValueError):
            Josty(max_content_chars=-1)

    def test_zero_query_variants_rejected(self):
        with pytest.raises(ValueError):
            Josty(max_query_variants=0)

    def test_unsupported_profile_rejected(self):
        with pytest.raises(ValueError):
            Josty(profile="bogus")

    def test_zero_breaker_thresholds_rejected(self):
        with pytest.raises(ValueError):
            Josty(breaker_fail_threshold=0)
        with pytest.raises(ValueError):
            Josty(breaker_window_seconds=0)
        with pytest.raises(ValueError):
            Josty(breaker_cool_down_seconds=0)

    def test_max_concurrency_alias_overrides_search(self):
        engine = Josty(max_concurrency=3)
        assert engine.max_search_concurrency == 3
        assert engine.max_fetch_concurrency == Josty.DEFAULT_FETCH_CONCURRENCY

    def test_independent_semaphores(self):
        engine = Josty(max_search_concurrency=2, max_fetch_concurrency=3)
        assert engine._search_semaphore() is not engine._fetch_semaphore()


# ======================================================================================
# SECTION 21: search_run parameter validation
# ======================================================================================

class TestSearchRunValidation:
    def test_limit_must_be_in_range(self, monkeypatch):
        with pytest.raises(ValueError):
            asyncio.run(Josty().search_run("q", limit=0))
        with pytest.raises(ValueError):
            asyncio.run(Josty().search_run("q", limit=101))

    def test_invalid_category_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="category"):
            asyncio.run(Josty().search_run("q", category="bogus"))

    def test_invalid_safesearch_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="safe-search"):
            asyncio.run(Josty().search_run("q", safesearch="bogus"))

    def test_invalid_timelimit_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="time limit"):
            asyncio.run(Josty().search_run("q", timelimit="bogus"))

    def test_invalid_max_query_variants_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="positive"):
            asyncio.run(Josty().search_run("q", max_query_variants=0))
