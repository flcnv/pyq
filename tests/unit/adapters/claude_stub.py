import json
from typing import Any

import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

# A complete, valid extraction of Annex A Example 1, as printed on a payment part (with spaces).
EXAMPLE_1_EXTRACTION: dict[str, Any] = {
    "creditor_iban": "CH64 3196 1000 0044 2155 7",
    "creditor": {
        "name": "Max Muster & Söhne",
        "address": {
            "street": "Musterstrasse",
            "building_number": "123",
            "postal_code": "8000",
            "town": "Seldwyla",
            "country": "CH",
        },
    },
    "amount": "50.00",
    "currency": "CHF",
    "debtor": {
        "name": "Simon Muster",
        "address": {
            "street": "Musterstrasse",
            "building_number": "1",
            "postal_code": "8000",
            "town": "Seldwyla",
            "country": "CH",
        },
    },
    "reference_type": "QRR",
    "reference": "00 00082 07791 22585 74212 86694",
    "unstructured_message": "Payment of travel",
    "billing_information": None,
}


def message_body(text: str, *, stop_reason: str = "end_turn") -> dict[str, Any]:
    """A Messages API response with a single text block."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


class ClaudeStub:
    """A real `AsyncAnthropic` client whose HTTP transport answers in-process: no network.

    This exercises the SDK's own request building and response parsing, which a hand-written
    fake of `client.messages.parse` would skip.
    """

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.client = AsyncAnthropic(
            api_key="test-key",
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self._handle)),
        )

    @classmethod
    def returning(cls, extraction: dict[str, Any] | str, **kwargs: Any) -> ClaudeStub:
        text = extraction if isinstance(extraction, str) else json.dumps(extraction)
        return cls(httpx2.Response(200, json=message_body(text, **kwargs)))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def last_request(self) -> dict[str, Any]:
        return self.requests[-1]
