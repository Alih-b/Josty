"""Fixed factual queries with hand-curated graded relevance.

Each query has:
  id        : stable slug
  query     : query string
  category  : factual | navigational | repository | technical
  answers   : list of strings that *answer* the question. A result containing
              any of these in title+snippet is a hit, even if it's not the
              canonical page.
  canonical : list of canonical answer URLs (graded rel=3). May be empty if
              the question is "X is a person/project, find their page."
  notes     : free text for the report.

The graded relevance used at scoring time:
   3  : URL is in `canonical`
   2  : title or snippet contains any string in `answers`
   1  : title or snippet mentions the subject without the answer
        (heuristic: subject tokens appear together in a result)
   0  : irrelevant / off-topic
   -1 : wrong answer (e.g. wrong year) — explicitly forbidden
"""

from __future__ import annotations

QUERIES: list[dict] = [
    # ----- Factual, long-stable -----
    {
        "id": "python_release",
        "query": "Python latest stable release version",
        "category": "factual",
        "answers": ["3.13", "3.14"],
        "canonical": [
            "https://www.python.org/downloads/",
            "https://en.wikipedia.org/wiki/python",
            "https://docs.python.org/3/whatsnew/",
        ],
        "notes": "Stable line as of 2026. Tolerates 3.13/3.14.",
    },
    {
        "id": "rust_foundation",
        "query": "Rust programming language owner foundation",
        "category": "factual",
        "answers": ["rust foundation", "rust-lang"],
        "canonical": [
            "https://foundation.rust-lang.org/",
            "https://www.rust-lang.org/",
            "https://en.wikipedia.org/wiki/rust_(programming_language)",
        ],
        "notes": "Owner of the trademark/governance.",
    },
    {
        "id": "linux_kernel_year",
        "query": "Linux kernel first release year",
        "category": "factual",
        "answers": ["1991"],
        "canonical": [
            "https://en.wikipedia.org/wiki/linux_kernel",
            "https://www.kernel.org/",
        ],
        "notes": "Initial release September 1991.",
    },
    {
        "id": "http_spec",
        "query": "HTTP protocol origin CERN specification",
        "category": "factual",
        "answers": ["tim berners-lee", "berners-lee"],
        "canonical": [
            "https://en.wikipedia.org/wiki/http",
            "https://www.w3.org/Protocols/",
        ],
        "notes": "Original 1989/1991 spec.",
    },
    {
        "id": "json_author",
        "query": "JSON specification author origin",
        "category": "factual",
        "answers": ["crockford", "douglas crockford"],
        "canonical": [
            "https://en.wikipedia.org/wiki/json",
            "https://www.json.org/json-en.html",
        ],
        "notes": "",
    },
    {
        "id": "git_author",
        "query": "Git version control system original author creator",
        "category": "factual",
        "answers": ["linus torvalds", "torvalds"],
        "canonical": [
            "https://en.wikipedia.org/wiki/git",
            "https://git-scm.com/",
        ],
        "notes": "BitKeeper replacement, 2005.",
    },
    {
        "id": "linux_author",
        "query": "Linux kernel author creator",
        "category": "factual",
        "answers": ["linus torvalds", "torvalds"],
        "canonical": [
            "https://en.wikipedia.org/wiki/linux",
            "https://www.linuxfoundation.org/",
        ],
        "notes": "1991.",
    },
    {
        "id": "openssl_author",
        "query": "OpenSSL cryptographic library origin author",
        "category": "factual",
        "answers": ["openssl", "eric young", "openssl project"],
        "canonical": [
            "https://en.wikipedia.org/wiki/openssl",
            "https://www.openssl.org/",
        ],
        "notes": "Project page or canonical wikipedia article.",
    },
    {
        "id": "asyncio_history",
        "query": "Python asyncio origin PEP",
        "category": "factual",
        "answers": ["asyncio", "pep 3156", "guido van rossum"],
        "canonical": [
            "https://peps.python.org/pep-3156/",
            "https://docs.python.org/3/library/asyncio.html",
        ],
        "notes": "PEP 3156 by Guido van Rossum.",
    },
    {
        "id": "rrf_paper",
        "query": "Reciprocal Rank Fusion original paper authors",
        "category": "factual",
        "answers": ["cormack", "clarke", "rrf"],
        "canonical": [
            "https://dl.acm.org/doi/10.1145/1571941.1571943",
            "https://en.wikipedia.org/wiki/reciprocal_rank_fusion",
        ],
        "notes": "Cormack, Clarke, Buettcher, Butterfield 2009.",
    },

    # ----- Repository / project lookup -----
    {
        "id": "ddgs_repo",
        "query": "deedy5 ddgs python search package",
        "category": "repository",
        "answers": ["deedy5", "ddgs"],
        "canonical": [
            "https://github.com/deedy5/ddgs",
            "https://pypi.org/project/ddgs/",
        ],
        "notes": "",
    },
    {
        "id": "trafilatura_repo",
        "query": "trafilatura python web text extraction",
        "category": "repository",
        "answers": ["trafilatura", "adbar"],
        "canonical": [
            "https://github.com/adbar/trafilatura",
            "https://trafilatura.readthedocs.io/",
        ],
        "notes": "",
    },
    {
        "id": "fastapi_repo",
        "query": "fastapi python web framework author",
        "category": "repository",
        "answers": ["fastapi", "tiangolo", "sebastián ramírez"],
        "canonical": [
            "https://github.com/tiangolo/fastapi",
            "https://fastapi.tiangolo.com/",
        ],
        "notes": "",
    },
    {
        "id": "httpx_repo",
        "query": "httpx python http client library encode",
        "category": "repository",
        "answers": ["httpx", "encode"],
        "canonical": [
            "https://github.com/encode/httpx",
            "https://www.python-httpx.org/",
        ],
        "notes": "",
    },
    {
        "id": "ruff_repo",
        "query": "ruff python linter astral",
        "category": "repository",
        "answers": ["ruff", "astral", "charlie marsh"],
        "canonical": [
            "https://github.com/astral-sh/ruff",
            "https://docs.astral.sh/ruff/",
        ],
        "notes": "",
    },

    # ----- Technical / spec -----
    {
        "id": "sqlite_author",
        "query": "SQLite embedded database author creator",
        "category": "technical",
        "answers": ["hipp", "sqlite", "richard hipp"],
        "canonical": [
            "https://en.wikipedia.org/wiki/sqlite",
            "https://www.sqlite.org/",
        ],
        "notes": "D. Richard Hipp.",
    },
    {
        "id": "unicode_consortium",
        "query": "Unicode Consortium standard governing body",
        "category": "technical",
        "answers": ["unicode", "consortium"],
        "canonical": [
            "https://en.wikipedia.org/wiki/unicode_consortium",
            "https://home.unicode.org/",
        ],
        "notes": "",
    },
    {
        "id": "tls_author_origin",
        "query": "TLS transport layer security origin history",
        "category": "technical",
        "answers": ["tls", "transport layer security", "ietf"],
        "canonical": [
            "https://en.wikipedia.org/wiki/transport_layer_security",
        ],
        "notes": "Successor to SSL, standardized by IETF.",
    },

    # ----- Navigational (find the project home page) -----
    {
        "id": "ddg_homepage",
        "query": "DuckDuckGo privacy search engine homepage",
        "category": "navigational",
        "answers": ["duckduckgo"],
        "canonical": ["https://duckduckgo.com/"],
        "notes": "Pure nav query.",
    },
    {
        "id": "github_homepage",
        "query": "GitHub official website",
        "category": "navigational",
        "answers": ["github"],
        "canonical": ["https://github.com/"],
        "notes": "Pure nav query.",
    },
]
