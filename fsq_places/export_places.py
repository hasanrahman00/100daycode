"""Download Foursquare OS Places from Hugging Face and export every column.

Usage:
    export HF_TOKEN=hf_xxx            # never commit your token
    python export_places.py --schema-only
    python export_places.py                       # every row, every column -> output/places.csv
    python export_places.py --country US --open-only --with-website
    python export_places.py --format parquet      # same data, ~5x smaller file
    python export_places.py --companies           # one row per unique website domain -> output/companies.csv

Outputs (in ./output):
    places.csv  (or places.parquet)   one file, all rows, all columns
    domains.csv                       unique website domains for the LinkedIn crawl
    companies.csv                     (--companies) one row per domain, all columns
"""
import argparse
import csv
import os
import re
import sys
from pathlib import Path

import duckdb
from huggingface_hub import HfApi, snapshot_download

REPO_ID = "foursquare/fsq-os-places"
# Website -> bare host. Some websites hold several URLs ("a.com,https://b.com"), so stop at separators too
DOMAIN_SQL = r"""rtrim(regexp_extract(lower(trim(website, ' "''')),
    '^(?:[a-z]+://)?(?:www\.)?([^/:?#\s,;|"''<>]+)', 1), '.')"""
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


def directory_sql(rows: str, min_places: int = 20, min_brand_share: float = 0.2) -> str:
    """Domains that look like directories (gelbeseiten.de) rather than one company's site.

    `rows` is a FROM target with `domain` and `name` columns. Chains (walmart.com) pass because
    most of their place names contain the domain's brand; directory listings don't.
    """
    return f"""
        WITH labeled AS (
            SELECT domain, name, string_split(domain, '.') AS parts FROM {rows}
        ), branded AS (
            SELECT domain, name,
                   regexp_replace(CASE
                       WHEN len(parts) >= 3 AND parts[len(parts) - 1] IN
                            ('co', 'com', 'org', 'net', 'gov', 'edu', 'ac', 'or', 'ne', 'go', 'gob')
                       THEN parts[len(parts) - 2] ELSE parts[len(parts) - 1] END, '[^a-z0-9]', '', 'g') AS brand
            FROM labeled
        )
        SELECT domain, count(*) AS places,
               avg(CASE WHEN contains(regexp_replace(lower(name), '[^a-z0-9]', '', 'g'), brand)
                        THEN 1 ELSE 0 END) AS brand_share
        FROM branded
        GROUP BY domain
        HAVING count(*) >= {min_places} AND brand_share < {min_brand_share}
    """


def csv_safe_select(con, source: str) -> str:
    """CSV can't hold lists, structs or binary, so turn those columns into text. Every column is kept."""
    cols = []
    for name, dtype, *_ in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{source}')").fetchall():
        q = f'"{name}"'
        if dtype == "BLOB":
            cols.append(f"hex({q}) AS {q}")  # raw WKB; hex keeps it lossless
        elif dtype.endswith("[]"):
            cols.append(f"array_to_string({q}, ' | ') AS {q}")
        elif dtype.startswith(("STRUCT", "MAP")):
            cols.append(f"CAST({q} AS VARCHAR) AS {q}")
        else:
            cols.append(q)
    return ", ".join(cols)


def add_site_places(con, out_dir: Path, keep_subdomains: bool = False):
    """View `site_places`: every place with a website plus `host` (www stripped) and `domain`.
    `domain` is the company's main domain (locations.pizzahut.com -> pizzahut.com), keeping the
    subdomain for website builders and government sites (see domain_root.py)."""
    host_sql = f"SELECT *, {DOMAIN_SQL} AS host FROM places WHERE website IS NOT NULL"
    if keep_subdomains:
        con.execute(f"CREATE VIEW site_places AS SELECT *, host AS domain FROM ({host_sql}) WHERE host LIKE '%_._%'")
        return
    from domain_root import root_domain

    print("Mapping website hosts to main domains (a few minutes)...", flush=True)
    path = out_dir / "host_roots.csv"
    cur = con.execute(f"SELECT DISTINCT host FROM ({host_sql}) WHERE host LIKE '%_._%'")
    n = changed = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["host", "root"])
        while batch := cur.fetchmany(200_000):
            for (host,) in batch:
                root = root_domain(host) or host
                changed += root != host
                w.writerow([host, root])
            n += len(batch)
            print(f"\r  {n:,} hosts", end="", flush=True)
    print(f"\r  {n:,} hosts, {changed:,} were subdomains mapped to their main domain")
    con.execute(f"""CREATE TEMP TABLE host_roots AS SELECT * FROM read_csv('{path}', header=true, quote='"',
                    columns={{'host': 'VARCHAR', 'root': 'VARCHAR'}})""")
    con.execute(f"CREATE VIEW site_places AS SELECT p.*, r.root AS domain FROM ({host_sql}) p JOIN host_roots r USING (host)")


def export_companies(con, source: str, out_dir: Path, fmt: str):
    """One row per unique website domain. For chains, keep the most useful location:
    open first, then without Foursquare quality flags, then one whose website is the main domain
    itself (not a store-locator subdomain), then the most recently refreshed.

    Picks the winning place id per domain from a few small columns, then reads the full
    rows for just those ids, so memory stays low even with 33M places that have a website.
    """
    con.execute("SET preserve_insertion_order = false")  # lets DuckDB spill big work to disk
    # A scratch folder per run, so deleting a shared .tmp can't break this one mid-way
    con.execute(f"SET temp_directory = '{out_dir / f'duckdb_tmp_{os.getpid()}'}'")
    print("Step 1/3: choosing one place per domain...", flush=True)
    con.execute("""
        CREATE TEMP TABLE winners AS
        SELECT domain, count(*) AS domain_places,
               arg_max(fsq_place_id, {'open': date_closed IS NULL,
                                      'unflagged': coalesce(len(unresolved_flags), 0) = 0,
                                      'main_site': host = domain,
                                      'refreshed': coalesce(date_refreshed, '')}) AS fsq_place_id
        FROM site_places
        GROUP BY domain
    """)
    print("Step 2/3: finding directory sites...", flush=True)
    con.execute(f"""
        CREATE TEMP TABLE directories AS
        {directory_sql("(SELECT name, domain FROM site_places "
                       "WHERE domain IN (SELECT domain FROM winners WHERE domain_places >= 20))")}
    """)
    print("Step 3/3: writing the file...", flush=True)
    columns = "p.*" if fmt == "parquet" else csv_safe_select(con, source)
    query = f"""
        SELECT {columns}, w.domain, w.domain_places,
               w.domain IN (SELECT domain FROM directories) AS likely_directory
        FROM read_parquet('{source}') p JOIN winners w USING (fsq_place_id)
    """
    out = out_dir / f"companies.{'parquet' if fmt == 'parquet' else 'csv'}"
    options = "FORMAT PARQUET, COMPRESSION ZSTD" if fmt == "parquet" else "FORMAT CSV, HEADER"
    n = con.execute("SELECT count(*) FROM winners").fetchone()[0]
    d = con.execute("SELECT count(*) FROM directories").fetchone()[0]
    con.execute(f"COPY ({query}) TO '{out}' ({options})")
    print(f"Wrote {out} ({n:,} unique domains, {d:,} flagged likely_directory)")


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
    p.add_argument("--domains-only", action="store_true",
                   help="Only rebuild domains.csv (skip the big places export)")
    p.add_argument("--companies", action="store_true",
                   help="Only write companies.csv: one row per unique website domain, all columns")
    p.add_argument("--source", help="Skip the download and read this local parquet glob instead")
    p.add_argument("--keep-subdomains", action="store_true",
                   help="Use each website host as is (default: main domain, e.g. locations.x.com -> x.com)")
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

    add_site_places(con, args.out_dir, args.keep_subdomains)
    if args.companies:
        export_companies(con, source, args.out_dir, args.format)
        return

    if not args.domains_only:
        if args.format == "parquet":
            out = args.out_dir / "places.parquet"
            con.execute(f"COPY places TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        else:
            out = args.out_dir / "places.csv"
            select = csv_safe_select(con, source)
            con.execute(f"COPY (SELECT {select} FROM places) TO '{out}' (FORMAT CSV, HEADER)")
        print(f"Wrote {out}")

    domains = args.out_dir / "domains.csv"
    con.execute("""
        CREATE TEMP TABLE domain_list AS
        SELECT domain, any_value(country) AS country, count(*) AS places, count(DISTINCT host) AS hosts
        FROM site_places
        GROUP BY domain
        ORDER BY domain  -- sorted, so loading it into the crawl database is fast
    """)
    con.execute(f"COPY domain_list TO '{domains}' (FORMAT CSV, HEADER)")
    n = con.execute("SELECT count(*) FROM domain_list").fetchone()[0]
    print(f"Wrote {domains} ({n:,} unique domains for the LinkedIn crawl)")


if __name__ == "__main__":
    main()
