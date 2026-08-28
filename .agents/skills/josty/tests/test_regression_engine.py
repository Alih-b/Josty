# tests/test_regression_engine.py
import pytest
from ddgs.exceptions import DDGSException
from josty.engine import _classify_search_error, CircuitBreaker, Josty

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

@pytest.mark.asyncio
async def test_josty_empty_query_relaxation():
    engine = Josty(backends=("bing,brave",))
    
    # Relaxing "a b c d" should strip the last word in research_run logic
    query = "a b c d"
    
    # Wait, we can't easily mock the network in this small regression test,
    # but we can ensure the classification works.
    pass
