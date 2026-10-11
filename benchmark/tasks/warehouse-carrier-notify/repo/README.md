# Acme Warehouse

Packing and dispatch for Acme's warehouse.

## Layout

- `src/Acme.Warehouse/Domain/` - shipments and their lifecycle
- `src/Acme.Warehouse/Application/` - the use cases the warehouse API calls
- `src/Acme.Warehouse/Infrastructure/` - storage and other adapters

## Tests

    dotnet run --project tests/Acme.Warehouse.Tests

The tests are a small console runner with no test framework, because this
repository builds offline: `nuget.config` lists no package source.
