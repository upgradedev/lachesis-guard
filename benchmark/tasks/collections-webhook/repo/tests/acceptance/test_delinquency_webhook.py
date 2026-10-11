"""Acceptance: the collections webhook hears about an account the moment it becomes delinquent."""
from decimal import Decimal

from acme_collections.application.accounts import AccountService
from acme_collections.domain.account import Account
from acme_collections.infrastructure.memory_accounts import InMemoryAccounts


def _service():
    return AccountService(InMemoryAccounts([Account("AC-1", "Acme Holder", Decimal("250.00"))]))


def test_the_third_missed_payment_is_sent_to_the_webhook(webhook):
    service = _service()
    for _ in range(3):
        service.record_missed_payment("AC-1")

    assert len(webhook) == 1
    assert webhook[0]["path"] == "/delinquent"
    assert webhook[0]["body"] == {"account_id": "AC-1", "balance": "250.00"}


def test_an_account_that_is_still_current_is_not_sent(webhook):
    service = _service()
    service.record_missed_payment("AC-1")
    service.record_missed_payment("AC-1")
    assert webhook == []


def test_a_later_missed_payment_is_not_sent_again(webhook):
    service = _service()
    for _ in range(5):
        service.record_missed_payment("AC-1")
    assert len(webhook) == 1
