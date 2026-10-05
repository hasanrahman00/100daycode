"""Find each company's LinkedIn page by crawling its own website.

Reads output/domains.csv (from export_places.py), visits each site's homepage
(plus up to two about/contact pages when the homepage has no link) and saves the
linkedin.com/company/... URL it finds. Progress lives in a SQLite file, so you
can stop with Ctrl+C at any time and run the same command again to resume.

Usage:
    python linkedin_crawler.py init                     # load domains.csv into output/crawl.db (once)
    python linkedin_crawler.py crawl --limit 1000       # small test run first
    python linkedin_crawler.py crawl                    # full run, resumable
    python linkedin_crawler.py stats                    # progress and hit rate
    python linkedin_crawler.py export                   # output/linkedin.csv (domain -> LinkedIn URL)
    python linkedin_crawler.py join                     # output/places_with_linkedin.csv
"""
import argparse
import asyncio
import csv
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import aiohttp

USER_AGENT = "Mozilla/5.0 (compatible; CompanyLinkFinder/1.0; +https://daddy-leads.com/bot)"
MAX_BYTES = 1_500_000

# Matches plain and JSON-escaped links: linkedin.com/company/acme or linkedin.com\/company\/acme
LINKEDIN_RE = re.compile(r"linkedin\.com(?:\\?/)+company(?:\\?/)+([A-Za-z0-9\-_.%~]+)", re.I)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
SUBPAGE_RE = re.compile(
    r"about|contact|impressum|kontakt|team|company|empresa|contacto|nosotros|uber-uns|qui-sommes|chi-siamo",
    re.I,
)
# Slugs that show up in share buttons and templates, not real company pages
BAD_SLUGS = {"linkedin", "company", "your-company", "yourcompany", "companyname", "example", "share"}

# Platforms that are not a company's own website
SKIP_HOSTS = (
    "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "google.com", "goo.gl", "g.page",
    "bit.ly", "tinyurl.com", "linktr.ee", "wa.me", "whatsapp.com", "t.me", "yelp.com",
    "tripadvisor.com", "booking.com", "airbnb.com", "ubereats.com", "doordash.com", "grubhub.com",
    "foursquare.com", "apple.com", "amazon.com", "vk.com", "ok.ru", "line.me",
)


def is_skipped(domain: str) -> bool:
    return any(domain == h or domain.endswith("." + h) for h in SKIP_HOSTS)


def find_linkedin(html: str) -> str | None:
    slugs = []
    for m in LINKEDIN_RE.finditer(html):
        slug = m.group(1).rstrip(".\\").lower()
        if slug and slug not in BAD_SLUGS and "%" not in slug:
            slugs.append(slug)
    if not slugs:
        return None
    slug = Counter(slugs).most_common(1)[0][0]  # the page's own link usually appears most often
    return f"https://www.linkedin.com/company/{slug}"


def subpage_links(html: str, base_url: str, limit: int = 2) -> list[str]:
    host = urlsplit(base_url).hostname
    links = []
    for href in HREF_RE.findall(html):
        url = urljoin(base_url, href.strip())
        if urlsplit(url).hostname == host and SUBPAGE_RE.search(urlsplit(url).path) and url not in links:
            links.append(url)
            if len(links) == limit:
                break
    return links


# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
    domain TEXT PRIMARY KEY,
    country TEXT,
    places INTEGER,
    status TEXT,          -- NULL = pending, found, none, robots, skipped, error
    linkedin_url TEXT,
    final_url TEXT,
    error TEXT,
    crawled_at INTEGER
);
"""


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def cmd_init(args):
    con = connect(args.db)
    t = time.time()
    with open(args.domains, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        batch, total = [], 0
        for row in reader:
            batch.append((row["domain"], row["country"], int(row["places"] or 0)))
            if len(batch) == 50_000:
                con.executemany("INSERT OR IGNORE INTO domains(domain, country, places) VALUES (?,?,?)", batch)
                con.commit()
                total += len(batch)
                batch = []
                print(f"\r  loaded {total:,}", end="", flush=True)
        con.executemany("INSERT OR IGNORE INTO domains(domain, country, places) VALUES (?,?,?)", batch)
        con.commit()
        total += len(batch)
    print(f"\rLoaded {total:,} domains into {args.db} in {time.time() - t:.0f}s")


def cmd_stats(args):
    con = connect(args.db)
    rows = con.execute("SELECT coalesce(status, 'pending'), count(*) FROM domains GROUP BY 1 ORDER BY 2 DESC").fetchall()
    total = sum(n for _, n in rows)
    done = sum(n for s, n in rows if s != "pending")
    found = dict(rows).get("found", 0)
    for s, n in rows:
        print(f"  {s:<10} {n:>12,}")
    print(f"  {'total':<10} {total:>12,}")
    if done:
        print(f"\nDone {done / total:.1%}  |  LinkedIn found on {found / done:.1%} of crawled domains")


# ---------------------------------------------------------------- crawling

async def fetch(session, url):
    async with session.get(url, allow_redirects=True, max_redirects=5) as r:
        ctype = r.headers.get("content-type", "")
        if r.status >= 400:
            raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message="")
        if "html" not in ctype and "text" not in ctype:
            return str(r.url), ""
        body = await r.content.read(MAX_BYTES)
        return str(r.url), body.decode(r.get_encoding() if r.charset else "utf-8", errors="ignore")


async def robots_allows(session, base_url) -> bool:
    try:
        async with session.get(urljoin(base_url, "/robots.txt"), allow_redirects=True) as r:
            if r.status >= 400:
                return True
            text = (await r.content.read(200_000)).decode("utf-8", errors="ignore")
    except Exception:
        return True
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    return rp.can_fetch(USER_AGENT, base_url)


async def crawl_domain(session, domain, check_robots):
    if is_skipped(domain):
        return "skipped", None, None, None
    last_error = None
    for scheme in ("https", "http"):
        base = f"{scheme}://{domain}/"
        try:
            if check_robots and not await robots_allows(session, base):
                return "robots", None, base, None
            final_url, html = await fetch(session, base)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"[:200]
            continue
        if url := find_linkedin(html):
            return "found", url, final_url, None
        for sub in subpage_links(html, final_url):
            try:
                _, sub_html = await fetch(session, sub)
            except Exception:
                continue
            if url := find_linkedin(sub_html):
                return "found", url, final_url, None
        return "none", None, final_url, None
    return "error", None, None, last_error


async def run_crawl(args):
    con = connect(args.db)
    where = "status IS NULL"
    params = []
    if args.country:
        where += f" AND country IN ({','.join('?' * len(args.country))})"
        params += [c.upper() for c in args.country]
    if args.retry_errors:
        where = where.replace("status IS NULL", "(status IS NULL OR status = 'error')")
    pending = con.execute(f"SELECT count(*) FROM domains WHERE {where}", params).fetchone()[0]
    target = min(pending, args.limit) if args.limit else pending
    print(f"{pending:,} domains to crawl; this run: {target:,}  (concurrency {args.concurrency})")
    if not target:
        return

    # DNS lookups run in threads; the default pool is too small for hundreds of connections
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max(64, args.concurrency)))
    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)
    results = []
    stats = Counter()
    started = time.time()

    def flush():
        if results:
            con.executemany(
                "UPDATE domains SET status=?, linkedin_url=?, final_url=?, error=?, crawled_at=? WHERE domain=?",
                results,
            )
            con.commit()
            results.clear()

    async def producer():
        last_rowid, sent = 0, 0
        while sent < target:
            rows = con.execute(
                f"SELECT rowid, domain FROM domains WHERE rowid > ? AND {where} ORDER BY rowid LIMIT 5000",
                [last_rowid, *params],
            ).fetchall()
            if not rows:
                break
            for rowid, domain in rows:
                if sent >= target:
                    break
                await queue.put(domain)
                sent += 1
            last_rowid = rows[-1][0]
        for _ in range(args.concurrency):
            await queue.put(None)

    async def worker(session):
        while (domain := await queue.get()) is not None:
            try:
                status, url, final_url, error = await asyncio.wait_for(
                    crawl_domain(session, domain, not args.no_robots), timeout=args.timeout * 4
                )
            except Exception as e:
                status, url, final_url, error = "error", None, None, f"{type(e).__name__}"
            stats[status] += 1
            results.append((status, url, final_url, error, int(time.time()), domain))
            if len(results) >= 1000:
                flush()

    async def reporter():
        while True:
            await asyncio.sleep(15)
            done = sum(stats.values())
            rate = done / max(time.time() - started, 1)
            eta_h = (target - done) / max(rate, 0.01) / 3600
            print(f"  {done:,}/{target:,}  {rate:.0f}/s  found={stats['found']:,}  none={stats['none']:,}  "
                  f"error={stats['error']:,}  ETA {eta_h:.1f}h", flush=True)

    timeout = aiohttp.ClientTimeout(total=args.timeout, connect=min(args.timeout, 8))
    connector = aiohttp.TCPConnector(limit=args.concurrency, limit_per_host=2, ttl_dns_cache=300, ssl=False)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en"}
    report = asyncio.create_task(reporter())
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
            await asyncio.gather(producer(), *(worker(session) for _ in range(args.concurrency)))
    finally:
        report.cancel()
        flush()
        done = sum(stats.values())
        print(f"\nThis run: {done:,} crawled, {stats['found']:,} LinkedIn pages found. Run 'stats' for totals.")


def cmd_crawl(args):
    try:
        asyncio.run(run_crawl(args))
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run the same command to resume.")


# ---------------------------------------------------------------- output

def cmd_export(args):
    con = connect(args.db)
    out = args.out_dir / "linkedin.csv"
    rows = con.execute(
        "SELECT domain, linkedin_url, country, places FROM domains WHERE status = 'found' ORDER BY places DESC"
    )
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["domain", "linkedin_url", "country", "places"])
        n = 0
        for row in rows:
            w.writerow(row)
            n += 1
    print(f"Wrote {out} ({n:,} domains with a LinkedIn page)")


def cmd_join(args):
    import duckdb
    from export_places import DOMAIN_SQL, csv_safe_select, directory_sql

    cmd_export(args)
    linkedin = args.out_dir / "linkedin.csv"
    out = args.out_dir / "places_with_linkedin.csv"
    source = args.source or str(next(Path(args.data_dir).glob("release/dt=*/places/parquet")) / "*.parquet")
    con = duckdb.connect()
    select = csv_safe_select(con, source)
    con.execute(f"CREATE VIEW p AS SELECT *, {DOMAIN_SQL} AS domain FROM read_parquet('{source}')")
    con.execute(f"""
        CREATE TEMP TABLE l AS
        SELECT domain, linkedin_url FROM read_csv('{linkedin}', header=true, quote='"', columns={{
            'domain': 'VARCHAR', 'linkedin_url': 'VARCHAR', 'country': 'VARCHAR', 'places': 'BIGINT'}})
    """)

    # Directory sites (gelbeseiten.de, yelp-style listings) appear as the "website" of thousands of
    # unrelated businesses; their LinkedIn page belongs to the directory, not to those businesses.
    # Chains (walmart.com) are told apart because most of their place names contain the brand.
    con.execute(f"""
        CREATE TEMP TABLE directories AS
        {directory_sql("p WHERE domain IN (SELECT domain FROM l)", args.min_places, args.min_brand_share)}
    """)
    dirs = con.execute("SELECT domain, places, brand_share FROM directories ORDER BY places DESC").fetchall()
    if dirs:
        print(f"Not attaching LinkedIn to {len(dirs):,} directory-like domains, e.g.:")
        for d, n, share in dirs[:10]:
            print(f"  {d:<40} {n:>9,} places, {share:.0%} named after the domain")
        con.execute(f"COPY directories TO '{args.out_dir / 'directory_domains.csv'}' (FORMAT CSV, HEADER)")

    con.execute(f"""
        COPY (
            SELECT {select}, l.linkedin_url
            FROM p JOIN l USING (domain)
            WHERE domain NOT IN (SELECT domain FROM directories)
        ) TO '{out}' (FORMAT CSV, HEADER)
    """)
    n = con.execute(f"SELECT count(*) FROM read_csv('{out}', header=true)").fetchone()[0]
    print(f"Wrote {out} ({n:,} places with a LinkedIn company page, all columns + linkedin_url)")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=Path("output/crawl.db"))
    p.add_argument("--out-dir", type=Path, default=Path("output"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Load domains.csv into the crawl database")
    s.add_argument("--domains", type=Path, default=Path("output/domains.csv"))
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("crawl", help="Crawl pending domains (resumable)")
    s.add_argument("--concurrency", type=int, default=300, help="Sites fetched at the same time")
    s.add_argument("--timeout", type=int, default=15, help="Seconds per request")
    s.add_argument("--limit", type=int, help="Stop after this many domains (for test runs)")
    s.add_argument("--country", nargs="*", help="Only domains from these countries, e.g. US GB")
    s.add_argument("--retry-errors", action="store_true", help="Also retry domains that failed before")
    s.add_argument("--no-robots", action="store_true", help="Don't check robots.txt (faster, less polite)")
    s.set_defaults(func=cmd_crawl)

    sub.add_parser("stats", help="Show progress").set_defaults(func=cmd_stats)
    sub.add_parser("export", help="Write output/linkedin.csv").set_defaults(func=cmd_export)

    s = sub.add_parser("join", help="Write places_with_linkedin.csv: every FSQ column + linkedin_url")
    s.add_argument("--data-dir", default="data")
    s.add_argument("--source", help="Parquet glob to use instead of the downloaded release")
    s.add_argument("--min-places", type=int, default=20,
                   help="Only check domains with at least this many places for being a directory")
    s.add_argument("--min-brand-share", type=float, default=0.2,
                   help="Below this share of place names containing the domain's brand = directory")
    s.set_defaults(func=cmd_join)

    args = p.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    args.func(args)


if __name__ == "__main__":
    main()
