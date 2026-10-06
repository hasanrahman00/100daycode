"""Reduce a website host to the company's main domain.

    locations.pizzahut.com   -> pizzahut.com      (chain store locator -> the company)
    www.acme.co.uk           -> acme.co.uk
    joesplumbing.wixsite.com -> joesplumbing.wixsite.com   (website builder: the subdomain IS the business)
    dmv.ca.gov               -> dmv.ca.gov        (government: each agency is its own organisation)

Uses the Public Suffix List bundled with tldextract (no network needed), plus a list of website
builders and hosting platforms that the Public Suffix List doesn't cover.
"""
from functools import lru_cache

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)

# Platforms where every customer gets a subdomain; the subdomain is the business's own site.
KEEP_SUBDOMAIN_OF = {
    "business.site", "squarespace.com", "wordpress.com", "weebly.com", "weeblysite.com", "godaddysites.com",
    "jimdosite.com", "jimdofree.com", "jimdo.com", "ueniweb.com", "ueni.com", "mystrikingly.com",
    "strikingly.com", "wix.com", "wixsite.com", "wixstudio.io", "site123.me", "odoo.com", "zohosites.com",
    "zohosites.in", "hubspotpagebuilder.com", "hs-sites.com", "webnode.page", "webnode.com", "webs.com",
    "yolasite.com", "tilda.ws", "wixsite.com", "ecwid.com", "company.site", "square.site", "bigcartel.com",
    "storenvy.com", "tictail.com", "shopwebly.com", "simplesite.com", "one.com", "mozello.com",
    "webflow.io", "framer.website", "framer.ai", "carrd.co", "durable.co", "durablesites.com",
    "homestead.com", "web.com", "websitebuilder.com", "sitey.me", "ucraft.site", "ueniweb.co", "mobirisesite.com",
    "blogspot.com", "tumblr.com", "medium.com", "substack.com", "ghost.io", "linktr.ee", "beacons.ai",
    "myshopify.com", "wpengine.com", "wpenginepowered.com", "pantheonsite.io", "azurewebsites.net",
    "herokuapp.com", "netlify.app", "vercel.app", "github.io", "gitlab.io", "pages.dev", "web.app",
    "firebaseapp.com", "appspot.com", "cloudfront.net", "s3.amazonaws.com", "glitch.me", "repl.co",
    "editorx.io", "vev.site", "dudaone.com", "multiscreensite.com", "website.com", "yell.com", "yellsites.co.uk",
    "thryv.com", "thryvsites.com", "hibu.com", "webstarts.com", "sitebuilder.com", "jigsy.com",
}


def _is_government(suffix: str, registered: str) -> bool:
    parts = (suffix + "." + registered).split(".")
    return "gov" in parts or "mil" in parts or suffix.startswith(("gob.", "gouv.", "go.")) or suffix in ("gov", "mil")


@lru_cache(maxsize=2_000_000)
def root_domain(host: str | None) -> str | None:
    if not host:
        return host
    host = host.strip().lower().rstrip(".")
    if ":" in host or host.replace(".", "").isdigit():  # host:port or an IP address: leave as is
        return host
    e = _extract(host)
    registered = getattr(e, "top_domain_under_public_suffix", None) or e.registered_domain
    if not registered:
        return host
    if registered in KEEP_SUBDOMAIN_OF or _is_government(e.suffix, registered):
        sub = e.subdomain.split(".") if e.subdomain else []
        if sub and sub[0] == "www":
            sub = sub[1:]
        return ".".join(sub[-1:] + [registered]) if sub else registered
    return registered
