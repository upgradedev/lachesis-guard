# Acme Collections

Customer accounts, missed payments and the hand-over to the collections team.

## Layout

- `src/acme_collections/domain/` - accounts and their payment standing
- `src/acme_collections/application/` - the use cases the accounts API calls
- `src/acme_collections/infrastructure/` - storage and HTTP clients
- `src/acme_collections/settings.py` - configuration, read from the environment

## Tests

    python -m pytest -q
