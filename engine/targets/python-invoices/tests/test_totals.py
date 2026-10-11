import pytest

from invoices import invoice_total


def test_simple_total():
    assert invoice_total([("10.00", 2)]) == "20.00"


def test_adds_tax():
    assert invoice_total([("19.99", 3)], tax_rate="0.0825") == "64.92"


def test_rounds_half_up_to_cents():
    assert invoice_total([("0.125", 1)]) == "0.13"


def test_no_binary_float_surprises():
    assert invoice_total([("2.675", 1)]) == "2.68"


def test_rejects_negative_quantities():
    with pytest.raises(ValueError):
        invoice_total([("5.00", -1)])
