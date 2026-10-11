from decimal import Decimal

import pytest

from acme_billing.infrastructure import customer_directory


@pytest.fixture(autouse=True)
def directory():
    """Every test starts with one customer who has plenty of credit."""
    customer_directory.reset()
    customer_directory.register(
        customer_directory.Customer("C-1", credit_limit=Decimal("10000.00"), outstanding=Decimal("0.00"))
    )
    yield customer_directory
    customer_directory.reset()
