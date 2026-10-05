"""Detect website technologies with the open-source Wappalyzer fingerprints.

Fingerprints come from https://github.com/enthec/webappanalyzer (the maintained
open-source fork of Wappalyzer's database, GPL-3.0). They are downloaded once into
data/webappanalyzer/ and matched against a page's HTML, script URLs, inline
scripts, meta tags, HTTP headers, cookies and URL.

Rules that need a real browser (`js` globals and `dom` selectors) are skipped, so
a few technologies that can only be seen that way won't be detected.
"""
import json
import re
import urllib.request
from pathlib import Path

SOURCE = "https://raw.githubusercontent.com/enthec/webappanalyzer/main/src"
FILES = [f"technologies/{c}.json" for c in "_abcdefghijklmnopqrstuvwxyz"] + ["categories.json"]
HTML_LIMIT = 300_000

SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.I | re.S)
META_TAG_RE = re.compile(r"<meta\s[^>]*>", re.I)
ATTR_RE = re.compile(r"""([a-zA-Z:\-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
METACHARS = set("[](){}*+?|^$.")


def download(dest: Path):
    dest.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        target = dest / Path(f).name
        if not target.exists():
            with urllib.request.urlopen(f"{SOURCE}/{f}", timeout=60) as r:
                target.write_bytes(r.read())


def _split_top(pattern: str, sep: str = "|") -> list[str]:
    """Split on `sep` outside groups and character classes."""
    parts, cur, depth, i, in_class = [], "", 0, 0, False
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            cur += pattern[i:i + 2]
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
            i += 1
            continue
        cur += ch
        i += 1
    parts.append(cur)
    return parts


def _best_run(alt: str) -> str:
    """Longest plain-text run at the top level of one alternative (groups and classes break runs)."""
    runs, cur, i, depth, in_class = [], "", 0, 0, False
    while i < len(alt):
        ch = alt[i]
        if ch == "\\" and i + 1 < len(alt):
            nxt = alt[i + 1]
            i += 2
            if depth or in_class or nxt.isalnum():  # \d \w \s \1 or inside a group
                runs.append(cur)
                cur = ""
                continue
            ch = nxt
        else:
            i += 1
            if in_class:
                in_class = ch != "]"
                continue
            if ch == "[":
                in_class = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                runs.append(cur)
                cur = ""
                continue
            if depth or ch in METACHARS:
                runs.append(cur)
                cur = ""
                continue
        if i < len(alt) and alt[i] in "?*{":  # the char just read is optional
            runs.append(cur)
            cur = ""
            continue
        cur += ch
    runs.append(cur)
    return max(runs, key=len)


def required_literals(pattern: str) -> list[str] | None:
    """Lowercased texts of which at least one must appear for the regex to match, used to skip
    regexes cheaply. None means no safe shortcut exists."""
    out = []
    for alt in _split_top(pattern):
        run = _best_run(alt)
        if len(run) >= 3:
            out.append(run.lower())
            continue
        inner = alt[3:-1] if alt.startswith("(?:") else alt[1:-1] if alt.startswith("(") else None
        if inner is None or not alt.endswith(")") or len(_split_top(alt)) != 1 \
                or _split_top(inner, ")")[0] != inner:
            return None
        sub = required_literals(inner)
        if sub is None:
            return None
        out += sub
    return out or None


class Pattern:
    __slots__ = ("rx", "literals", "version", "confidence")

    def __init__(self, raw: str):
        parts = raw.split("\\;")
        self.rx = re.compile(parts[0], re.I | re.M) if parts[0] else None
        self.literals = required_literals(parts[0]) if parts[0] else None
        self.version, self.confidence = None, 100
        for p in parts[1:]:
            if p.startswith("version:"):
                self.version = p[8:]
            elif p.startswith("confidence:"):
                self.confidence = int(p[11:] or 0)

    def match(self, text: str, lower: str | None = None):
        """Returns version string ('' if none) on match, None otherwise."""
        if self.rx is None:
            return ""  # key presence alone is enough
        if self.literals and lower is not None and not any(lit in lower for lit in self.literals):
            return None
        m = self.rx.search(text)
        if not m:
            return None
        if not self.version:
            return ""
        v = self.version
        for n, g in enumerate(m.groups(), 1):
            v = v.replace(f"\\{n}", g or "")
        if "?" in v and ":" in v:  # ternary \1?a:b
            cond, _, rest = v.partition("?")
            yes, _, no = rest.partition(":")
            v = yes if cond else no
        return v.strip() if re.fullmatch(r"[\w.\-]{1,30}", v.strip()) else ""


def _patterns(value) -> list[Pattern]:
    out = []
    for raw in value if isinstance(value, list) else [value]:
        if isinstance(raw, str):
            try:
                out.append(Pattern(raw))
            except (re.error, ValueError):
                continue
    return out


def _keyed(value) -> dict[str, list[Pattern]]:
    if not isinstance(value, dict):
        return {}
    return {k.lower(): _patterns(v) for k, v in value.items()}


class Detector:
    def __init__(self, data_dir: Path = Path("data/webappanalyzer")):
        download(data_dir)
        techs = {}
        for f in sorted(data_dir.glob("[a-z_].json")):
            techs.update(json.loads(f.read_text(encoding="utf-8")))
        cats = json.loads((data_dir / "categories.json").read_text(encoding="utf-8"))
        self.cat_names = {int(k): v["name"] for k, v in cats.items()}
        self.techs = {}
        for name, t in techs.items():
            self.techs[name] = {
                "cats": t.get("cats", []),
                "html": _patterns(t.get("html", [])),
                "scriptSrc": _patterns(t.get("scriptSrc", [])),
                "scripts": _patterns(t.get("scripts", [])),
                "url": _patterns(t.get("url", [])),
                "meta": _keyed(t.get("meta")),
                "headers": _keyed(t.get("headers")),
                "cookies": _keyed(t.get("cookies")),
                "implies": [i.split("\\;")[0] for i in _as_list(t.get("implies"))],
                "requires": [i.split("\\;")[0] for i in _as_list(t.get("requires"))],
                "requiresCategory": _as_list(t.get("requiresCategory")),
                "excludes": [i.split("\\;")[0] for i in _as_list(t.get("excludes"))],
            }

        # Only technologies that have rules for a source, so detect() skips the rest quickly
        self.by_source = {src: [(n, t[src]) for n, t in self.techs.items() if t[src]]
                          for src in ("scriptSrc", "html", "scripts", "url", "meta", "headers", "cookies")}

    def detect(self, html: str, url: str = "", headers: dict | None = None, cookies: dict | None = None) -> dict:
        """-> {technology name: version or ''}"""
        html = html[:HTML_LIMIT]
        html_l = html.lower()
        srcs = "\n".join(SCRIPT_SRC_RE.findall(html))
        srcs_l = srcs.lower()
        inline = "\n".join(INLINE_SCRIPT_RE.findall(html))[:HTML_LIMIT]
        inline_l = inline.lower()
        metas: dict[str, str] = {}
        for tag in META_TAG_RE.findall(html):
            a = {k.lower(): (v1 or v2) for k, v1, v2 in ATTR_RE.findall(tag)}
            key = (a.get("name") or a.get("property") or a.get("http-equiv") or "").lower()
            if key and "content" in a:
                metas.setdefault(key, a["content"])
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        cookies = {k.lower(): v for k, v in (cookies or {}).items()}

        found: dict[str, str] = {}

        def hit(name, version):
            if version or name not in found:
                found[name] = version or found.get(name, "")

        for src, text, lower in (("scriptSrc", srcs, srcs_l), ("html", html, html_l),
                                 ("scripts", inline, inline_l), ("url", url, None)):
            for name, pats in self.by_source[src]:
                for p in pats:
                    if (v := p.match(text, lower)) is not None:
                        hit(name, v)
                        break
        for src, values in (("meta", metas), ("headers", headers), ("cookies", cookies)):
            if not values:
                continue
            for name, keyed in self.by_source[src]:
                for key, pats in keyed.items():
                    if key in values:
                        for p in pats or [Pattern("")]:
                            if (v := p.match(values[key])) is not None:
                                hit(name, v)
                                break

        # implied technologies (WordPress -> PHP, MySQL)
        queue = list(found)
        while queue:
            for implied in self.techs.get(queue.pop(), {}).get("implies", []):
                if implied in self.techs and implied not in found:
                    found[implied] = ""
                    queue.append(implied)

        detected_cats = {c for n in found for c in self.techs[n]["cats"]}
        for name in list(found):
            t = self.techs[name]
            if t["requires"] and not any(r in found for r in t["requires"]):
                del found[name]
            elif t["requiresCategory"] and not any(c in detected_cats for c in t["requiresCategory"]):
                del found[name]
        for name in list(found):
            for ex in self.techs.get(name, {}).get("excludes", []):
                found.pop(ex, None)
        return found

    def categories(self, names) -> list[str]:
        return sorted({self.cat_names.get(c, str(c)) for n in names for c in self.techs.get(n, {}).get("cats", [])})


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]
