# Foursquare OS Places export

Downloads the latest [Foursquare Open Source Places](https://huggingface.co/datasets/foursquare/fsq-os-places)
release (~100M places, Apache 2.0) and exports **every row and every column into a single CSV**,
plus a list of unique website domains to feed the LinkedIn crawler.

## Setup

1. On Hugging Face, open `foursquare/fsq-os-places` and accept the dataset terms.
2. Create a read token (Settings → Access Tokens).
3. Install and run:

```bash
pip install -r requirements.txt
export HF_TOKEN=hf_xxx                  # keep it out of git

python export_places.py --schema-only   # print the real columns and row count
python export_places.py                 # output/places.csv + output/domains.csv
```

## Options

| Flag | Effect |
|---|---|
| `--country US GB` | Keep only these countries |
| `--open-only` | Drop places with `date_closed` set |
| `--with-website` | Keep only places that have a website |
| `--format parquet` | Write `places.parquet` instead (same data, much smaller) |
| `--release 2025-09-09` | Use a specific release instead of the latest |
| `--source 'path/*.parquet'` | Skip the download and use local files |

## Notes

- The full CSV is very large (expect tens of GB) and has far more rows than Excel can open
  (~1M row limit). Open it with DuckDB, Postgres, BigQuery or pandas in chunks.
- You need ~10 GB of disk for the download plus room for the CSV.
- In the CSV, list columns (`fsq_category_ids`, `fsq_category_labels`) are joined with ` | `,
  `geom` is WKT text (e.g. `POINT (90.4 23.8)`), and `bbox` is written as text. Parquet keeps the original types.
- `domains.csv` has one row per unique website domain, with how many places use it.
