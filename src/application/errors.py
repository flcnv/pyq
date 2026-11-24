from uuid import UUID


class ApplicationError(Exception):
    """Base class for all application errors."""


class UnreadableDocumentError(ApplicationError):
    """The upload can't be read as a document, or holds nothing to extract from."""


class NoQrCodeFoundError(ApplicationError):
    """No Swiss QR Code in the document. The only signal that triggers the LLM fallback."""


class ExtractionFailedError(ApplicationError):
    """The LLM extraction couldn't run or returned unusable output (API error, timeout, ...)."""


class UnusableLlmOutputError(ExtractionFailedError):
    """The LLM answered, but the answer can't be used (wrong shape, refusal, cut off).

    Deterministic for a given input and model: retrying the same file won't help, unlike the
    transient failures (timeouts, network, 5xx, 429) that the base class also covers.
    """


class InvoiceNotFoundError(ApplicationError):
    def __init__(self, invoice_id: UUID) -> None:
        super().__init__(f"Invoice {invoice_id} not found")
        self.invoice_id = invoice_id


class RateLimitExceededError(ApplicationError):
    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class LlmQuotaExceededError(RateLimitExceededError):
    """The LLM fallback is over its per-client or global budget. QR invoices still work."""


class RateLimiterUnavailableError(ApplicationError):
    """The rate limit store can't be reached, so limits can't be checked."""


class JobAlreadyExistsError(ApplicationError):
    """A job for the same file content exists (a concurrent upload of the same file)."""


class IngestionNotFoundError(ApplicationError):
    def __init__(self, job_id: UUID) -> None:
        super().__init__(f"Ingestion {job_id} not found")
        self.job_id = job_id


class LlmFallbackNotConfiguredError(ApplicationError):
    """No QR code, and no LLM is configured (no API key) to extract the invoice instead."""


class StorageUnavailableError(ApplicationError):
    """The database can't be reached or dropped the connection: worth retrying later."""
