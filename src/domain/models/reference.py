from dataclasses import dataclass
from enum import StrEnum


class ReferenceType(StrEnum):
    QRR = "QRR"
    SCOR = "SCOR"
    NON = "NON"


@dataclass(frozen=True, slots=True, kw_only=True)
class Reference:
    type: ReferenceType
    value: str | None
