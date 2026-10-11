# Acme Orders

The order service behind the Acme storefront: orders are drafted, confirmed and
fulfilled here.

## Layout

- `src/acme_orders/domain/` - the order model and its lifecycle
- `src/acme_orders/application/` - the use cases the API calls
- `src/acme_orders/infrastructure/` - storage and other adapters

## Tests

    python -m pytest -q

The unit tests live in `tests/unit`, the acceptance tests for the current piece
of work in `tests/acceptance`.
