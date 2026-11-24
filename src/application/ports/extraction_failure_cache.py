from typing import Protocol

from src.application.errors import UnusableLlmOutputError
from src.domain.errors import InvalidInvoiceError

type DeterministicFailure = UnusableLlmOutputError | InvalidInvoiceError


class ExtractionFailureCache(Protocol):
    """Remembers LLM extractions that failed in a way a retry can't fix, keyed by file hash.

    Implementations scope entries to the model and extraction version, and never raise when
    their store is unavailable: they log, miss and skip the write instead.
    """

    async def get(self, content_sha256: str) -> DeterministicFailure | None: ...

    async def remember(self, content_sha256: str, failure: DeterministicFailure) -> None: ...
