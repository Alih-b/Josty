"""Backend health classification.

Hermetic: the probe takes an injected fetcher, so every classification is
driven from HTML literals and nothing here touches the network.
"""

from __future__ import annotations

from josty.health import classify_page, probe_backend, run_health

CAPTCHA_PAGE = "<html><head><title>Captcha</title></head><body>are you a robot</body></html>"
RESULTS_PAGE = (
    "<html><head><title>python packaging at DuckDuckGo</title></head><body>"
    + "<a class=\"result__a\" href=\"https://a\">a</a>"
    + "<a class=\"result__a\" href=\"https://b\">b</a>"
    + "<a class=\"result__a\" href=\"https://c\">c</a>"
    + "</body></html>"
)
JS_PAGE = "<html><body><noscript>please enable javascript to continue</noscript></body></html>"
PLAIN_PAGE = "<html><body>nothing here at all</body></html>"


def test_challenge_page_is_challenged_not_empty():
    state, evidence = classify_page("mojeek", 200, CAPTCHA_PAGE)
    assert state == "challenged"
    assert "captcha" in evidence[0]


def test_result_markup_is_ok():
    state, evidence = classify_page("duckduckgo", 200, RESULTS_PAGE)
    assert state == "ok"
    assert evidence and evidence[0].startswith("marker x3:")


def test_rate_limit_status_wins_over_body():
    assert classify_page("brave", 429, RESULTS_PAGE)[0] == "rate_limited"


def test_forbidden_status_is_blocked():
    assert classify_page("startpage", 403, RESULTS_PAGE)[0] == "blocked"


def test_js_shell_is_js_required():
    assert classify_page("google", 200, JS_PAGE)[0] == "js_required"


def test_quiet_page_is_empty():
    assert classify_page("yahoo", 200, PLAIN_PAGE)[0] == "empty"


def test_probe_records_status_title_and_latency():
    def fetch(url: str) -> tuple[int, str, float]:
        assert "mojeek.com" in url
        return 200, CAPTCHA_PAGE, 12.5

    health = probe_backend("mojeek", "python packaging", fetch=fetch)
    assert health.state == "challenged"
    assert health.http_status == 200
    assert health.title == "Captcha"
    assert health.latency_ms == 12.5
    assert health.bytes == len(CAPTCHA_PAGE)


def test_fetch_exception_is_reported_as_network():
    def fetch(url: str) -> tuple[int, str, float]:
        raise OSError("connection reset")

    health = probe_backend("yahoo", "q", fetch=fetch)
    assert health.state == "network"
    assert health.error and "connection reset" in health.error


def test_unknown_backend_is_unmapped_without_a_request():
    def fetch(url: str) -> tuple[int, str, float]:  # pragma: no cover - must not run
        raise AssertionError("no request should be made for an unmapped backend")

    assert probe_backend("mystery", "q", fetch=fetch).state == "unmapped"


def test_run_health_separates_healthy_from_blocked():
    pages = {
        "duckduckgo": (200, RESULTS_PAGE, 1.0),
        "mojeek": (200, CAPTCHA_PAGE, 1.0),
        "startpage": (403, PLAIN_PAGE, 1.0),
    }

    def fetch(url: str) -> tuple[int, str, float]:
        for name, payload in pages.items():
            if name in url or (name == "duckduckgo" and "duckduckgo" in url):
                return payload
        return 200, PLAIN_PAGE, 1.0

    report = run_health(["duckduckgo", "mojeek", "startpage"], fetch=fetch)
    assert report["status"] == "complete"
    assert report["healthy"] == ["duckduckgo"]
    assert report["blocked"] == ["mojeek", "startpage"]
    assert report["count"] == 3


def test_run_health_never_raises_on_a_broken_fetcher():
    def fetch(url: str) -> tuple[int, str, float]:
        raise RuntimeError("boom")

    report = run_health(["yahoo"], fetch=fetch)
    assert report["backends"][0]["state"] == "network"
    assert report["healthy"] == []
