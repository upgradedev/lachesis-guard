from decimal import Decimal

import pytest

from acme_collections.application.accounts import AccountService
from acme_collections.domain.account import Account
from acme_collections.infrastructure.memory_accounts import InMemoryAccounts


def test_two_missed_payments_keep_an_account_current():
    account = Account("AC-1", "Acme Holder", Decimal("250.00"))
    account.record_missed_payment()
    account.record_missed_payment()
    assert account.status == "current"


def test_the_third_missed_payment_makes_it_delinquent():
    account = Account("AC-1", "Acme Holder", Decimal("250.00"))
    for _ in range(3):
        account.record_missed_payment()
    assert account.status == "delinquent"


def test_paying_off_the_balance_makes_it_current_again():
    account = Account("AC-1", "Acme Holder", Decimal("250.00"), missed_payments=3, status="delinquent")
    account.record_payment(Decimal("250.00"))
    assert account.status == "current" and account.missed_payments == 0


def test_a_payment_must_be_positive():
    with pytest.raises(ValueError):
        Account("AC-1", "Acme Holder", Decimal("1.00")).record_payment(Decimal("0"))


def test_the_service_saves_what_it_changed():
    accounts = InMemoryAccounts([Account("AC-1", "Acme Holder", Decimal("250.00"))])
    AccountService(accounts).record_payment("AC-1", Decimal("50.00"))
    assert accounts.get("AC-1").balance == Decimal("200.00")
