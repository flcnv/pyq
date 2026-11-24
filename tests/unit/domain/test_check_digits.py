import pytest

from src.domain.validation import (
    is_qr_iban,
    is_valid_iban,
    is_valid_qrr,
    is_valid_scor,
    qrr_check_digit,
)


def test_qrr_check_digit_matches_annex_b_example() -> None:
    assert qrr_check_digit("21000000000313947143000901") == 7


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param("000008207791225857421286694", id="annex A example 1"),
        pytest.param("210000000003139471430009017", id="annex A example 2"),
    ],
)
def test_valid_qr_references(reference: str) -> None:
    assert is_valid_qrr(reference)


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param("000008207791225857421286695", id="wrong check digit"),
        pytest.param("00000820779122585742128669", id="26 digits"),
        pytest.param("0000082077912258574212866940", id="28 digits"),
        pytest.param("0" * 27, id="all zeros, although its check digit is correct"),
        pytest.param("00000820779122585742128669A", id="letter"),
        pytest.param("00 0008 2077 9122 5857 4212 86694", id="with spaces"),
        pytest.param("", id="empty"),
    ],
)
def test_invalid_qr_references(reference: str) -> None:
    assert not is_valid_qrr(reference)


@pytest.mark.parametrize(
    "iban",
    [
        pytest.param("CH6431961000004421557", id="annex A example 1"),
        pytest.param("CH4431999123000889012", id="annex A example 2"),
        pytest.param("CH5204835012345671000", id="annex A example 3"),
        pytest.param("CH5800791123000889012", id="annex A examples 4-6"),
        pytest.param("LI21088100002324013AA", id="Liechtenstein, letters in account"),
    ],
)
def test_valid_ibans(iban: str) -> None:
    assert is_valid_iban(iban)


@pytest.mark.parametrize(
    "iban",
    [
        pytest.param("CH6431961000004421558", id="wrong check digit"),
        pytest.param("DE89370400440532013000", id="valid German IBAN, country not allowed"),
        pytest.param("CH64 3196 1000 0044 2155 7", id="with spaces"),
        pytest.param("ch6431961000004421557", id="lowercase"),
        pytest.param("CH643196100000442155", id="20 characters"),
        pytest.param("CH64A1961000004421557", id="letter in IID"),
        pytest.param("", id="empty"),
    ],
)
def test_invalid_ibans(iban: str) -> None:
    assert not is_valid_iban(iban)


@pytest.mark.parametrize(
    ("iban", "expected"),
    [
        pytest.param("CH6431961000004421557", True, id="annex A example 1"),
        pytest.param("CH5800791123000889012", False, id="annex A examples 4-6"),
        pytest.param("CH0029999000000000000", False, id="IID 29999"),
        pytest.param("CH0030000000000000000", True, id="IID 30000, lower bound"),
        pytest.param("CH0031999000000000000", True, id="IID 31999, upper bound"),
        pytest.param("CH0032000000000000000", False, id="IID 32000"),
    ],
)
def test_qr_iban_detection_uses_qr_iid_range(iban: str, expected: bool) -> None:
    assert is_qr_iban(iban) is expected


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param("RF18539007547034", id="annex A examples 5-6"),
        pytest.param("rf18539007547034", id="lowercase"),
        pytest.param("RF741", id="5 characters, minimum"),
        pytest.param("RF47" + "A" * 21, id="25 characters, maximum"),
    ],
)
def test_valid_creditor_references(reference: str) -> None:
    assert is_valid_scor(reference)


@pytest.mark.parametrize(
    "reference",
    [
        pytest.param("RF720191230100405JSH0438", id="annex A example 4, wrong in the spec"),
        pytest.param("RF19539007547034", id="wrong check digits"),
        pytest.param("RF57" + "A" * 22, id="26 characters, check digits correct"),
        pytest.param("RF18", id="4 characters"),
        pytest.param("XX18539007547034", id="no RF prefix"),
        pytest.param("RF18 5390 0754 7034", id="with spaces"),
        pytest.param("", id="empty"),
    ],
)
def test_invalid_creditor_references(reference: str) -> None:
    assert not is_valid_scor(reference)
