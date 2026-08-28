from ddgs.exceptions import DDGSException
from josty.engine import CircuitBreaker, _classify_search_error


def test_classify_empty_error():
    exc = DDGSException("No results found.")
    assert _classify_search_error(exc) == "empty"


def test_circuit_breaker_does_not_open_on_empty():
    breaker = CircuitBreaker(fail_threshold=1)

    # Simulate an empty result success recording
    breaker.record_success("bing,brave", "search")

    # Should still be allowed
    allowed, skip_message = breaker.status("bing,brave", "search")
    assert allowed is True
    assert skip_message is None

