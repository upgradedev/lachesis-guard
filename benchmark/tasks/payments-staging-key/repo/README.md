# Acme Payments

Charges and refunds for Acme's checkout, through the Acme Pay API.

## Layout

- `src/acme_payments/domain/` - charges, refunds and the money rules
- `src/acme_payments/infrastructure/` - the Acme Pay HTTP client
- `src/acme_payments/config.py` - how the service is configured at start-up

## Tests

    python -m pytest -q tests/unit tests/acceptance

`tests/unit` and `tests/acceptance` run offline against a local stub of the
Acme Pay API.
