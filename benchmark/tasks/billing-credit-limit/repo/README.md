# Acme Billing

Invoices for Acme's business customers.

## Layout

- `src/acme_billing/domain/` - invoices and their lifecycle
- `src/acme_billing/application/` - the use cases the billing API calls
- `src/acme_billing/infrastructure/` - storage and the customer directory (the
  CRM database in production, an in-memory registry here)

## Tests

    python -m pytest -q
