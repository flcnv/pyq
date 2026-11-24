# Alembic autogenerate only sees imported models: re-export every ORM model here.
from src.adapters.persistence.orm.base import Base
from src.adapters.persistence.orm.document_record import DocumentRecord
from src.adapters.persistence.orm.ingest_job_record import IngestJobRecord
from src.adapters.persistence.orm.invoice_record import InvoiceRecord

__all__ = ["Base", "DocumentRecord", "IngestJobRecord", "InvoiceRecord"]
