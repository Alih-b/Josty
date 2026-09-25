"""Unknown-edge probes for josty 0.6.1.

This suite targets boundaries the existing suites do not cover. It was written
black-box: the probes assert the documented contract, and where they expose a
defect they are marked xfail(strict=True) so they fail until the behavior is
made correct (at which point the strict marker turns green and forces a review).

Conventions
-----------
* xfail(strict=True) tests assert the CORRECT behavior and currently fail.
  They are defects, not preferences.
* Passing tests pin current behavior that is surprising but arguably by design,
  with a FINDING comment describing what was observed and the risk.

Run: pytest tests/test_unknown_edges.py -q
"""

from __future__ import annotations

import random

import pytest

from josty.engine import Josty
from josty.models import ProviderStatus, SearchResult, SearchRun
from josty.ranking import _site_matches, canonical, domain_weight, normalize_sites, rrf


@pytest.fixture(autouse=True)
def isolate_test_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))


def result(url, snippet="", source="test", **kwargs):
    return SearchResult("title", url, snippet, sources=[source], **kwargs)


# ======================================================================================
# 1. Trailing-dot hostnames (RFC 1034 absolute FQDN notation)
# ======================================================================================

class TestTrailingDotHostnames:
    """A host written pinterest.com. is the same DNS name as pinterest.com.

    josty lowercases and strips www. in domain_weight()/canonical(), but never
    strips a trailing root dot. That escapes the spam penalty AND the authority
    boost, and yields a second canonical key for the same page.
    """

    def test_trailing_dot_and_apex_are_distinct_canonical_keys(self):
        # FINDING: same DNS name, two canonical keys. RRF fusion and the search
        # cache treat them as different pages, and a page can occupy two result
        # slots with a trailing-dot/non-dot pair from two engines.
        assert canonical("https://example.com/a") != canonical("https://example.com./a")

    @pytest.mark.xfail(strict=True, reason="domain_weight does not strip a trailing root dot")
    def test_spam_penalty_applies_to_trailing_dot_host(self):
        assert domain_weight("https://pinterest.com./x") == 0.6
        assert domain_weight("https://geeksforgeeks.org./x", profile="dev") == 0.5

    @pytest.mark.xfail(strict=True, reason="domain_weight does not strip a trailing root dot")
    def test_authority_boost_applies_to_trailing_dot_host(self):
        assert domain_weight("https://github.com./a") == 1.2
        assert domain_weight("https://arxiv.org./a", profile="academic") == 1.4

    @pytest.mark.xfail(strict=True, reason="canonical does not normalize a trailing root dot")
    def test_trailing_dot_canonicalizes_identically(self):
        assert canonical("https://example.com./a") == canonical("https://example.com/a")

    def test_current_trailing_dot_weight_is_flat(self):
        # Pins the defect's observable signature: both spam and authority flat at 1.0.
        assert domain_weight("https://pinterest.com./x") == 1.0
        assert domain_weight("https://github.com./x") == 1.0
        assert domain_weight("https://arxiv.org./x", profile="academic") == 1.0

    def test_site_filter_drops_trailing_dot_result(self):
        # FINDING: a --site filter removes the very page it names when the SERP
        # returns the absolute form. This is a false negative, not a bypass.
        assert _site_matches("https://example.com./x", ["example.com"]) is False


# ======================================================================================
# 2. Constructor guards
# ======================================================================================

class TestConstructorGuards:
    """Validation gaps in Josty.__init__ that the existing tests do not exercise."""

    @pytest.mark.xfail(strict=True, reason="timeout NaN compares false against <= 0")
    def test_nan_timeout_rejected(self):
        with pytest.raises(ValueError):
            Josty(timeout=float("nan"), enable_cache=False)

    def test_nan_and_inf_timeout_accepted_today(self):
        # FINDING: nan/inf pass the timeout <= 0 guard. inf disables the outer
        # wait_for bound; nan makes every timeout comparison false.
        assert Josty(timeout=float("inf"), enable_cache=False).timeout == float("inf")

    def test_string_backends_fan_out_per_character(self):
        # FINDING (BUG): the type hint says tuple[str, ...] but a string is
        # accepted and iterated character by character, silently querying
        # engines "b", "r", "a", ... and reporting status=failed.
        engine = Josty(backends="brave,duckduckgo", enable_cache=False)
        per_char = ["b", "r", "a", "v", "e", "d", "u", "c", "k", "g", "o"]
        assert engine._engine_names("text") == per_char

    def test_explicit_empty_text_backends_fall_back_but_empty_news_disables(self):
        # FINDING (inconsistent): backends=[] is falsy, so text silently
        # reverts to DEFAULT_BACKENDS while the news expression keeps the empty
        # tuple. The same literal means "defaults" for text and "nothing" for news.
        engine = Josty(backends=[], enable_cache=False)
        assert engine.backends == Josty.DEFAULT_BACKENDS
        assert engine.news_backends == []

    def test_empty_news_backends_means_defaults(self):
        # FINDING (inconsistent): news_backends=[] is falsy and silently reverts
        # to DEFAULT_NEWS_BACKENDS, unlike backends=[].
        engine = Josty(news_backends=[], enable_cache=False)
        assert engine.news_backends == Josty.DEFAULT_NEWS_BACKENDS

    def test_custom_text_backends_also_become_news_backends(self):
        # FINDING: passing text backends also replaces the news backends, so a
        # caller who customizes text silently loses the news engine set.
        engine = Josty(backends=("google", "mojeek"), enable_cache=False)
        assert engine.news_backends == ("google", "mojeek")


# ======================================================================================
# 3. Site-filter granularity
# ======================================================================================

class TestSiteFilterGranularity:
    """normalize_sites accepts a single-label suffix, which matches a whole TLD."""

    def test_single_label_site_matches_entire_tld(self):
        # FINDING: --site com is accepted and post-filters every .com host,
        # because _site_matches treats the filter as a domain suffix. A TLD is
        # not a hostname; an agent/user passing a bare label gets a far wider
        # filter than intended.
        sites = normalize_sites(["com"])
        assert _site_matches("https://anything.com/x", sites)
        assert _site_matches("https://evil.com/y", sites)

    def test_site_filter_still_respects_label_boundary(self):
        # The suffix guard is correct for real multi-label hosts: no sibling leak.
        assert _site_matches("https://notexample.com/x", ["example.com"]) is False
        assert _site_matches("https://a.example.com/x", ["example.com"]) is True


# ======================================================================================
# 4. Deterministic property probes
# ======================================================================================

class TestPropertyProbes:
    """Seeded randomized invariants, fast and hermetic."""

    ALPHABET = "abcABC012:/?#[]@!$&'()*+,;=%.-_ ~"

    def _random(self, rng, n=30):
        return "".join(rng.choice(self.ALPHABET) for _ in range(rng.randint(0, n)))

    def test_canonical_is_idempotent_or_rejects(self):
        rng = random.Random(20260920)
        schemes = ["http", "https", "ftp", "file", ""]
        hosts = ["example.com", "pinterest.com.", "github.com", "sub.example.com",
                 "127.0.0.1", "[::1]", "xn--x.com", "example.com:8080", "user@example.com"]
        paths = ["", "/", "/a", "/a/b/", "/%7E", "/a b", "//x"]
        queries = ["", "?a=1", "?utm_source=x&b=2", "?x=", "?a=%2B&b=+"]
        checked = 0
        for _ in range(3000):
            url = (
                f"{rng.choice(schemes)}://{rng.choice(hosts)}"
                f"{rng.choice(paths)}{rng.choice(queries)}"
            )
            # A malformed string must be rejected with ValueError, never anything else.
            try:
                once = canonical(url)
            except ValueError:
                continue
            assert canonical(once) == once
            checked += 1
        assert checked > 100  # the structured corpus mostly canonicalizes

    def test_rrf_score_reconstructs_from_attribution(self):
        for profile in ("general", "dev", "academic"):
            lists = [
                [
                    result(f"https://host{i}.example.com/p{j}", source=f"e{k}")
                    for j in range(3)
                ]
                for i in range(2)
                for k in range(2)
            ]
            for item in rrf(lists, k=60, profile=profile):
                expected = round(
                    item.score_weights["domain_weight"]
                    * sum(item.rank_contributions.values()),
                    6,
                )
                assert item.score == expected
                assert item.engine_ranks  # every fused item has a discovery rank

    def test_rrf_does_not_mutate_caller_items(self):
        original = result("https://example.com/a", "s", source="brave")
        snapshot = (list(original.sources), dict(original.engine_ranks), original.score)
        rrf([[original]])
        assert (original.sources, original.engine_ranks, original.score) == snapshot

    def test_search_run_dict_roundtrip_is_lossless(self):
        from josty.cache import _search_run_from_dict

        run = SearchRun(
            query="q",
            run_at="2026-01-01T00:00:00+00:00",
            query_variant_count=3,
            request_count=18,
            results=[result("https://ex.com/a", source="brave", score=0.5)],
            providers=[
                ProviderStatus(
                    "brave", "q", True, 1, latency_ms=3.5,
                    circuit_state="closed", failures=0, backoff_remaining=0.0,
                )
            ],
        )
        assert _search_run_from_dict(run.dict()).dict() == run.dict()

    def test_normalize_sites_accepted_values_are_stable(self):
        rng = random.Random(99)
        for _ in range(3000):
            raw = self._random(rng, 20)
            try:
                accepted = normalize_sites([raw])
            except ValueError:
                continue
            assert normalize_sites(accepted) == accepted
