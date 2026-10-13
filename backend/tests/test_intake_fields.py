"""Intake sheet cell parsing: typed by people, refused rather than guessed."""
import pytest

from app.intake_fields import IntakeFieldError, customer_type, requested_amount


@pytest.mark.parametrize("cell,expected", [
    ("New", "new"), ("new customer", "new"), ("  NEW  ", "new"),
    ("Existing", "existing"), ("Existing  Customer", "existing"), ("repeat", "existing"),
])
def test_customer_type_reads_the_ways_people_type_it(cell, expected):
    assert customer_type(cell) == expected


@pytest.mark.parametrize("cell", ["", None, "vip", "n"])
def test_customer_type_refuses_anything_else(cell):
    with pytest.raises(IntakeFieldError):
        customer_type(cell)


@pytest.mark.parametrize("cell,expected", [
    (1000000, 1000000), (500000.0, 500000), ("1,000,000", 1000000),
    ("RWF 1 000 000", 1000000), ("750000 FRW", 750000), ("300000.00", 300000),
])
def test_requested_amount_reads_numbers_and_formatted_text(cell, expected):
    assert requested_amount(cell) == expected


@pytest.mark.parametrize("cell", ["", None, "1.5M", "about 200k", "12.5", -5, 0, True, "1,000,000-2,000,000"])
def test_requested_amount_refuses_what_it_cannot_read_for_certain(cell):
    with pytest.raises(IntakeFieldError):
        requested_amount(cell)
