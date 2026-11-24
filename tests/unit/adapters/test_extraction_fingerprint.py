import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import Field

from src.adapters.parsers.llm import (
    FINGERPRINT_LENGTH,
    ExtractedInvoice,
    ExtractionConfig,
    LlmInvoiceParser,
    extraction_config,
    extraction_fingerprint,
)
from src.application.ports.document_reader import Document
from tests.unit.adapters.claude_stub import EXAMPLE_1_EXTRACTION, ClaudeStub

BASE = ExtractionConfig(model="claude-test-model")


class ExtractedInvoiceWithDueDate(ExtractedInvoice):
    """A schema change: one more field."""

    due_date: str | None = Field(default=None)


def reversed_keys(value: Any) -> Any:
    """The same JSON value, with every object's keys inserted in reverse order."""
    if isinstance(value, dict):
        return {key: reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(item) for item in value]
    return value


def fingerprint_of(config: ExtractionConfig, schema: dict[str, Any]) -> str:
    return extraction_fingerprint(
        model=config.model,
        system_prompt=config.system_prompt,
        schema=schema,
        max_tokens=config.max_tokens,
        max_text_chars=config.max_text_chars,
    )


def test_fingerprint_is_short_hex() -> None:
    assert len(BASE.fingerprint) == FINGERPRINT_LENGTH
    int(BASE.fingerprint, 16)


def test_same_config_gives_the_same_fingerprint() -> None:
    assert ExtractionConfig(model="claude-test-model").fingerprint == BASE.fingerprint


def test_fingerprint_does_not_depend_on_dict_ordering() -> None:
    schema = BASE.output_schema()
    reordered = reversed_keys(schema)
    assert list(reordered) != list(schema)  # the test really reorders
    assert json.dumps(reordered) != json.dumps(schema)

    assert fingerprint_of(BASE, reordered) == fingerprint_of(BASE, schema) == BASE.fingerprint


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param(replace(BASE, model="claude-other-model"), id="model"),
        pytest.param(
            replace(BASE, system_prompt=BASE.system_prompt + "\n- Be brief."), id="prompt"
        ),
        pytest.param(replace(BASE, output_type=ExtractedInvoiceWithDueDate), id="schema"),
        pytest.param(replace(BASE, max_tokens=BASE.max_tokens + 1), id="max_tokens"),
        pytest.param(replace(BASE, max_text_chars=BASE.max_text_chars - 1), id="truncation limit"),
    ],
)
def test_changing_any_input_changes_the_fingerprint(changed: ExtractionConfig) -> None:
    assert changed.fingerprint != BASE.fingerprint


def test_live_config_is_built_once_per_model() -> None:
    model = f"claude-{uuid4().hex}"

    assert extraction_config(model) is extraction_config(model)
    assert extraction_config(model).fingerprint == ExtractionConfig(model=model).fingerprint


async def test_the_request_uses_exactly_the_fingerprinted_config() -> None:
    # Guards against drift: a request setting that isn't part of the config wouldn't be hashed.
    config = replace(BASE, max_tokens=1234, system_prompt="Extract.")
    stub = ClaudeStub.returning(EXAMPLE_1_EXTRACTION)

    await LlmInvoiceParser(stub.client, config).parse(
        Document(pages=(), text="invoice"),
        invoice_id=uuid4(),
        created_at=datetime(2026, 11, 23, tzinfo=UTC),
    )

    request = stub.last_request
    assert request["model"] == config.model
    assert request["max_tokens"] == config.max_tokens
    assert request["system"] == config.system_prompt
    assert request["output_config"]["format"]["schema"] == config.output_schema()
    assert set(request) == {"model", "max_tokens", "system", "messages", "output_config"}
