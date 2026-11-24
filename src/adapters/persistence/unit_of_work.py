from types import TracebackType
from typing import Self

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.persistence.document_storage import PostgresDocumentStorage
from src.adapters.persistence.job_repository import SqlAlchemyIngestJobRepository
from src.adapters.persistence.repository import SqlAlchemyInvoiceRepository
from src.application.errors import StorageUnavailableError
from src.application.ports.document_storage import DocumentStorage
from src.application.ports.ingest_job_repository import IngestJobRepository
from src.application.ports.invoice_repository import InvoiceRepository


class SqlAlchemyUnitOfWork:
    """One session and one transaction; every repository works on it."""

    documents: DocumentStorage
    jobs: IngestJobRepository
    invoices: InvoiceRepository

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        self.documents = PostgresDocumentStorage(self._session)
        self.jobs = SqlAlchemyIngestJobRepository(self._session)
        self.invoices = SqlAlchemyInvoiceRepository(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            # Rolls back whatever wasn't committed (a no-op after `commit()`), then frees it.
            await self._session.rollback()
            await self._session.close()
        except Exception as cleanup_error:
            if exc is None:
                raise _translated(cleanup_error) from cleanup_error
            # Otherwise the original error explains more than the failed cleanup does.
        if isinstance(exc, Exception) and _is_connection_failure(exc):
            raise StorageUnavailableError("The database is unavailable") from exc

    async def commit(self) -> None:
        await self._session.commit()


def _is_connection_failure(error: Exception) -> bool:
    """The database is unreachable or dropped the connection: a reason to retry later. Not a
    constraint violation or a bad query, which retrying won't fix."""
    if isinstance(error, OperationalError | InterfaceError):
        return True
    if isinstance(error, DBAPIError) and error.connection_invalidated:
        return True
    # asyncpg raises these unwrapped when it can't connect at all.
    return isinstance(error, OSError | TimeoutError)


def _translated(error: Exception) -> Exception:
    if _is_connection_failure(error):
        return StorageUnavailableError("The database is unavailable")
    return error
