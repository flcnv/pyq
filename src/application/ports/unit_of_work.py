from collections.abc import Callable
from types import TracebackType
from typing import Protocol, Self

from src.application.ports.document_storage import DocumentStorage
from src.application.ports.ingest_job_repository import IngestJobRepository
from src.application.ports.invoice_repository import InvoiceRepository


class UnitOfWork(Protocol):
    """One transaction across the repositories. Nothing is saved unless `commit()` is called;
    leaving the `async with` block without it rolls everything back."""

    documents: DocumentStorage
    jobs: IngestJobRepository
    invoices: InvoiceRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


type UnitOfWorkFactory = Callable[[], UnitOfWork]
