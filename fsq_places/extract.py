"""Pull B2B company details out of a website's HTML.

Pure functions with no network access, so they are easy to test and tweak.
`extract(html, url)` returns a dict of everything found on one page, and
`merge(pages)` combines the dicts from a site's homepage and contact/about pages.
"""
import html as htmllib
import json
import re
from collections import Counter
from urllib.parse import unquote, urljoin, urlsplit

# ---------------------------------------------------------------- social profiles

# platform -> (pattern capturing the profile part, path prefixes that are share/tracking links)
SOCIAL = {
    "linkedin": (r"linkedin\.com/(company|school|showcase)/([A-Za-z0-9\-_.%~]+)", ()),
    "facebook": (r"(?:facebook|fb)\.com/([A-Za-z0-9.\-_]+(?:/[0-9]{6,})?)",
                 ("sharer", "share", "plugins", "tr", "dialog", "login", "groups", "events", "watch",
                  "photo", "photos", "profile.php", "pages", "hashtag", "help", "policies", "privacy", "permalink")),
    "instagram": (r"instagram\.com/([A-Za-z0-9._]+)", ("p", "explore", "reel", "reels", "stories", "accounts", "tv")),
    "twitter": (r"(?:twitter|x)\.com/([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_.])",
                ("intent", "share", "home", "hashtag", "search", "i", "login", "privacy", "tos", "widgets")),
    "youtube": (r"youtube\.com/((?:channel|c|user)/[A-Za-z0-9\-_]+|@[A-Za-z0-9\-_.]+)", ()),
    "tiktok": (r"tiktok\.com/(@[A-Za-z0-9._]+)", ()),
    "pinterest": (r"pinterest\.[a-z.]+/([A-Za-z0-9_]+)", ("pin", "search", "ideas")),
    "github": (r"github\.com/([A-Za-z0-9\-]+)", ("sponsors", "features", "about", "login", "orgs", "topics")),
    "whatsapp": (r"(?:wa\.me/|api\.whatsapp\.com/send/?\?phone=)(\+?[0-9]{7,15})", ()),
}
SOCIAL_RE = {k: re.compile(p, re.I) for k, (p, _) in SOCIAL.items()}
BAD_SLUGS = {"linkedin", "company", "your-company", "yourcompany", "companyname", "example", "share",
             "username", "yourpage", "youraccount", "yourname", "user", "page", "account"}
SOCIAL_BASE = {
    "linkedin": "https://www.linkedin.com/", "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/", "twitter": "https://x.com/",
    "youtube": "https://www.youtube.com/", "tiktok": "https://www.tiktok.com/",
    "pinterest": "https://www.pinterest.com/", "github": "https://github.com/", "whatsapp": "https://wa.me/",
}


def socials(text: str) -> dict:
    """Most frequent profile per platform (a site's own link usually appears in header and footer)."""
    found = {}
    for platform, rx in SOCIAL_RE.items():
        skip = SOCIAL[platform][1]
        hits = []
        for m in rx.finditer(text):
            if platform == "linkedin":
                kind, slug = m.group(1).lower(), m.group(2).rstrip(".").lower()
                if slug in BAD_SLUGS or "%" in slug:
                    continue
                hits.append(f"{kind}/{slug}")
            else:
                part = m.group(1).rstrip("./")
                if not part or part.split("/")[0].lower() in skip or part.lower() in BAD_SLUGS:
                    continue
                hits.append(part.lstrip("+") if platform == "whatsapp" else part)
        if hits:
            found[platform] = SOCIAL_BASE[platform] + Counter(hits).most_common(1)[0][0]
    return found


# ---------------------------------------------------------------- emails and phones

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24}")
MAILTO_RE = re.compile(r"mailto:([^\"'?>\s]+)", re.I)
CFEMAIL_RE = re.compile(r"""data-cfemail=["']([0-9a-fA-F]+)["']""")
TEL_RE = re.compile(r"""href\s*=\s*["']tel:([^"']+)["']""", re.I)
BAD_EMAIL_DOMAINS = ("example.com", "example.org", "domain.com", "email.com", "yourdomain.com", "yoursite.com",
                     "sentry.io", "wixpress.com", "sentry-next.wixpress.com", "godaddy.com", "company.com",
                     "test.com", "mysite.com", "website.com", "site.com", "address.com")
BAD_EMAIL_ENDINGS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".avif", ".ico")


def decode_cfemail(hexstr: str) -> str:
    """Cloudflare hides emails as XOR-encoded hex; the first byte is the key."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def clean_email(raw: str) -> str | None:
    e = unquote(raw).strip().strip(".").lower()
    if not EMAIL_RE.fullmatch(e):
        return None
    if e.endswith(BAD_EMAIL_ENDINGS) or e.split("@")[1] in BAD_EMAIL_DOMAINS:
        return None
    if re.search(r"@\d+x\.", e) or e.startswith(("noreply", "no-reply", "donotreply")):
        return None
    return e


def emails(html: str) -> list[str]:
    raw = MAILTO_RE.findall(html) + [decode_cfemail(h) for h in CFEMAIL_RE.findall(html)]
    raw += EMAIL_RE.findall(htmllib.unescape(html))
    seen = []
    for r in raw:
        e = clean_email(r)
        if e and e not in seen:
            seen.append(e)
    return seen


def clean_phone(raw: str) -> str | None:
    p = re.sub(r"[^\d+()\-. ]", "", unquote(raw)).strip()
    digits = re.sub(r"\D", "", p)
    return p if 7 <= len(digits) <= 15 else None


def phones(html: str) -> list[str]:
    seen, out = set(), []
    for raw in TEL_RE.findall(html):
        p = clean_phone(raw)
        if p and (d := re.sub(r"\D", "", p)) not in seen:
            seen.add(d)
            out.append(p)
    return out


# ---------------------------------------------------------------- page metadata

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
META_RE = re.compile(r"<meta\s[^>]*>", re.I)
ATTR_RE = re.compile(r"""([a-zA-Z:\-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
LANG_RE = re.compile(r"""<html[^>]*\blang\s*=\s*["']?([A-Za-z\-]{2,10})""", re.I)
JSONLD_RE = re.compile(r"""<script[^>]+application/ld\+json[^>]*>(.*?)</script>""", re.I | re.S)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)
CONTACT_RE = re.compile(r"contact|kontakt|contacto|contato|impressum|get-in-touch|reach-us", re.I)
ABOUT_RE = re.compile(r"about|uber-uns|ueber-uns|qui-sommes|chi-siamo|nosotros|sobre|company|team", re.I)

def metas(html: str) -> dict:
    out = {}
    for tag in META_RE.findall(html[:300_000]):
        attrs = {k.lower(): (v1 or v2) for k, v1, v2 in ATTR_RE.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in out:
            out[key] = htmllib.unescape(attrs["content"]).strip()
    return out


def _walk_jsonld(node, found):
    if isinstance(node, list):
        for n in node:
            _walk_jsonld(n, found)
    elif isinstance(node, dict):
        types = node.get("@type", [])
        types = [types] if isinstance(types, str) else types
        if any(t in ("Organization", "Corporation", "LocalBusiness", "Store", "Restaurant",
                     "ProfessionalService", "MedicalBusiness", "LegalService") or t.endswith("Business")
               for t in types if isinstance(t, str)):
            found.append(node)
        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk_jsonld(v, found)


def _jsonld_address(org: dict) -> str | None:
    addr = org.get("address")
    if not addr and isinstance(org.get("location"), dict):
        addr = org["location"].get("address")
    if isinstance(addr, list):
        addr = addr[0] if addr else None
    if isinstance(addr, dict):
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode")]
        addr = ", ".join(str(x).strip() for x in [*parts, country] if isinstance(x, (str, int)) and str(x).strip())
    return _clean_address(addr) if isinstance(addr, str) else None


def jsonld_org(html: str) -> dict:
    """schema.org Organization/LocalBusiness data that many sites embed for Google."""
    orgs = []
    for block in JSONLD_RE.findall(html):
        try:
            _walk_jsonld(json.loads(block.strip()), orgs)
        except (ValueError, RecursionError):
            continue
    if not orgs:
        return {}
    o = orgs[0]
    addr = next((a for org in orgs if (a := _jsonld_address(org))), None)
    same_as = o.get("sameAs") or []
    same_as = [same_as] if isinstance(same_as, str) else same_as
    employees = o.get("numberOfEmployees")
    if isinstance(employees, dict):
        employees = employees.get("value") or employees.get("minValue")
    return {k: v for k, v in {
        "org_name": o.get("name"), "legal_name": o.get("legalName"),
        "founding_date": o.get("foundingDate"), "org_address": addr if isinstance(addr, str) else None,
        "employees": employees, "jsonld_phone": o.get("telephone"), "jsonld_email": o.get("email"),
        "same_as": " ".join(s for s in same_as if isinstance(s, str)),
    }.items() if isinstance(v, (str, int)) and str(v).strip()}


# ---------------------------------------------------------------- postal address

MICRODATA_RE = re.compile(
    r"""itemprop\s*=\s*["'](streetAddress|addressLocality|addressRegion|postalCode|addressCountry)["']"""
    r"""(?:[^>]*\bcontent\s*=\s*["']([^"']*)["'])?[^>]*>([^<]{0,150})""", re.I)
ADDRESS_TAG_RE = re.compile(r"<address[^>]*>(.*?)</address>", re.I | re.S)
MAPS_RE = re.compile(
    r"""(?:google\.[a-z.]+/maps(?:/place/|/search/|/dir/[^/"']*/|/?\?(?:[^"'#]*&)?q=)|maps\.google\.[a-z.]+/"""
    r"""(?:maps)?\?(?:[^"'#]*&)?q=|maps\.apple\.com/\?(?:[^"'#]*&)?(?:address|q)=)([^"'&#<>/]{8,200})""", re.I)
TAGS_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.I | re.S)
STREET = (r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|"
          r"Parkway|Pkwy|Highway|Hwy|Circle|Cir|Terrace|Trail|Square|Sq|Plaza|Suite|Ste|Unit|Floor|Fl)")
TEXT_ADDRESS_RES = [
    # US: 123 Main St, Suite 4, Springfield, IL 62701
    re.compile(rf"\b\d{{1,6}}\s+(?:[A-Z0-9][\w'.\-]*\s+){{0,5}}{STREET}\b\.?[^\n|]{{0,80}}?,\s*"
               r"[A-Za-z .'\-]{2,40},?\s+[A-Z]{2}\s+\d{5}(?:-\d{4})?\b"),
    # Canada: ... Toronto, ON M5V 2T6
    re.compile(r"\b\d{1,6}\s+[^\n|]{3,80}?,\s*[A-Za-z .'\-]{2,40},?\s+"
               r"(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)\s+[A-Z]\d[A-Z]\s?\d[A-Z]\d\b"),
    # UK: 10 Downing Street, London SW1A 2AA
    re.compile(r"\b\d{1,4}[A-Za-z]?\s+[A-Z][\w'.\- ]{2,60},\s*(?:[A-Z][\w'.\- ]{2,40},?\s+){0,2}"
               r"[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"),
    # Germany/Austria/Switzerland: Hauptstraße 5, 10115 Berlin
    re.compile(r"\b[A-ZÄÖÜ][\wäöüß.\- ]{2,40}(?:straße|strasse|str\.|weg|platz|allee|gasse|ring|damm)"
               r"\s*\d{1,4}[a-z]?\s*,?\s+(?:D-|A-|CH-)?\d{4,5}\s+[A-ZÄÖÜ][\wäöüß.\- ]{2,40}", re.I),
]


TEXT_ADDRESS_ANCHORS = [  # one per pattern above, same order
    re.compile(r"\b[A-Z]{2}\s+\d{5}\b"),                                   # US state + ZIP
    re.compile(r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b"),                             # Canadian postcode
    re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b"),                     # UK postcode
    re.compile(r"(?:straße|strasse|str\.|weg|platz|allee|gasse|ring|damm)\s*\d", re.I),  # DE street
]


def _clean_address(text: str | None) -> str | None:
    if not text:
        return None
    a = re.sub(r"\s+", " ", htmllib.unescape(text)).strip(" ,;|-")
    a = re.sub(r"\s*,\s*", ", ", a)
    if not (8 <= len(a) <= 200) or not re.search(r"\d", a) or not re.search(r"[A-Za-zÀ-ÿ]{2}", a):
        return None
    if re.fullmatch(r"[-\d.,\s]+", a):  # bare coordinates
        return None
    return a


def visible_text(html: str) -> str:
    if len(html) > 600_000:  # keep the header and the footer, where addresses usually are
        html = html[:300_000] + "\n" + html[-300_000:]
    text = SCRIPT_STYLE_RE.sub(" ", html)
    text = re.sub(r"<br\s*/?>|</(?:p|div|li|td|tr|h\d)>", "\n", text, flags=re.I)
    return htmllib.unescape(TAGS_RE.sub(" ", text))


def postal_address(html: str) -> str | None:
    """Best-effort postal address from microdata, <address>, map links or the page text."""
    parts: dict = {}
    for key, content, inner in MICRODATA_RE.findall(html):
        val = (content or inner).strip()
        if val and key.lower() not in parts:
            parts[key.lower()] = val
    if parts.get("streetaddress") or parts.get("postalcode"):
        order = ("streetaddress", "addresslocality", "addressregion", "postalcode", "addresscountry")
        if a := _clean_address(", ".join(parts[k] for k in order if k in parts)):
            return a
    for block in ADDRESS_TAG_RE.findall(html):
        lines = [ln.strip() for ln in TAGS_RE.sub("\n", re.sub(r"<br\s*/?>", "\n", block, flags=re.I)).split("\n")]
        if a := _clean_address(", ".join(ln for ln in lines if ln and "@" not in ln and not ln.lower().startswith(("tel", "phone", "fax", "email")))):
            return a[:200]
    for m in MAPS_RE.findall(html):
        if a := _clean_address(unquote(m.replace("+", " "))):
            return a
    text = visible_text(html)
    # Find cheap anchors (postcodes, street words) first and run the full pattern only near them
    for anchor, rx in zip(TEXT_ADDRESS_ANCHORS, TEXT_ADDRESS_RES):
        for n, am in enumerate(anchor.finditer(text)):
            if n == 30:
                break
            window = text[max(0, am.start() - 220): am.end() + 80]
            if (m := rx.search(window)) and (a := _clean_address(m.group(0))):
                return a
    return None


def subpages(html: str, base_url: str) -> dict:
    """Best contact and about page URLs on the same site."""
    host = urlsplit(base_url).hostname
    out = {}
    for href in HREF_RE.findall(html):
        url = urljoin(base_url, htmllib.unescape(href.strip()))
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.hostname != host:
            continue
        if "contact" not in out and CONTACT_RE.search(parts.path):
            out["contact"] = url
        elif "about" not in out and ABOUT_RE.search(parts.path):
            out["about"] = url
        if len(out) == 2:
            break
    return out


def extract(html: str, url: str) -> dict:
    text = html.replace("\\/", "/")  # JSON-escaped links in scripts
    meta = metas(html)
    title = TITLE_RE.search(html)
    lang = LANG_RE.search(html[:5000])
    org = jsonld_org(html)
    page = {
        "title": re.sub(r"\s+", " ", htmllib.unescape(title.group(1))).strip()[:300] if title else None,
        "description": (meta.get("description") or meta.get("og:description") or "")[:500] or None,
        "site_name": meta.get("og:site_name") or meta.get("application-name"),
        "lang": lang.group(1).lower() if lang else None,
        "generator": meta.get("generator"),
        "emails": emails(html),
        "phones": phones(html),
        "links": subpages(html, url),
        **socials(text + " " + org.get("same_as", "")),
        **{k: v for k, v in org.items() if k != "same_as"},
    }
    if not page.get("org_address"):
        page["org_address"] = postal_address(html)
    if org.get("jsonld_email") and (e := clean_email(str(org["jsonld_email"]).replace("mailto:", ""))):
        page["emails"] = [e] + [x for x in page["emails"] if x != e]
    if org.get("jsonld_phone") and (p := clean_phone(str(org["jsonld_phone"]))):
        page["phones"] = [p] + [x for x in page["phones"] if re.sub(r"\D", "", x) != re.sub(r"\D", "", p)]
    return page


def merge(pages: list[dict], domain: str) -> dict:
    """Combine homepage + subpages: first value wins for single fields, lists are unioned.
    Emails on the company's own domain are listed first."""
    out: dict = {}
    for page in pages:
        for k, v in page.items():
            if k == "links":
                continue
            if isinstance(v, list):
                out.setdefault(k, [])
                out[k] += [x for x in v if x not in out[k]]
            elif v and not out.get(k):
                out[k] = v
    base = domain.split(":")[0]
    out["emails"] = sorted(out.get("emails", []), key=lambda e: not e.split("@")[1].endswith(base))[:10]
    out["phones"] = out.get("phones", [])[:5]
    return out
