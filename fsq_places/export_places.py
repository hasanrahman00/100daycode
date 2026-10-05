"""Download Foursquare OS Places from Hugging Face and export every column.

Usage:
    export HF_TOKEN=hf_xxx            # never commit your token
    python export_places.py --schema-only
    python export_places.py                       # every row, every column -> output/places.csv
    python export_places.py --country US --open-only --with-website
    python export_places.py --format parquet      # same data, ~5x smaller file

Outputs (in ./output):
    places.csv  (or places.parquet)   one file, all rows, all columns
    domains.csv                       unique website domains for the LinkedIn crawl
"""
import argparse
import os
import re
import sys
from pathlib import Path

import duckdb
from huggingface_hub import HfApi, snapshot_download

REPO_ID = "foursquare/fsq-os-places"
RELEASE_RE = re.compile(r"release/dt=(\d{4}-\d{2}-\d{2})/places/parquet/")


def latest_release(api: HfApi, token: str) -> str:
    files = api.list_repo_files(REPO_ID, repo_type="dataset", token=token)
    releases = sorted({m.group(1) for f in files if (m := RELEASE_RE.search(f))})
    if not releases:
        sys.exit(f"No places parquet files found in {REPO_ID}. Did you accept the dataset terms on Hugging Face?")
    print(f"Available releases: {', '.join(releases)}")
    return releases[-1]


def download(release: str, data_dir: Path, token: str) -> str:
    pattern = f"release/dt={release}/places/parquet/*"
    print(f"Downloading {pattern} to {data_dir} (~10 GB, resumes if interrupted)...")
    snapshot_download(
        REPO_ID, repo_type="dataset", allow_patterns=[pattern],
        local_dir=data_dir, token=token,
    )
    return str(data_dir / f"release/dt={release}/places/parquet/*.parquet")


def csv_safe_select(con, source: str) -> str:
    """CSV can't hold lists, structs or binary, so turn those columns into text. Every column is kept."""
    cols = []
    for name, dtype, *_ in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{source}')").fetchall():
        q = f'"{name}"'
        if dtype == "BLOB":
            cols.append(f"hex({q}) AS {q}")  # geom is WKB; hex keeps it lossless
        elif dtype.endswith("[]"):
            cols.append(f"array_to_string({q}, ' | ') AS {q}")
        elif dtype.startswith(("STRUCT", "MAP")):
            cols.append(f"CAST({q} AS VARCHAR) AS {q}")
        else:
            cols.append(q)
    return ", ".join(cols)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--release", help="dt=YYYY-MM-DD release to use (default: latest)")
    p.add_argument("--data-dir", default="data", type=Path)
    p.add_argument("--out-dir", default="output", type=Path)
    p.add_argument("--format", choices=["parquet", "csv"], default="csv")
    p.add_argument("--country", nargs="*", help="Only these 2-letter country codes, e.g. US GB DE")
    p.add_argument("--open-only", action="store_true", help="Drop places with date_closed set")
    p.add_argument("--with-website", action="store_true", help="Only places that have a website")
    p.add_argument("--schema-only", action="store_true", help="Print the columns and row count, then stop")
    p.add_argument("--source", help="Skip the download and read this local parquet glob instead")
    args = p.parse_args()

    if args.source:
        source = args.source
    else:
        token = os.environ.get("HF_TOKEN")
        if not token:
            sys.exit("Set HF_TOKEN first: export HF_TOKEN=hf_xxx")
        release = args.release or latest_release(HfApi(), token)
        print(f"Using release {release}")
        source = download(release, args.data_dir, token)

    con = duckdb.connect()
    print("\nColumns:")
    for name, dtype, *_ in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{source}')").fetchall():
        print(f"  {name:<24} {dtype}")
    total = con.execute(f"SELECT count(*) FROM read_parquet('{source}')").fetchone()[0]
    print(f"\nTotal rows: {total:,}")
    if args.schema_only:
        return

    where = ["TRUE"]
    if args.country:
        codes = ", ".join(f"'{c.upper()}'" for c in args.country)
        where.append(f"country IN ({codes})")
    if args.open_only:
        where.append("date_closed IS NULL")
    if args.with_website:
        where.append("website IS NOT NULL AND trim(website) <> ''")
    where_sql = " AND ".join(where)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"CREATE VIEW places AS SELECT * FROM read_parquet('{source}') WHERE {where_sql}")
    rows = con.execute("SELECT count(*) FROM places").fetchone()[0]
    print(f"Rows after filters: {rows:,}")

    if args.format == "parquet":
        out = args.out_dir / "places.parquet"
        con.execute(f"COPY places TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    else:
        out = args.out_dir / "places.csv"
        select = csv_safe_select(con, source)
        con.execute(f"COPY (SELECT {select} FROM places) TO '{out}' (FORMAT CSV, HEADER)")
    print(f"Wrote {out}")

    domains = args.out_dir / "domains.csv"
    con.execute(f"""
        COPY (
            SELECT domain, any_value(country) AS country, count(*) AS places
            FROM (
                SELECT country, regexp_extract(lower(trim(website)),
                       '^(?:[a-z]+://)?(?:www\\.)?([^/:?#\\s]+)', 1) AS domain
                FROM places WHERE website IS NOT NULL
            )
            WHERE domain LIKE '%.%'
            GROUP BY domain
            ORDER BY places DESC
        ) TO '{domains}' (FORMAT CSV, HEADER)
    """)
    n = con.execute(f"SELECT count(*) FROM read_csv('{domains}')").fetchone()[0]
    print(f"Wrote {domains} ({n:,} unique domains for the LinkedIn crawl)")


if __name__ == "__main__":
    main()
