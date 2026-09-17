"""Example of mocking an external API call: tests the retry/circuit-breaker
logic in llm.py without hitting a real LLM provider, spending a real API
call, or waiting out a real backoff delay.

The pattern: replace the network call (client._client.post) with a fake
that returns real httpx.Response objects, so raise_for_status()/json()
behave exactly like the real thing would -- only the network round trip
is faked, not the response-handling logic being tested.
"""

from unittest.mock import patch

import httpx
import pytest

from applypilot.llm import LLMCircuitOpenError, LLMClient


def _response(status_code, **kwargs):
    request = httpx.Request("POST", "https://example.test/chat/completions")
    return httpx.Response(status_code, request=request, **kwargs)


def _ok_response(text="hello"):
    return _response(200, json={"choices": [{"message": {"content": text}}]})


def test_retries_on_429_then_succeeds():
    client = LLMClient(base_url="https://example.test", model="test-model", api_key="fake-key")

    with patch("applypilot.llm.time.sleep"), \
         patch.object(client, "_client") as mock_http:
        mock_http.post.side_effect = [
            _response(429, headers={"Retry-After": "1"}),
            _ok_response("recovered"),
        ]
        result = client.chat([{"role": "user", "content": "hi"}])

    assert result == "recovered"
    assert mock_http.post.call_count == 2


def test_circuit_breaker_trips_after_repeated_failure():
    client = LLMClient(base_url="https://example.test", model="test-model", api_key="fake-key")

    with patch("applypilot.llm.time.sleep"), \
         patch.object(client, "_client") as mock_http:
        # Every call exhausts its own retries and fails -- simulate a
        # sustained outage, not per-call flakiness.
        mock_http.post.return_value = _response(503)

        for _ in range(5):
            with pytest.raises(httpx.HTTPStatusError):
                client.chat([{"role": "user", "content": "hi"}])

        # The 6th call should not even attempt the network -- the circuit
        # is open. This is the behavior that protects a real batch run
        # (e.g. scoring hundreds of jobs) from paying a multi-minute
        # backoff on every remaining item once the provider is clearly down.
        calls_before = mock_http.post.call_count
        with pytest.raises(LLMCircuitOpenError):
            client.chat([{"role": "user", "content": "hi"}])
        assert mock_http.post.call_count == calls_before
