"""URL canonicalization, domain weighting, and RRF fusion."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import SearchResult
from .status import MAX_SITES, ProfileType

TRACKING_QUERY_KEYS = {
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "msclkid",
}

AUTHORITATIVE_DOMAINS_GENERAL = {
    "github.com",
    "gitlab.com",
    "stackoverflow.com",
    "superuser.com",
    "serverfault.com",
    "developer.mozilla.org",
    "wikipedia.org",
    "python.org",
    "pypi.org",
    "rust-lang.org",
    "crates.io",
    "go.dev",
    "golang.org",
    "archlinux.org",
    "kernel.org",
    "w3.org",
    "ietf.org",
}

AUTHORITATIVE_DOMAINS_DEV = {
    "github.com",
    "github.io",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "stackoverflow.com",
    "superuser.com",
    "serverfault.com",
    "developer.mozilla.org",
    "python.org",
    "pypi.org",
    "rust-lang.org",
    "crates.io",
    "go.dev",
    "golang.org",
    "pkg.go.dev",
    "npmjs.com",
    "rubygems.org",
    "packagist.org",
    "nuget.org",
    "archlinux.org",
    "kernel.org",
    "w3.org",
    "ietf.org",
    "man7.org",
    "react.dev",
    "reactjs.org",
    "vuejs.org",
    "angular.dev",
    "angular.io",
    "svelte.dev",
    "nextjs.org",
    "djangoproject.com",
    "rubyonrails.org",
    "fastapi.tiangolo.com",
    "flask.palletsprojects.com",
    "spring.io",
    "docker.com",
    "kubernetes.io",
    "apache.org",
    "postgresql.org",
    "sqlite.org",
    "redis.io",
    "mongodb.com",
    "linux.die.net",
    # Modern AI/ML & LLM Frameworks & Hubs
    "huggingface.co",
    "hf.co",
    "pytorch.org",
    "tensorflow.org",
    "keras.io",
    "paperswithcode.com",
    "kaggle.com",
    "ollama.com",
    "vllm.ai",
    "unsloth.ai",
    "modal.com",
    "triton-lang.org",
    "qdrant.tech",
    "milvus.io",
    "weaviate.io",
    # Modern Language Toolchains, Web & Cloud
    "astral.sh",
    "bun.sh",
    "deno.com",
    "deno.land",
    "ziglang.org",
    "biomejs.dev",
    "tailwindcss.com",
    "shadcn.com",
    "prisma.io",
    "supabase.com",
    "trpc.io",
    "cloudflare.com",
    "tailscale.com",
    "fly.io",
    "val.town",
}

AUTHORITATIVE_DOMAINS_ACADEMIC = {
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "ncbi.nlm.nih.gov",
    "nih.gov",
    "nlm.nih.gov",
    "ieee.org",
    "ieeexplore.ieee.org",
    "acm.org",
    "dl.acm.org",
    "nature.com",
    "science.org",
    "springer.com",
    "sciencedirect.com",
    "semanticscholar.org",
    "openalex.org",
    "doi.org",
    "crossref.org",
    "jstor.org",
    "plos.org",
    "cell.com",
    "oup.com",
    "tandfonline.com",
    "wiley.com",
    "frontiersin.org",
    "mdpi.com",
    "pnas.org",
    "cambridge.org",
    "thelancet.com",
    # Top AI/ML Conferences & Preprints
    "openreview.net",
    "paperswithcode.com",
    "chemrxiv.org",
    "hal.science",
    "aclweb.org",
    "neurips.cc",
    "icml.cc",
    "iclr.cc",
}

SPAM_DOMAINS = {
    "pinterest.com",
    "quora.com",
    "softonic.com",
    "ehow.com",
    "geeksforgeeks.org",
    "experts-exchange.com",
}


def canonical(url: str) -> str:
    """Normalize URL variants while preserving resource-identifying query parameters."""
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not hostname or parsed.username or parsed.password:
        raise ValueError("canonical URLs must be public-style HTTP(S) URLs")
    port = parsed.port
    hostname = f"[{hostname}]" if ":" in hostname else hostname
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        hostname = f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS
        ],
        doseq=True,
    )
    return urlunsplit((scheme, hostname, path, query, ""))


def _clone(item: SearchResult) -> SearchResult:
    return replace(
        item,
        sources=list(item.sources),
        engine_ranks=dict(item.engine_ranks),
        rank_contributions=dict(item.rank_contributions),
        score_weights=dict(item.score_weights),
    )


def _merge_result(current: SearchResult, candidate: SearchResult) -> None:
    current.sources = list(dict.fromkeys([*current.sources, *candidate.sources]))
    if len(candidate.snippet) > len(current.snippet):
        current.snippet = candidate.snippet
        current.title = candidate.title or current.title
    current.published_at = current.published_at or candidate.published_at
    current.publisher = current.publisher or candidate.publisher
    # Only engine_ranks merge here; rank_contributions and score_weights are
    # derived from engine_ranks (and the profile weight) inside rrf().
    for engine, rank in candidate.engine_ranks.items():
        if engine not in current.engine_ranks or rank < current.engine_ranks[engine]:
            current.engine_ranks[engine] = rank


def domain_weight(url: str, profile: ProfileType = "general") -> float:
    """Return ranking multiplier for authoritative vs spam domains based on profile."""
    try:
        hostname = (urlsplit(url).hostname or "").lower()
    except Exception:
        return 1.0
    if hostname.startswith("www."):
        hostname = hostname[4:]
    if not hostname:
        return 1.0

    def _matches_any(domains: set[str]) -> bool:
        return any(hostname == d or hostname.endswith("." + d) for d in domains)

    if _matches_any(SPAM_DOMAINS):
        return 0.5 if profile in ("dev", "academic") else 0.6

    if profile == "academic":
        if _matches_any(AUTHORITATIVE_DOMAINS_ACADEMIC):
            return 1.4
        if (
            hostname.startswith("docs.")
            or hostname.endswith(".readthedocs.io")
            or _matches_any(AUTHORITATIVE_DOMAINS_GENERAL)
        ):
            return 1.2
        return 1.0

    if profile == "dev":
        if (
            hostname.startswith("docs.")
            or hostname.endswith(".readthedocs.io")
            or _matches_any(AUTHORITATIVE_DOMAINS_DEV)
        ):
            return 1.3
        if _matches_any(AUTHORITATIVE_DOMAINS_GENERAL):
            return 1.2
        return 1.0

    # general (default)
    if (
        hostname.startswith("docs.")
        or hostname.endswith(".readthedocs.io")
        or _matches_any(AUTHORITATIVE_DOMAINS_GENERAL)
    ):
        return 1.2

    return 1.0


def _with_discovery_ranks(item: SearchResult, rank: int) -> SearchResult:
    """Clone ``item`` and fill missing ``engine_ranks`` on the clone only."""
    cloned = _clone(item)
    for source in cloned.sources:
        if source not in cloned.engine_ranks:
            cloned.engine_ranks[source] = rank
    return cloned


def rrf(
    ranked: list[list[SearchResult]],
    k: int = 60,
    profile: ProfileType = "general",
) -> list[SearchResult]:
    """Fuse independent backend-group ranked lists with Reciprocal Rank Fusion.

    Each engine contributes at most one vote per URL, taken from its 1-indexed
    discovery rank in ``engine_ranks`` (min-merged across lists). The fused
    score is ``round(domain_weight * sum(round(1/(k + rank_e), 6) for e), 6)``
    so a caller can verify the score from the recorded attribution alone.
    Engine agreement therefore counts: a URL found by two engines of the same
    group carries both votes. That is the deliberate per-engine fusion
    semantics (see PROJECT.md, "Transparent RRF Attribution Contract").

    Fusion never mutates caller-owned items: ranks are backfilled on clones.
    """
    if k < 1:
        raise ValueError("k must be positive")
    merged: dict[str, SearchResult] = {}

    for results in ranked:
        seen_in_list: set[str] = set()
        for rank, item in enumerate(results, 1):
            try:
                key = canonical(item.url)
            except ValueError:
                continue
            if not key or key in seen_in_list:
                continue
            seen_in_list.add(key)
            candidate = _with_discovery_ranks(item, rank)
            if key not in merged:
                merged[key] = candidate
            else:
                _merge_result(merged[key], candidate)

    for item in merged.values():
        w = domain_weight(item.url, profile=profile)
        item.score_weights = {"k": float(k), "domain_weight": w}
        item.rank_contributions = {
            engine: round(1.0 / (k + rank), 6) for engine, rank in item.engine_ranks.items()
        }
        item.score = round(w * sum(item.rank_contributions.values()), 6)

    return sorted(
        merged.values(),
        key=lambda item: (-item.score, canonical(item.url)),
    )


def merge_query_variants(ranked: list[list[SearchResult]]) -> list[SearchResult]:
    """Merge query rewrites for one backend without counting them as independent votes."""
    merged: dict[str, tuple[int, int, SearchResult]] = {}
    for variant_index, results in enumerate(ranked):
        for rank, item in enumerate(results, 1):
            try:
                key = canonical(item.url)
            except ValueError:
                continue
            candidate = _with_discovery_ranks(item, rank)
            current = merged.get(key)
            if current is None:
                merged[key] = (rank, variant_index, candidate)
                continue
            best_rank, best_variant, saved = current
            _merge_result(saved, candidate)
            merged[key] = (min(rank, best_rank), min(variant_index, best_variant), saved)
    return [
        item
        for _, _, item in sorted(
            merged.values(),
            key=lambda row: (row[0], row[1], canonical(row[2].url)),
        )
    ]


def normalize_sites(sites: list[str] | None) -> list[str]:
    if not sites:
        return []
    if len(sites) > MAX_SITES:
        raise ValueError(f"at most {MAX_SITES} site filters are allowed")
    normalized: list[str] = []
    for raw in sites:
        site = raw.strip().lower().rstrip(".")
        if site.startswith("www."):
            site = site[4:]
        labels = site.split(".")
        if (
            not site
            or "://" in site
            or "/" in site
            or ":" in site
            or not re.fullmatch(r"[a-z0-9.-]+", site)
            or any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in labels
            )
        ):
            raise ValueError(f"invalid site filter: {raw}")
        normalized.append(site)
    return list(dict.fromkeys(normalized))


def _site_matches(url: str, sites: list[str]) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return any(hostname == site or hostname.endswith(f".{site}") for site in sites)
