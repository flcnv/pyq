from src.application.errors import (
    ExtractionFailedError,
    LlmFallbackNotConfiguredError,
    LlmQuotaExceededError,
    RateLimiterUnavailableError,
    StorageUnavailableError,
    UnreadableDocumentError,
)
from src.application.models.ingest_job import JobError, JobErrorDetail
from src.domain.errors import InvalidInvoiceError, InvalidQrPayloadError

# One mapping from an error to its public code and message, used by the API's error responses and
# stored on failed jobs, so both report a problem the same way.

EXTRACTION_FAILED_MESSAGE = "Invoice extraction is unavailable right now"
RATE_LIMITER_UNAVAILABLE_MESSAGE = "Usage limits can't be checked right now, try again later"

# For an error with no public form: a bug. Details stay in the log, never in the response.
INTERNAL_ERROR = JobError(code="internal_error", message="An unexpected error occurred")

# A worker stopped (crash, kill, OOM) while holding a job that had no attempts left.
LEASE_EXPIRED = JobErrorDetail(
    code="lease_expired", message="The worker processing this file stopped without finishing"
)


def retries_exhausted(attempts: int, last_error: JobError | JobErrorDetail) -> JobError:
    return JobError(
        code="retries_exhausted",
        message=f"Processing failed {attempts} times; upload the file again to retry",
        details=(JobErrorDetail(code=last_error.code, message=last_error.message),),
    )


def job_error_for(error: Exception) -> JobError | None:
    """The public form of a known error; None for an error with no public form (a bug)."""
    match error:
        case InvalidInvoiceError():
            code = (
                "invalid_qr_payload"
                if isinstance(error, InvalidQrPayloadError)
                else "invalid_invoice"
            )
            details = tuple(
                JobErrorDetail(
                    code=issue.code.value,
                    field=issue.field,
                    message=issue.message,
                    severity=issue.severity.value,
                )
                for issue in error.issues
            )
            return JobError(code=code, message=str(error), details=details)
        case UnreadableDocumentError():
            return JobError(code="unreadable_document", message=str(error))
        case LlmFallbackNotConfiguredError():
            return JobError(code="llm_fallback_not_configured", message=str(error))
        case LlmQuotaExceededError():
            return JobError(code="llm_quota_exceeded", message=str(error))
        case ExtractionFailedError():
            # The cause (API error, odd output) is logged; the public message stays generic.
            return JobError(code="extraction_failed", message=EXTRACTION_FAILED_MESSAGE)
        case RateLimiterUnavailableError():
            return JobError(
                code="rate_limiter_unavailable", message=RATE_LIMITER_UNAVAILABLE_MESSAGE
            )
        case StorageUnavailableError():
            return JobError(code="storage_unavailable", message="The database is unavailable")
    return None
