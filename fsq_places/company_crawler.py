"""Crawl each company website and collect B2B details.

For every domain in output/domains.csv it visits the homepage, then the contact and
about pages, and saves: title, description, site name, language, emails, phones,
LinkedIn/Facebook/Instagram/X/YouTube/TikTok/Pinterest/GitHub/WhatsApp links,
schema.org company data (name, legal name, founding date, address, employees) and
the website's technologies (7,000+ open-source Wappalyzer fingerprints: CMS,
ecommerce, analytics, marketing tools, servers, frameworks...).

Progress lives in SQLite (output/crawl.db): stop with Ctrl+C any time and rerun the
same command to resume. Page analysis runs on all CPU cores automatically; --shard
is only needed to split the work across several machines.

Usage:
    python company_crawler.py init                      # load domains.csv (once)
    python company_crawler.py crawl --limit 1000        # test run
    python company_crawler.py crawl                     # full run, resumable
    python company_crawler.py crawl --shard 0/4         # 4 windows: --shard 0/4 ... --shard 3/4
    python company_crawler.py diagnose                  # test your network, get recommended settings
    python company_crawler.py stats                     # progress and what was found
    python company_crawler.py reset                     # clear all results and start fresh
    python company_crawler.py export                    # output/crawl_results.csv
    python company_crawler.py join --companies C:/Users/Administrator/Documents/companies.csv
"""
import argparse
import asyncio
import csv
import logging
import os
import random
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import aiohttp

from extract import extract, merge
from tech_detect import Detector

USER_AGENT = "Mozilla/5.0 (compatible; CompanyInfoBot/1.0; +https://daddy-leads.com/bot)"
MAX_BYTES = 600_000  # company info sits in the first/last part of a page; less text = less CPU

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
LIVE_COLS = ["domain", "country", "places", *RESULT_COLS]  # order of rows built during a crawl
# Columns written to crawl_live.csv and crawl_results.csv (the database keeps everything)
SITE_COLS = ["final_url", "title", "description", "site_name", "lang", "emails", "phones", *SOCIAL_COLS,
             "org_name", "org_address", "tech", "tech_categories", "generator", "contact_url", "about_url"]
OUTPUT_COLS = ["domain", "country", "places", "status", "http_status", *SITE_COLS, "pages", "error", "crawled_at"]


def as_date(ts) -> str:
    """Unix timestamp -> 'YYYY-MM-DD HH:MM:SS' in local time."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (TypeError, ValueError):
        return ""


def output_row(record: dict) -> list:
    return [as_date(record.get(c)) if c == "crawled_at" else record.get(c) for c in OUTPUT_COLS]


def start_live_file(path: Path) -> Path:
    """Keep appending to an existing live file only if it has the same columns; otherwise move it aside."""
    if not path.exists() or path.stat().st_size == 0:
        return path
    with open(path, newline="", encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    if header == OUTPUT_COLS:
        return path
    old = path.with_name(f"{path.stem}_old_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}")
    try:
        path.rename(old)
        print(f"Columns changed: moved the previous live file to {old.name}")
        return path
    except OSError:  # locked (open in Excel): write to a new file instead
        new = path.with_name(f"{path.stem}_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}")
        print(f"Columns changed and {path.name} is locked; writing to {new.name}")
        return new

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


def cmd_reset(args):
    """Start over: clear every crawl result (keeps the loaded domains, so no new init needed)."""
    con = connect(args.db)
    done = con.execute("SELECT count(*) FROM sites WHERE status IS NOT NULL").fetchone()[0]
    if not args.yes:
        answer = input(f"Clear the results of {done:,} crawled sites and start fresh? Type yes: ")
        if answer.strip().lower() != "yes":
            print("Nothing changed.")
            return
    con.execute(f"UPDATE sites SET {', '.join(f'{c} = NULL' for c in RESULT_COLS)} WHERE status IS NOT NULL")
    con.commit()
    old_dir = args.out_dir / "old_runs"
    moved = 0
    for f in args.out_dir.glob("crawl_live*.csv"):
        old_dir.mkdir(exist_ok=True)
        try:
            f.rename(old_dir / f"{f.stem}_{time.strftime('%Y%m%d_%H%M%S')}{f.suffix}")
            moved += 1
        except OSError:
            print(f"  ! Couldn't move {f.name} (open in Excel?). Close it and move or delete it yourself.")
    print(f"Cleared {done:,} results; all sites are pending again."
          + (f" Moved {moved} old live file(s) to {old_dir}." if moved else ""))


def cmd_stats(args):
    """Two passes over the table (17M rows): status + field coverage, then errors + recent speed."""
    con = connect(args.db)
    fields = ["emails", "phones", "org_address", *SOCIAL_COLS, "org_name", "tech", "contact_url"]
    print("Counting (reads the whole crawl database, ~1-2 minutes on 17M sites)...", flush=True)
    sums = ", ".join(f"sum({c} <> '')" for c in fields)
    rows = con.execute(f"""
        SELECT coalesce(status, 'pending') AS st, count(*), max(CAST(crawled_at AS INTEGER)), {sums}
        FROM sites GROUP BY st ORDER BY 2 DESC
    """).fetchall()
    total = sum(r[1] for r in rows)
    for r in rows:
        print(f"  {r[0]:<10} {r[1]:>12,}")
    print(f"  {'total':<10} {total:>12,}", flush=True)
    by_status = {r[0]: r for r in rows}
    last = max((r[2] for r in rows if r[2]), default=None)
    if "ok" in by_status:
        ok_row = by_status["ok"]
        ok = ok_row[1]
        print(f"\nOf {ok:,} sites crawled successfully, how many had each field:")
        for col, n in zip(fields, ok_row[3:]):
            n = n or 0
            print(f"  {col:<14} {n:>12,}  {n / ok:6.1%}")
    if last is None:
        return

    print("\nCounting errors...", flush=True)
    kinds = con.execute("""
        SELECT CASE WHEN status <> 'error' THEN '' WHEN error LIKE 'HTTP %' THEN error
                    ELSE substr(error, 1, instr(coalesce(error, '?') || ':', ':') - 1) END AS kind,
               count(*), sum(CAST(crawled_at AS INTEGER) >= ?), min(CASE WHEN CAST(crawled_at AS INTEGER) >= ?
                                                                         THEN CAST(crawled_at AS INTEGER) END)
        FROM sites WHERE status IS NOT NULL GROUP BY kind ORDER BY 2 DESC
    """, (last - 600, last - 600)).fetchall()
    errors = [(k, n) for k, n, _, _ in kinds if k != ""][:12]
    if errors:
        n_err = sum(n for k, n, _, _ in kinds if k != "")
        print("Why sites failed:")
        for kind, n in errors:
            print(f"  {kind or 'unknown':<34} {n:>10,}  {n / n_err:6.1%}  {ERROR_HINTS.get(kind, '')}")

    # Speed over the last 10 minutes of activity, so pauses between runs don't skew it
    recent = sum(r[2] or 0 for r in kinds)
    first = min((r[3] for r in kinds if r[3]), default=last)
    pending = by_status.get("pending", (None, 0))[1]
    if last - first >= 30:
        per_sec = recent / (last - first)
        print(f"\nRecent speed {per_sec:.1f} sites/s  ->  {pending:,} pending ≈ {pending / per_sec / 3600:.1f} h"
              f"  (last result {as_date(last)})")


ERROR_HINTS = {
    "ClientConnectorDNSError": "domain doesn't exist any more (or DNS is overloaded)",
    "ClientConnectorError": "server refused or unreachable",
    "ClientConnectorCertificateError": "broken HTTPS certificate",
    "ClientConnectorSSLError": "HTTPS handshake failed",
    "Timeout": "no answer within --timeout (slow/dead site, or your connection is saturated)",
    "TimeoutError": "whole site took too long",
    "ServerDisconnectedError": "server closed the connection",
    "TooManyRedirects": "redirect loop",
    "HTTP 403": "site blocks bots",
    "HTTP 404": "homepage not found",
    "HTTP 429": "rate-limited",
    "HTTP 503": "site down or bot protection",
}


# ---------------------------------------------------------------- crawling

# ---------------------------------------------------------------- proxy list

def parse_proxy(line: str) -> str | None:
    """Accepts ip:port:user:pass (Webshare download format), user:pass@ip:port, ip:port or a full URL."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "://" in line:
        if not line.startswith(("http://", "https://")):
            raise SystemExit(f"Only HTTP proxies are supported (not {line.split('://')[0]}); "
                             "download the HTTP list from your provider")
        return line
    if "@" in line:
        return "http://" + line
    parts = line.split(":")
    if len(parts) == 4:
        ip, port, user, password = parts
        return f"http://{user}:{password}@{ip}:{port}"
    if len(parts) == 2:
        return "http://" + line
    raise SystemExit(f"Can't read proxy line: {line!r}")


class ProxyPool:
    """Picks a random working proxy per site; proxies that keep failing are rested for a while."""

    def __init__(self, proxies: list[str]):
        self.proxies = proxies
        self.fails: Counter = Counter()
        self.rest_until: dict[str, float] = {}

    def pick(self, avoid: set | None = None) -> str:
        now = time.time()
        avoid = avoid or set()
        usable = ([p for p in self.proxies if self.rest_until.get(p, 0) <= now and p not in avoid]
                  or [p for p in self.proxies if p not in avoid] or self.proxies)
        return random.choice(usable)

    def failed(self, proxy: str):
        self.fails[proxy] += 1
        if self.fails[proxy] >= 5:  # 5 proxy errors in a row: rest it for 10 minutes
            self.rest_until[proxy] = time.time() + 600
            self.fails[proxy] = 0

    def worked(self, proxy: str):
        self.fails[proxy] = 0


PROXIES: ProxyPool | None = None
PROXY_ERRORS = (aiohttp.ClientProxyConnectionError, aiohttp.ClientHttpProxyError)


async def fetch(session, url, proxy: str | None = None):
    """-> (status, final url, html or None on HTTP error, headers, cookies).
    With a proxy list, a failing proxy is swapped for another one (up to 3 tries)."""
    tried: set = set()
    for attempt in range(3 if PROXIES and proxy else 1):
        try:
            result = await _fetch(session, url, proxy)
            if PROXIES and proxy:
                PROXIES.worked(proxy)
            return result
        except PROXY_ERRORS:
            if not (PROXIES and proxy) or attempt == 2:
                raise
            PROXIES.failed(proxy)
            tried.add(proxy)
            proxy = PROXIES.pick(avoid=tried)


async def _fetch(session, url, proxy: str | None):
    kwargs = {"proxy": proxy} if proxy else {}
    async with session.get(url, allow_redirects=True, max_redirects=5, **kwargs) as r:
        headers = {k: v for k, v in r.headers.items()}
        cookies = {k: m.value for k, m in r.cookies.items()}
        ctype = r.headers.get("content-type", "")
        if r.status >= 400:
            return r.status, str(r.url), None, headers, cookies
        if "html" not in ctype and "text" not in ctype:
            return r.status, str(r.url), "", headers, cookies
        body = await r.content.read(MAX_BYTES)
        return r.status, str(r.url), body.decode(r.charset or "utf-8", errors="ignore"), headers, cookies


async def robots_allows(session, base_url, proxy: str | None = None) -> bool:
    try:
        kwargs = {"proxy": proxy} if proxy else {}
        async with session.get(urljoin(base_url, "/robots.txt"), allow_redirects=True, **kwargs) as r:
            if r.status >= 400:
                return True
            text = (await r.content.read(200_000)).decode("utf-8", errors="ignore")
    except Exception:
        return True
    rp = RobotFileParser()
    rp.parse(text.splitlines())
    return rp.can_fetch(USER_AGENT, base_url)


# ---------------------------------------------------------------- page analysis (worker processes)
# Parsing and technology matching are CPU work. They run in a process pool so the download loop
# never stalls on a heavy page, and so every CPU core is used.

_DETECTOR: Detector | None = None


def _init_worker(tech_data: str | None):
    global _DETECTOR
    _DETECTOR = Detector(Path(tech_data)) if tech_data else None


def analyze_home(html: str, url: str, headers: dict, cookies: dict):
    home = extract(html, url)
    techs = _DETECTOR.detect(html, url, headers, cookies) if _DETECTOR else {}
    return home, techs, (_DETECTOR.categories(techs) if _DETECTOR else [])


def analyze_sub(html: str, url: str):
    return extract(html, url)


# Pages waiting for (or in) CPU analysis. If this stays far above the number of workers,
# the CPU, not the network, is what limits the speed.
BACKLOG = {"pages": 0}


async def analyze(loop, pool, fn, *fn_args):
    BACKLOG["pages"] += 1
    try:
        return await loop.run_in_executor(pool, fn, *fn_args)
    finally:
        BACKLOG["pages"] -= 1


# ---------------------------------------------------------------- fetching one site

async def crawl_site(session, domain, args, pool) -> dict:
    if is_skipped(domain):
        return {"status": "skipped"}
    loop = asyncio.get_running_loop()
    proxy = PROXIES.pick() if PROXIES else None  # one proxy per site keeps its pages consistent
    last_error, status_code = None, None
    for scheme in ("https", "http"):
        base = f"{scheme}://{domain}/"
        # robots.txt and the homepage are fetched at the same time
        robots = None if args.no_robots else asyncio.create_task(robots_allows(session, base, proxy))
        try:
            status_code, final_url, html, headers, cookies = await fetch(session, base, proxy)
        except asyncio.TimeoutError:
            if robots:
                robots.cancel()
            last_error = "Timeout"
            break  # a site that times out on https almost never answers on http either
        except Exception as e:
            if robots:
                robots.cancel()
            last_error = f"{type(e).__name__}: {e}"[:200]
            if classify_error(e) == "dns":
                break  # the domain doesn't resolve; http:// would fail the same way
            continue
        if robots and not await robots:
            return {"status": "robots", "final_url": base}
        if html is None:
            last_error = f"HTTP {status_code}"
            continue
        home, techs, cats = await analyze(loop, pool, analyze_home, html, final_url, headers, cookies)

        # contact and about pages are fetched at the same time
        sub_urls = [u for kind in ("contact", "about")[: max(args.max_pages - 1, 0)]
                    if (u := home["links"].get(kind)) and u.rstrip("/") != final_url.rstrip("/")]

        async def sub_page(url):
            try:
                _, sub_url, sub_html, _, _ = await fetch(session, url, proxy)
            except Exception:
                return None
            return await analyze(loop, pool, analyze_sub, sub_html, sub_url) if sub_html else None

        pages = [home] + [p for p in await asyncio.gather(*(sub_page(u) for u in sub_urls)) if p]
        info = merge(pages, domain)
        return {
            "status": "ok", "http_status": status_code, "final_url": final_url, "pages": len(pages),
            "contact_url": home["links"].get("contact"), "about_url": home["links"].get("about"),
            "tech": " | ".join(f"{n} {v}".strip() for n, v in sorted(techs.items())) or None,
            "tech_categories": " | ".join(cats) or None,
            **{k: (" | ".join(map(str, v)) if isinstance(v, list) else v) for k, v in info.items()},
        }
    return {"status": "error", "http_status": status_code, "error": last_error}


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0.0.0 Safari/537.36")


def make_headers(user_agent: str = "bot") -> dict:
    """'bot' announces the crawler honestly; 'browser' looks like Chrome (gets past simple bot filters)."""
    if user_agent == "browser":
        return {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9", "Accept-Encoding": "gzip, deflate",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1"}
    return {"User-Agent": USER_AGENT if user_agent == "bot" else user_agent,
            "Accept": "text/html,*/*;q=0.8", "Accept-Language": "en", "Accept-Encoding": "gzip, deflate"}


def make_session(concurrency: int, timeout: float, dns: str, user_agent: str = "bot",
                 proxy: str | None = None) -> aiohttp.ClientSession:
    """dns: 'system' (Windows/router resolver) or 'public' / comma-separated server IPs.
    proxy: e.g. http://user:pass@gate.provider.com:8000 (the proxy then also does the DNS lookups)."""
    resolver = None
    if dns != "system":
        from dns_resolver import PublicDNSResolver
        servers = None if dns == "public" else [x.strip() for x in dns.split(",") if x.strip()]
        resolver = PublicDNSResolver(servers, max_in_flight=max(50, concurrency))
    # up to ~3 requests per site run at once (robots + homepage, then contact + about)
    connector = aiohttp.TCPConnector(limit=concurrency * 3, limit_per_host=3, ttl_dns_cache=300,
                                     ssl=False, resolver=resolver)
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout, connect=min(timeout, 5)),
                                 connector=connector, headers=make_headers(user_agent), proxy=proxy)


# aiohttp logs "Can not load cookies: Illegal cookie name ..." for odd cookies some sites send. Harmless.
logging.getLogger("aiohttp.client").setLevel(logging.ERROR)


def quiet_connection_resets(loop):
    """Windows prints a traceback whenever a server resets a connection while it closes
    (_ProactorBasePipeTransport._call_connection_lost). It's harmless, so hide it."""
    def handler(loop, context):
        if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
            return
        loop.default_exception_handler(context)
    loop.set_exception_handler(handler)


async def run_crawl(args, pool):
    con = connect(args.db)
    if args.retry_errors:
        # only sites that had already failed when this run started, so it can run next to the main crawl
        where = f"status = 'error' AND CAST(crawled_at AS INTEGER) < {int(time.time())}"
        if args.only_blocked:  # sites that answered but refused us: worth retrying with --user-agent/--proxy
            where += " AND (error LIKE 'HTTP 403%' OR error LIKE 'HTTP 429%' OR error LIKE 'HTTP 503%')"
    else:
        where = "status IS NULL"
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
    quiet_connection_resets(asyncio.get_running_loop())
    print(f"DNS: {'system resolver' if args.dns == 'system' else 'public DNS servers'}; "
          f"user agent: {args.user_agent}; "
          f"proxy: {f'{len(PROXIES.proxies):,} from list' if PROXIES else 'yes' if args.proxy else 'no'}")

    queue: asyncio.Queue = asyncio.Queue(maxsize=args.concurrency * 4)
    results: list = []
    stats: Counter = Counter()
    started = time.time()
    update_sql = f"UPDATE sites SET {', '.join(f'{c}=?' for c in RESULT_COLS)} WHERE domain=?"

    # Live CSV on disk, appended every few seconds; one file per shard so windows don't collide
    live_path = start_live_file(Path(args.live) if args.live else args.out_dir / (
        ("crawl_live_retry" if args.retry_errors else "crawl_live")
        + (".csv" if shards == 1 else f"_shard{shard}of{shards}.csv")))
    live_pending: list = []
    live_warned = False
    print(f"Live results file: {live_path.resolve()}")

    def write_live():
        nonlocal live_warned
        if not live_pending:
            return
        new = not live_path.exists() or live_path.stat().st_size == 0
        try:
            with open(live_path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(OUTPUT_COLS)
                w.writerows(live_pending)
            live_pending.clear()
            live_warned = False
        except PermissionError:  # usually the file is open in Excel, which locks it
            if not live_warned:
                print(f"  ! Can't write {live_path.name} (open in Excel?). Close it; rows are kept and "
                      "will be written on the next try.", flush=True)
                live_warned = True

    def flush():
        if results:
            con.executemany(update_sql, [row[3:] for row in results])
            con.commit()
            live_pending.extend(output_row(dict(zip(LIVE_COLS, row[:-1]))) for row in results
                                if args.live_all or row[3] == "ok")
            results.clear()
        write_live()

    async def producer():
        last_rowid, sent = 0, 0
        while sent < target:
            rows = con.execute(
                f"SELECT rowid, domain, country, places FROM sites WHERE rowid > ? AND {where} "
                "ORDER BY rowid LIMIT 5000",
                [last_rowid, *params],
            ).fetchall()
            if not rows:
                break
            for _, domain, country, places in rows:
                if sent >= target:
                    break
                await queue.put((domain, country, places))
                sent += 1
            last_rowid = rows[-1][0]
        for _ in range(args.concurrency):
            await queue.put(None)

    async def worker(session):
        while (item := await queue.get()) is not None:
            domain, country, places = item
            try:
                r = await asyncio.wait_for(crawl_site(session, domain, args, pool), timeout=args.timeout * 3)
            except Exception as e:
                r = {"status": "error", "error": type(e).__name__}
            r["crawled_at"] = int(time.time())
            stats[r["status"]] += 1
            stats["with_email"] += bool(r.get("emails"))
            stats["with_linkedin"] += bool(r.get("linkedin"))
            # [domain, country, places] + result columns + [domain] (last one for the SQL WHERE)
            results.append([domain, country, places] + [r.get(c) for c in RESULT_COLS] + [domain])
            if len(results) >= 500:
                flush()

    async def flusher():
        while True:
            await asyncio.sleep(5)
            flush()

    inline = args.progress_every < 5  # refresh one line in place instead of printing a new line each time
    last_len = 0

    async def reporter():
        nonlocal last_len
        while True:
            await asyncio.sleep(args.progress_every)
            done = sum(stats[s] for s in ("ok", "error", "skipped", "robots"))
            rate = done / max(time.time() - started, 1)
            eta = (target - done) / max(rate, 0.01) / 3600
            backlog = BACKLOG["pages"]
            cpu_note = "  <- CPU-bound: see README speed tips" if backlog > args.workers_used * 6 else ""
            ok_pct = stats["ok"] / done if done else 0
            line = (f"  {done:,}/{target:,}  {rate:.0f}/s  ok={stats['ok']:,} ({ok_pct:.0%})  "
                    f"email={stats['with_email']:,}  linkedin={stats['with_linkedin']:,}  "
                    f"error={stats['error']:,}  cpu-queue={backlog}  ETA {eta:.1f}h{cpu_note}")
            if inline:
                print("\r" + line.ljust(last_len), end="", flush=True)
                last_len = len(line)
            else:
                print(line, flush=True)

    report = asyncio.create_task(reporter())
    flushing = asyncio.create_task(flusher())
    try:
        async with make_session(args.concurrency, args.timeout, args.dns, args.user_agent, args.proxy) as session:
            await asyncio.gather(producer(), *(worker(session) for _ in range(args.concurrency)))
    finally:
        report.cancel()
        flushing.cancel()
        flush()
        done = sum(stats[s] for s in ("ok", "error", "skipped", "robots"))
        print(f"\nThis run: {done:,} sites, {stats['ok']:,} ok, {stats['with_email']:,} with email, "
              f"{stats['with_linkedin']:,} with LinkedIn. Run 'stats' for totals.")


# ---------------------------------------------------------------- network diagnosis

def classify_error(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    if isinstance(e, asyncio.TimeoutError) or "Timeout" in type(e).__name__:
        return "timeout"
    if "DNS" in text or "getaddrinfo" in text or "Name or service not known" in text \
            or "nodename nor servname" in text or "11001" in text:
        return "dns"
    if "SSL" in text or "Certificate" in text or "certificate" in text:
        return "ssl"
    if isinstance(e, (ConnectionResetError, aiohttp.ServerDisconnectedError)) or "10054" in text:
        return "reset"
    if isinstance(e, aiohttp.ClientConnectorError):
        return "refused"
    return "other"


async def probe(domains: list[str], concurrency: int, timeout: float, dns: str) -> dict:
    """Fetch each homepage once (https, then http) and count outcomes."""
    counts: Counter = Counter()
    sem = asyncio.Semaphore(concurrency)
    started = time.time()

    async def one(session, domain):
        async with sem:
            outcome = "other"
            for scheme in ("https", "http"):
                try:
                    async with session.get(f"{scheme}://{domain}/", allow_redirects=True, max_redirects=5) as r:
                        await r.content.read(50_000)
                        outcome = "ok" if r.status < 400 else f"http {r.status // 100}xx"
                        break
                except Exception as e:  # noqa: BLE001 - every failure is a data point here
                    outcome = classify_error(e)
                    if outcome == "timeout":
                        break
            counts[outcome] += 1

    async with make_session(concurrency, timeout, dns) as session:
        await asyncio.gather(*(one(session, d) for d in domains))
    counts["_seconds"] = time.time() - started
    return counts


def cmd_diagnose(args):
    """Try a few hundred real domains with different settings and recommend the best ones."""
    import random

    con = connect(args.db)
    max_rowid = con.execute("SELECT max(rowid) FROM sites").fetchone()[0] or 0
    if not max_rowid:
        sys.exit("No domains loaded. Run init first.")
    configs = [("system", 50), ("system", 300), ("public", 50), ("public", 300), ("public", 800)]
    rng = random.Random(42)
    rowids = rng.sample(range(1, max_rowid + 1), min(max_rowid, args.sample * len(configs) * 2))
    pool = []
    for i in range(0, len(rowids), 900):
        chunk = rowids[i:i + 900]
        pool += [d for (d,) in con.execute(
            f"SELECT domain FROM sites WHERE rowid IN ({','.join('?' * len(chunk))})", chunk)
            if not is_skipped(d)]
    rng.shuffle(pool)
    known = ["google.com", "microsoft.com", "wikipedia.org", "amazon.com", "apple.com",
             "cloudflare.com", "github.com", "bbc.co.uk", "shopify.com", "wordpress.org"]

    async def run_all():
        quiet_connection_resets(asyncio.get_running_loop())
        print("Checking basic internet access with 10 well-known sites...")
        base = await probe(known, 10, args.timeout, "system")
        print(f"  {base['ok']}/10 reachable" + ("" if base["ok"] >= 8 else
              "  <- your internet connection itself has problems (proxy, firewall or antivirus?)"))
        print(f"\nTesting {args.sample} random domains from your list per setting "
              "(takes a few minutes; nothing is saved):")
        print(f"  {'DNS':<8}{'at once':>8}{'ok':>7}{'dns':>7}{'timeout':>9}{'refused':>9}{'reset':>7}"
              f"{'ssl':>6}{'http4/5':>9}{'other':>7}{'sites/s':>9}")
        results = []
        for n, (dns, conc) in enumerate(configs):
            sample = pool[n * args.sample:(n + 1) * args.sample]
            c = await probe(sample, conc, args.timeout, dns)
            total = len(sample)
            pct = {k: c[k] / total for k in ("ok", "dns", "timeout", "refused", "reset", "ssl", "other")}
            pct["http"] = (c["http 4xx"] + c["http 5xx"]) / total
            speed = total / max(c["_seconds"], 0.1)
            results.append((dns, conc, pct["ok"], speed))
            print(f"  {dns:<8}{conc:>8}{pct['ok']:>7.0%}{pct['dns']:>7.0%}{pct['timeout']:>9.0%}"
                  f"{pct['refused']:>9.0%}{pct['reset']:>7.0%}{pct['ssl']:>6.0%}{pct['http']:>9.0%}"
                  f"{pct['other']:>7.0%}{speed:>9.1f}", flush=True)
        best_ok = max(r[2] for r in results)
        good = [r for r in results if r[2] >= best_ok - 0.03]  # nearly as accurate as the best
        dns, conc, ok, _ = max(good, key=lambda r: (r[1], r[3]))
        print(f"\nBest setting: --dns {dns} --concurrency {conc}  ({ok:.0%} of sites reachable)")
        print(f"Run:  python company_crawler.py crawl --dns {dns} --concurrency {conc} --timeout {args.timeout:g}")
        if best_ok < 0.45:
            print("Note: under 45% of random domains load with every setting. Many listed websites are "
                  "dead, but if 'timeout' or 'reset' is high your network/router is the limit; a cloud "
                  "server (VPS) would be much faster.")

    asyncio.run(run_all())


def cmd_crawl(args):
    global PROXIES
    if args.only_blocked and not args.retry_errors:
        sys.exit("--only-blocked works together with --retry-errors")
    if args.proxy_file:
        lines = Path(args.proxy_file).read_text(encoding="utf-8-sig").splitlines()
        proxies = [p for line in lines if (p := parse_proxy(line))]
        if not proxies:
            sys.exit(f"No proxies found in {args.proxy_file}")
        PROXIES = ProxyPool(proxies)
        print(f"Loaded {len(proxies):,} proxies from {args.proxy_file}")
    workers = args.workers_used = args.workers or max(1, (os.cpu_count() or 2) - 1)
    tech_data = None if args.no_tech else args.tech_data
    if tech_data:
        Detector(Path(tech_data))  # download fingerprints once, before the workers start
    print(f"Page analysis on {workers} CPU worker(s); technology detection {'on' if tech_data else 'off'}")
    with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(tech_data,)) as pool:
        try:
            asyncio.run(run_crawl(args, pool))
        except KeyboardInterrupt:
            print("\nStopped. Progress is saved; run the same command to resume.")


def cmd_export(args):
    con = connect(args.db)
    out = args.out_dir / "crawl_results.csv"
    n = 0
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(OUTPUT_COLS)
        cur = con.execute(f"SELECT {', '.join(OUTPUT_COLS)} FROM sites WHERE status = 'ok'")
        for row in cur:
            w.writerow(output_row(dict(zip(OUTPUT_COLS, row))))
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
                     for col in SITE_COLS)
    con.execute(f"""
        COPY (
            SELECT c.*, r.status AS crawl_status, r.crawled_at AS site_crawled_at, {cols}
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
    s.add_argument("--concurrency", type=int, default=500, help="Sites fetched at the same time")
    s.add_argument("--timeout", type=int, default=10, help="Seconds per request")
    s.add_argument("--workers", type=int, help="CPU processes for page analysis (default: cores - 1)")
    s.add_argument("--max-pages", type=int, default=3, help="Pages per site: homepage + contact + about")
    s.add_argument("--limit", type=int, help="Stop after this many sites (for test runs)")
    s.add_argument("--country", nargs="*", help="Only sites from these countries, e.g. US GB")
    s.add_argument("--shard", default="0/1", help="Split work across windows: 0/4, 1/4, 2/4, 3/4")
    s.add_argument("--retry-errors", action="store_true",
                   help="Retry only sites that failed before this run started (safe next to the main crawl; "
                        "writes crawl_live_retry.csv)")
    s.add_argument("--only-blocked", action="store_true",
                   help="With --retry-errors: only retry sites that blocked us (HTTP 403/429/503)")
    s.add_argument("--user-agent", default="bot",
                   help="'bot' (default, honest crawler name), 'browser' (looks like Chrome) or a custom string")
    s.add_argument("--proxy", help="Proxy URL, e.g. http://user:pass@gate.provider.com:8000")
    s.add_argument("--proxy-file", help="Text file with one proxy per line (ip:port:user:pass, e.g. a "
                                        "Webshare list); each site uses a random one")
    s.add_argument("--progress-every", type=float, default=15,
                   help="Seconds between progress updates (under 5 refreshes one line in place)")
    s.add_argument("--no-robots", action="store_true", help="Don't check robots.txt (faster, less polite)")
    s.add_argument("--no-tech", action="store_true", help="Skip technology detection (faster)")
    s.add_argument("--dns", default="system",
                   help="'system' (default), 'public' (Cloudflare/Google/Quad9) or comma-separated DNS server IPs")
    s.add_argument("--live", help="Live CSV to append results to (default: output/crawl_live.csv)")
    s.add_argument("--live-all", action="store_true", help="Also write failed/skipped sites to the live CSV")
    s.add_argument("--tech-data", default="data/webappanalyzer", help="Where Wappalyzer fingerprints are cached")
    s.set_defaults(func=cmd_crawl)

    sub.add_parser("stats", help="Show progress and field coverage").set_defaults(func=cmd_stats)

    s = sub.add_parser("diagnose", help="Test your network with different settings and recommend the best")
    s.add_argument("--sample", type=int, default=300, help="Domains tested per setting")
    s.add_argument("--timeout", type=float, default=8)
    s.set_defaults(func=cmd_diagnose)

    s = sub.add_parser("reset", help="Clear all crawl results and start fresh (no new init needed)")
    s.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    s.set_defaults(func=cmd_reset)
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
