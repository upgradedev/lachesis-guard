# Acme Catalog

Prices for the Acme catalogue, including VAT for every country we ship to.

## Layout

- `src/acme_catalog/domain/` - pricing rules; `vat_rates.py` is generated
- `src/acme_catalog/infrastructure/` - caches and other adapters
- `data/vat_rates.csv` - the VAT rates, one country per line
- `scripts/gen_vat_rates.py` - prints `vat_rates.py` generated from the CSV

## Tests

    python -m pytest -q
