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
| `--companies` | Write `companies.csv`: one row per unique website domain (all columns + `domain`, `domain_places`, `likely_directory`) |
| `--domains-only` | Rebuild only `domains.csv` (skips the big places export) |
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

## Company website crawler

`company_crawler.py` visits each domain in `domains.csv` (homepage, then contact and about pages)
and collects:

- title, meta description, site name, language
- emails (incl. Cloudflare-protected ones) and phone numbers
- LinkedIn, Facebook, Instagram, X/Twitter, YouTube, TikTok, Pinterest, GitHub, WhatsApp
- schema.org company data: name, legal name, founding date, employees
- postal address as one full string (`org_address`), from schema.org data, microdata, the
  `<address>` tag, Google/Apple Maps links, or address patterns in the page text (US, CA, UK, DE/AT/CH)
- technologies, detected with 7,000+ open-source Wappalyzer fingerprints from
  [enthec/webappanalyzer](https://github.com/enthec/webappanalyzer) (GPL-3.0, downloaded once to
  `data/webappanalyzer/`). Rules that need a real browser (`js`, `dom`) are skipped.

Progress is stored in `output/crawl.db`, so you can stop with Ctrl+C and rerun to resume.
Results are also appended every 5 seconds to a **live CSV** you can watch while it runs
(columns: domain, country, places, status, http_status, final_url, title, description, site_name,
lang, emails, phones, nine social links, org_name, org_address, tech, tech_categories, generator,
contact_url, about_url, pages, error, crawled_at as a readable date):
`output/crawl_live.csv`, or `output/crawl_live_shard0of4.csv` etc. when using `--shard`
(one file per window so they never collide). Only successfully crawled sites are written unless
you pass `--live-all`. If the file is open in Excel (which locks it), rows are kept in memory and
written once it's closed.

```bash
python company_crawler.py init                 # load domains.csv into output/crawl.db (once)
python company_crawler.py crawl --limit 1000   # test run
python company_crawler.py crawl                # full run (resumable)
python company_crawler.py stats                # progress, field coverage, speed and ETA
python company_crawler.py join --companies path/to/companies.csv   # output/companies_enriched.csv
```

| Crawl flag | Effect |
|---|---|
| `--concurrency 200` | Sites fetched at the same time per window |
| `--shard 0/4` | Split the work over several windows/cores: run `0/4`, `1/4`, `2/4`, `3/4` |
| `--max-pages 3` | Pages per site: homepage + contact + about |
| `--country US GB` | Only crawl domains from these countries |
| `--retry-errors` | Also retry sites that failed before |
| `--no-robots` | Skip the robots.txt check (faster, less polite) |
| `--no-tech` | Skip technology detection (faster) |

Statuses: `ok`, `robots` (site disallows crawling), `skipped` (social/booking platforms),
`error` (dead or unreachable).

**Directory sites:** some businesses list a directory page as their website (e.g. `gelbeseiten.de`,
244k German places). A domain with 20+ places where under 20% of place names contain the domain's brand gets
`likely_directory = true` in `companies.csv`, and `join` leaves its `site_*` columns empty so the
directory's own emails and social links aren't given to the businesses it lists. Chains like
`walmart.com` are kept.
