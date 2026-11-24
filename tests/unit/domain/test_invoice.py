from decimal import Decimal

import pytest

from tests.unit.domain.factories import make_invoice


@pytest.mark.parametrize(
    "message",
    [
        "NICHT ZUR ZAHLUNG VERWENDEN",
        "NE PAS UTILISER POUR LE PAIEMENT",
        "NON UTILIZZARE PER IL PAGAMENTO",
        "DO NOT USE FOR PAYMENT",
        "BETG DUVRAR PER IL PAJAMENT",
    ],
)
def test_zero_amount_with_notification_message_is_notification(message: str) -> None:
    invoice = make_invoice(amount=Decimal("0.00"), unstructured_message=message)

    assert invoice.is_notification


@pytest.mark.parametrize(
    ("amount", "message"),
    [
        pytest.param(Decimal("50.00"), "DO NOT USE FOR PAYMENT", id="non-zero amount"),
        pytest.param(None, "DO NOT USE FOR PAYMENT", id="blank amount"),
        pytest.param(Decimal("0.00"), "do not use for payment", id="not capitals"),
        pytest.param(Decimal("0.00"), "DO NOT USE FOR PAYMENT!", id="not exact text"),
        pytest.param(Decimal("0.00"), None, id="no message"),
    ],
)
def test_other_invoices_are_not_notifications(amount: Decimal | None, message: str | None) -> None:
    invoice = make_invoice(amount=amount, unstructured_message=message)

    assert not invoice.is_notification
