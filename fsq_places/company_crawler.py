"""Crawl each company website and collect B2B details.

For every domain in output/domains.csv it visits the homepage, then the contact and
about pages, and saves: title, description, site name, language, emails, phones,
LinkedIn/Facebook/Instagram/X/YouTube/TikTok/Pinterest/GitHub/WhatsApp links,
schema.org company data (name, legal name, founding date, address, employees) and
the website's technologies (7,000+ open-source Wappalyzer fingerprints: CMS,
ecommerce, analytics, marketing tools, servers, frameworks...).

Progress lives in SQLite (output/crawl.db): stop with Ctrl+C any time and rerun the
same command to resume. Run several copies with --shard to use more CPU cores.

Usage:
    python company_crawler.py init                      # load domains.csv (once)
    python company_crawler.py crawl --limit 1000        # test run
    python company_crawler.py crawl                     # full run, resumable
    python company_crawler.py crawl --shard 0/4         # 4 windows: --shard 0/4 ... --shard 3/4
    python company_crawler.py stats                     # progress and what was found
    python company_crawler.py export                    # output/crawl_results.csv
    python company_crawler.py join --companies C:/Users/Administrator/Documents/companies.csv
"""
import argparse
import asyncio
import csv
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import aiohttp

from extract import extract, merge
from tech_detect import Detector

USER_AGENT = "Mozilla/5.0 (compatible; CompanyInfoBot/1.0; +https://daddy-leads.com/bot)"
MAX_BYTES = 1_000_000

# Platforms that are not a company's own website
SKIP_HOSTS = (
    "facebook.com", "fb.com", "fb.me", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "pinterest.com", "google.com", "goo.gl", "g.page",
    "bit.ly", "tinyurl.com", "linktr.ee", "wa.me", "whatsapp.com", "t.me", "yelp.com",
    "tripadvisor.com", "booking.com", "airbnb.com", "ubereats.com", "doordash.com", "grubhub.com",
    "foursquare.com", "apple.com", "amazon.com", "vk.com", "ok.ru", "line.me",
)

SOCIAL_COLS = ["linkedin", "facebook", "instagram", "twitter", "youtube", "tiktok", "pinterest", "github", "whatsapp"]
INFO_COLS = ["title", "description", "site_name", "lang", "emails", "phones", *SOCIAL_COLS,
             "org_name", "legal_name", "founding_date", "org_address", "employees",
             "tech", "tech_categories", "generator",
             "contact_url", "about_url"]
RESULT_COLS = ["status", "http_status", "final_url", *INFO_COLS, "pages", "error", "crawled_at"]

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS sites (
    domain TEXT PRIMARY KEY,
    country TEXT,
    places INTEGER,
    {", ".join(f"{c} TEXT" for c in RESULT_COLS)}
);
"""
# status: NULL = pending, ok, skipped, robots, error


def is_skipped(domain: str) -> bool:
    return any(domain == h or domain.endswith("." + h) for h in SKIP_HOSTS)


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=120)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- setup and progress

def cmd_init(args):
    con = connect(args.db)
    t = time.time()
    total = 0
    with open(args.domains, newline="", encoding="utf-8") as f:
        batch = []
        for row in csv.DictReader(f):
            batch.append((row["domain"], row["country"], int(row["places"] or 0)))
            if len(batch) == 50_000:
                con.executemany("INSERT OR IGNORE INTO sites(domain, country, places) VALUES (?,?,?)", batch)
                con.commit()
                total += len(batch)
                batch = []
                print(f"\r  loaded {total:,}", end="", flush=True)
        con.executemany("INSERT OR IGNORE INTO sites(domain, country, places) VALUES (?,?,?)", batch)
        con.commit()
        total += len(batch)
    print(f"\rLoaded {total:,} domains into {args.db} in {time.time() - t:.0f}s")


def cmd_stats(args):
    con = connect(args.db)
    rows = con.execute("SELECT coalesce(status, 'pending'), count(*) FROM sites GROUP BY 1 ORDER BY 2 DESC").fetchall()
    total = sum(n for _, n in rows)
    for s, n in rows:
        print(f"  {s:<10} {n:>12,}")
    print(f"  {'total':<10} {total:>12,}")
    ok = dict(rows).get("ok", 0)
    if not ok:
        return
    print(f"\nOf {ok:,} sites crawled successfully, how many had each field:")
    for col in ["emails", "phones", *SOCIAL_COLS, "org_name", "founding_date", "employees", "tech"]:
        n = con.execute(f"SELECT count(*) FROM sites WHERE status='ok' AND {col} IS NOT NULL AND {col} <> ''").fetchone()[0]
        print(f"  {col:<14} {n:>12,}  {n / ok:6.1%}")
    rate = con.execute("SELECT count(*), max(crawled_at) - min(crawled_at) FROM sites WHERE crawled_at IS NOT NULL").fetchone()
    pending = dict(rows).get("pending", 0)
    if rate[1] and int(rate[1]) > 60:
        per_sec = rate[0] / int(rate[1])
        print(f"\nAverage speed {per_sec:.0f} sites/s  ->  {pending:,} pending ≈ {pending / per_sec / 3600:.1f} h")


# ---------------------------------------------------------------- crawling

async def fetch(session, url):
    """-> (status, final url, html or None on HTTP error, headers, cookies)"""
    async with session.get(url, allow_redirects=True, max_redirects=5) as r:
        headers = {k: v for k, v in r.headers.items()}
        cookies = {k: m.value for k, m in r.cookies.items()}
        ctype = r.headers.get("content-type", "")
        if r.status >= 400:
            return r.status, str(r.url), None, headers, cookies
        if "html" not in ctype and "text" not in ctype:
            return r.status, str(r.url), "", headers, cookies
        body = await r.content.read(MAX_BYTES)
        return r.status, str(r.url), body.decode(r.charset or "utf-8", errors="ignore"), headers, cookies


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


async def crawl_site(session, domain, args) -> dict:
    if is_skipped(domain):
        return {"status": "skipped"}
    last_error, status_code = None, None
    for scheme in ("https", "http"):
        base = f"{scheme}://{domain}/"
        try:
            if not args.no_robots and not await robots_allows(session, base):
                return {"status": "robots", "final_url": base}
            status_code, final_url, html, headers, cookies = await fetch(session, base)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"[:200]
            continue
        if html is None:
            last_error = f"HTTP {status_code}"
            continue
        home = extract(html, final_url)
        techs = DETECTOR.detect(html, final_url, headers, cookies) if DETECTOR else {}
        pages = [home]
        for kind in ("contact", "about")[: max(args.max_pages - 1, 0)]:
            url = home["links"].get(kind)
            if not url or url.rstrip("/") == final_url.rstrip("/"):
                continue
            try:
                _, sub_url, sub_html, _, _ = await fetch(session, url)
            except Exception:
                continue
            if sub_html:
                pages.append(extract(sub_html, sub_url))
        info = merge(pages, domain)
        return {
            "status": "ok", "http_status": status_code, "final_url": final_url, "pages": len(pages),
            "contact_url": home["links"].get("contact"), "about_url": home["links"].get("about"),
            "tech": " | ".join(f"{n} {v}".strip() for n, v in sorted(techs.items())) or None,
            "tech_categories": (" | ".join(DETECTOR.categories(techs)) or None) if DETECTOR else None,
            **{k: (" | ".join(map(str, v)) if isinstance(v, list) else v) for k, v in info.items()},
        }
    return {"status": "error", "http_status": status_code, "error": last_error}


async def run_crawl(args):
    con = connect(args.db)
    where = "(status IS NULL OR status = 'error')" if args.retry_errors else "status IS NULL"
    params: list = []
    if args.country:
        where += f" AND country IN ({','.join('?' * len(args.country))})"
        params += [c.upper() for c in args.country]
    shard, shards = map(int, args.shard.split("/"))
    if shards > 1:
        where += f" AND rowid % {shards} = {shard}"
    pending = con.execute(f"SELECT count(*) FROM sites WHERE {where}", params).fetchone()[0]
    target = min(pending, args.limit) if args.limit else pending
    print(f"{pending:,} sites to crawl in this shard; this run: {target:,} (concurrency {args.concurrency})")
    if not target:
        return

    # DNS lookups run in threads; the default pool is too small for hundreds of connections
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max(64, args.concurrency)))
    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)
    results: list = []
    stats: Counter = Counter()
    started = time.time()
    update_sql = f"UPDATE sites SET {', '.join(f'{c}=?' for c in RESULT_COLS)} WHERE domain=?"

    def flush():
        if results:
            con.executemany(update_sql, results)
            con.commit()
            results.clear()

    async def producer():
        last_rowid, sent = 0, 0
        while sent < target:
            rows = con.execute(
                f"SELECT rowid, domain FROM sites WHERE rowid > ? AND {where} ORDER BY rowid LIMIT 5000",
                [last_rowid, *params],
            ).fetchall()
            if not rows:
                break
            for _, domain in rows:
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
                r = await asyncio.wait_for(crawl_site(session, domain, args), timeout=args.timeout * 4)
            except Exception as e:
                r = {"status": "error", "error": type(e).__name__}
            r["crawled_at"] = int(time.time())
            stats[r["status"]] += 1
            stats["with_email"] += bool(r.get("emails"))
            stats["with_linkedin"] += bool(r.get("linkedin"))
            results.append([r.get(c) for c in RESULT_COLS] + [domain])
            if len(results) >= 500:
                flush()

    async def reporter():
        while True:
            await asyncio.sleep(15)
            done = sum(stats[s] for s in ("ok", "error", "skipped", "robots"))
            rate = done / max(time.time() - started, 1)
            eta = (target - done) / max(rate, 0.01) / 3600
            print(f"  {done:,}/{target:,}  {rate:.0f}/s  ok={stats['ok']:,}  email={stats['with_email']:,}  "
                  f"linkedin={stats['with_linkedin']:,}  error={stats['error']:,}  ETA {eta:.1f}h", flush=True)

    timeout = aiohttp.ClientTimeout(total=args.timeout, connect=min(args.timeout, 8))
    connector = aiohttp.TCPConnector(limit=args.concurrency, limit_per_host=2, ttl_dns_cache=300, ssl=False)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.8",
               "Accept-Language": "en", "Accept-Encoding": "gzip, deflate"}
    report = asyncio.create_task(reporter())
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
            await asyncio.gather(producer(), *(worker(session) for _ in range(args.concurrency)))
    finally:
        report.cancel()
        flush()
        done = sum(stats[s] for s in ("ok", "error", "skipped", "robots"))
        print(f"\nThis run: {done:,} sites, {stats['ok']:,} ok, {stats['with_email']:,} with email, "
              f"{stats['with_linkedin']:,} with LinkedIn. Run 'stats' for totals.")


DETECTOR: Detector | None = None


def cmd_crawl(args):
    global DETECTOR
    if not args.no_tech:
        DETECTOR = Detector(Path(args.tech_data))
        print(f"Technology detection: {len(DETECTOR.techs):,} fingerprints loaded")
    try:
        asyncio.run(run_crawl(args))
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved; run the same command to resume.")


# ---------------------------------------------------------------- output

def cmd_export(args):
    con = connect(args.db)
    out = args.out_dir / "crawl_results.csv"
    cols = ["domain", *RESULT_COLS]
    n = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for row in con.execute(f"SELECT {', '.join(cols)} FROM sites WHERE status = 'ok'"):
            w.writerow(row)
            n += 1
    print(f"Wrote {out} ({n:,} crawled sites)")
    return out


def cmd_join(args):
    """companies.csv + crawl results -> companies_enriched.csv (one row per company, all columns).
    Directory sites (likely_directory) don't pass their own details to the businesses they list."""
    import duckdb

    results = cmd_export(args)
    out = args.out_dir / "companies_enriched.csv"
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    cols = ", ".join(f"CASE WHEN lower(coalesce(c.likely_directory, 'false')) <> 'true' THEN r.{col} END AS site_{col}"
                     for col in ["final_url", *INFO_COLS])
    con.execute(f"""
        COPY (
            SELECT c.*, r.status AS crawl_status, {cols}
            FROM read_csv('{args.companies}', header=true, quote='"', all_varchar=true) c
            LEFT JOIN read_csv('{results}', header=true, quote='"', all_varchar=true) r USING (domain)
        ) TO '{out}' (FORMAT CSV, HEADER)
    """)
    print(f"Wrote {out}: every companies.csv column + site_* columns from the crawl")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", type=Path, default=Path("output/crawl.db"))
    p.add_argument("--out-dir", type=Path, default=Path("output"))
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="Load domains.csv into the crawl database")
    s.add_argument("--domains", type=Path, default=Path("output/domains.csv"))
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("crawl", help="Crawl pending sites (resumable)")
    s.add_argument("--concurrency", type=int, default=200, help="Sites fetched at the same time")
    s.add_argument("--timeout", type=int, default=15, help="Seconds per request")
    s.add_argument("--max-pages", type=int, default=3, help="Pages per site: homepage + contact + about")
    s.add_argument("--limit", type=int, help="Stop after this many sites (for test runs)")
    s.add_argument("--country", nargs="*", help="Only sites from these countries, e.g. US GB")
    s.add_argument("--shard", default="0/1", help="Split work across windows: 0/4, 1/4, 2/4, 3/4")
    s.add_argument("--retry-errors", action="store_true", help="Also retry sites that failed before")
    s.add_argument("--no-robots", action="store_true", help="Don't check robots.txt (faster, less polite)")
    s.add_argument("--no-tech", action="store_true", help="Skip technology detection (faster)")
    s.add_argument("--tech-data", default="data/webappanalyzer", help="Where Wappalyzer fingerprints are cached")
    s.set_defaults(func=cmd_crawl)

    sub.add_parser("stats", help="Show progress and field coverage").set_defaults(func=cmd_stats)
    sub.add_parser("export", help="Write output/crawl_results.csv").set_defaults(func=cmd_export)

    s = sub.add_parser("join", help="Write output/companies_enriched.csv")
    s.add_argument("--companies", default="output/companies.csv", help="Path to companies.csv")
    s.set_defaults(func=cmd_join)

    args = p.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    args.func(args)


if __name__ == "__main__":
    main()
