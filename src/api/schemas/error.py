from pydantic import BaseModel

from src.application.models.ingest_job import JobError


class ErrorDetail(BaseModel):
    code: str
    field: str | None = None
    message: str
    severity: str | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] = []

    @classmethod
    def from_job_error(cls, error: JobError) -> ErrorBody:
        return cls(
            code=error.code,
            message=error.message,
            details=[
                ErrorDetail(
                    code=detail.code,
                    field=detail.field,
                    message=detail.message,
                    severity=detail.severity,
                )
                for detail in error.details
            ],
        )


class ErrorResponse(BaseModel):
    """The one error shape every endpoint returns: `{"error": {"code", "message", "details"}}`."""

    error: ErrorBody
