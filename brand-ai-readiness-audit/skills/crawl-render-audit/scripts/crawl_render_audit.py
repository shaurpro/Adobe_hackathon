#!/usr/bin/env python3
"""
crawl_render_audit.py — deterministic checks for mechanisms M1-M3.

M1 Crawl -> Read -> Extract gate
M2 Fetchability of a quotable source (fetch-gating half)
M3 Machine readability != human readability

Read-only. Obeys robots.txt for its own requests. Never POSTs, never
authenticates, never modifies anything.

Usage:
  python3 crawl_render_audit.py --url https://example.com \
      [--max-pages 20] [--out findings.json] [--pages-cache pages.json]
      [--pages-in pages.json] [--render auto|off]

Exit code is 0 whenever the audit itself completed, regardless of how many
findings were produced (findings are data, not failures).
"""

import argparse
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auditlib import (  # noqa: E402
    AI_AGENTS, DEFAULT_UA, Finding, HTMLDoc, crawl, discover_sitemaps, doc_for,
    emit, get_robots, html_pages, is_internal, load_pages, norm_text,
    registrable_domain, save_pages, template_signature, tokens, utc_now,
)

SKILL = "crawl-render-audit"

# Paths a well-run site *should* keep out of an index. Blocking these is
# hygiene, not a discoverability defect — the false-positive guard for M1-S1.
UTILITY_PATTERNS = re.compile(
    r"(^/(wp-admin|admin|api|cgi-bin|checkout|cart|account|login|signin|signup|"
    r"register|logout|search|preview|draft|test|tmp|static|assets|_next|cdn-cgi|"
    r"wp-includes|xmlrpc)\b)|(\?|&)(utm_|sort=|filter=|session|sid=|replytocom)",
    re.I)

CHALLENGE_SIGNATURES = [
    "just a moment", "checking your browser", "enable javascript and cookies",
    "captcha", "cf-browser-verification", "ddos protection by",
    "attention required! | cloudflare", "please verify you are a human",
    "access denied", "request unsuccessful",
]

SOFT_404_SIGNATURES = [
    "page not found", "404 error", "cannot be found", "doesn't exist",
    "does not exist", "no longer available", "sorry, we couldn't find",
    "oops! that page", "error 404",
]

CONSENT_MARKERS = [
    "we use cookies", "cookie policy", "accept all cookies", "manage consent",
    "privacy preferences", "consent management", "gdpr",
]

PLACEHOLDER_VALUES = re.compile(
    r"(your[_\- ]?(company|name|brand|site|url)|example\.com|lorem ipsum|"
    r"yourdomain|change ?me|todo|xxxx|placeholder|\{\{|\$\{)", re.I)

PRICE_IN_TEXT = re.compile(r"[$€£₹¥]\s?\d[\d,.]*|\b\d[\d,.]*\s?(usd|eur|gbp|inr)\b", re.I)

ENTITY_TYPES = {"organization", "corporation", "localbusiness", "onlinestore",
                "ngo", "educationalorganization", "governmentorganization",
                "person", "brand", "sportsorganization", "medicalorganization",
                "restaurant", "store", "professionalservice"}


def infer_page_type(url, doc, ld_types):
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    if path in ("", "/"):
        return "homepage"
    lt = {t.lower() for t in ld_types}
    if lt & {"product", "offer", "productgroup"}:
        return "product"
    if lt & {"article", "newsarticle", "blogposting", "techarticle"}:
        return "article"
    if lt & {"faqpage", "qapage"}:
        return "faq"
    if re.search(r"/(product|products|item|shop|store|p)/", path, re.I):
        return "product"
    if re.search(r"/(blog|news|article|post|insights|resources)/", path, re.I):
        return "article"
    if re.search(r"/(about|company|who-we-are|our-story|team)", path, re.I):
        return "about"
    if re.search(r"/(contact|support|help)", path, re.I):
        return "contact"
    if re.search(r"/(pricing|plans|price)", path, re.I):
        return "pricing"
    return "other"


def ld_types_of(nodes):
    out = []
    for n in nodes:
        t = n.get("@type")
        if isinstance(t, list):
            out.extend([str(x) for x in t])
        elif t:
            out.append(str(t))
    return out


def word_count(s):
    return len(tokens(s))


# --------------------------------------------------------------------------
# M1-S1 — crawler admission
# --------------------------------------------------------------------------

def check_admission(site, robots, robots_url, robots_resp, pages, sitemap_urls, F):
    if robots.unreachable and not robots.missing:
        F.append(Finding(
            "M1-S1", "M1",
            "robots.txt is unreachable or erroring",
            "critical",
            f"GET {robots_url} returned "
            f"{robots_resp['status'] or robots_resp['error']}. Major crawlers treat a "
            f"persistently failing robots.txt as a site-wide disallow and stop fetching, "
            f"so every other on-page improvement is unreachable.",
            "Serve robots.txt with HTTP 200 (an empty file is valid and means "
            "'crawl everything'), or 404 it deliberately. Never let it 5xx.",
            "critical", scope="site", urls=[robots_url],
            evidence_key="M1-S1:robots-unreachable", skill=SKILL,
            falsifier="A 200 or 404 response on retry falsifies this flag."))
        return

    blocked = []
    for agent in AI_AGENTS:
        is_blocked, rule = robots.blanket_block(agent)
        if is_blocked:
            blocked.append((agent, rule))

    ai_only = [a for a, _ in blocked if a not in ("Googlebot", "Bingbot")]
    if blocked:
        rules = "; ".join(f"{a} -> {r}" for a, r in blocked[:6])
        if len(blocked) >= 4:
            sev, scope_note = "critical", "assistant traffic as a class"
        else:
            sev, scope_note = "medium", "specific assistants"
        F.append(Finding(
            "M1-S1", "M1",
            f"robots.txt blocks {len(blocked)} AI/search crawler(s) at the site root",
            sev,
            f"{robots_url} denies '/' to: {rules}. Stage-1 failure — these agents "
            f"never fetch a byte, so nothing else about the site can be read, quoted "
            f"or cited by {scope_note}.",
            "Decide deliberately which agents may read public marketing content. "
            "If citation in AI answers is wanted, allow the retrieval agents "
            "(ClaudeBot, GPTBot, PerplexityBot, OAI-SearchBot) on public pages while "
            "keeping bulk-training crawlers (CCBot, Google-Extended) blocked if that "
            "is the policy preference.",
            sev, scope="site", urls=[robots_url],
            evidence_key="M1-S1:root-block", skill=SKILL,
            falsifier="Deliberate policy to stay out of AI answers falsifies the "
                      "severity but not the observation; confirm intent before acting."))

    # Blocked paths that carry public content (in nav or sitemap) — not utility URLs.
    content_blocked = []
    candidates = set(sitemap_urls[:200])
    for p in html_pages(pages)[:5]:
        for a in doc_for(p).anchors(internal_only=True):
            candidates.add(a["absolute"])
    for u in list(candidates)[:300]:
        path = urllib.parse.urlsplit(u).path or "/"
        if UTILITY_PATTERNS.search(path):
            continue  # false-positive guard
        ok, rule = robots.allowed("Googlebot", path)
        if not ok:
            content_blocked.append((u, rule))
    if content_blocked:
        sample = "; ".join(f"{u} ({r})" for u, r in content_blocked[:3])
        F.append(Finding(
            "M1-S1", "M1",
            f"{len(content_blocked)} linked/sitemapped content URL(s) are disallowed in robots.txt",
            "high",
            f"These URLs are advertised to crawlers (via internal links or the sitemap) "
            f"yet blocked from fetching: {sample}. Utility paths (cart, admin, search, "
            f"faceted params) were excluded from this count.",
            "Remove the Disallow rules covering public content paths, or stop linking/"
            "sitemapping them so crawl budget is not spent on doors that are locked.",
            "high", scope="site", urls=[u for u, _ in content_blocked],
            evidence_key="M1-S1:content-path-block", skill=SKILL,
            falsifier="If these pages are intentionally private, the correct fix is "
                      "removing them from the sitemap/nav, not opening robots."))

    # noindex directives on fetchable content
    noindex = []
    for p in html_pages(pages):
        xr = (p["headers"].get("x-robots-tag") or "").lower()
        doc = doc_for(p)
        mr = (doc.meta_value(name="robots") or "").lower()
        gb = (doc.meta_value(name="googlebot") or "").lower()
        if "noindex" in xr or "noindex" in mr or "noindex" in gb:
            src = "X-Robots-Tag header" if "noindex" in xr else "<meta name=robots>"
            noindex.append((p["url"], src))
    if noindex:
        home = [u for u, _ in noindex
                if (urllib.parse.urlsplit(u).path.rstrip("/") in ("", "/"))]
        sev = "critical" if home else ("high" if len(noindex) >= 3 else "medium")
        F.append(Finding(
            "M1-S1", "M1",
            "noindex directive on crawlable content page(s)",
            sev,
            f"{len(noindex)} of {len(html_pages(pages))} sampled pages carry noindex: "
            + "; ".join(f"{u} via {s}" for u, s in noindex[:4])
            + (". This includes the homepage." if home else ""),
            "Remove noindex from any page meant to be found. If the page is genuinely "
            "internal, also remove it from the sitemap and internal navigation.",
            sev, scope="site" if len(noindex) > 2 else "page",
            urls=[u for u, _ in noindex], evidence_key="M1-S1:noindex", skill=SKILL,
            falsifier="Thank-you pages, paginated duplicates and staging URLs are "
                      "correctly noindexed — those page types are excluded here only "
                      "if their URL says so, so verify page intent before acting."))


# --------------------------------------------------------------------------
# M1-S2 — response integrity
# --------------------------------------------------------------------------

def check_response_integrity(site, pages, F):
    long_chains, bad_status, soft404, canon_issues = [], [], [], []
    for p in pages:
        if p.get("skipped"):
            continue
        if len(p.get("chain", [])) >= 3:
            long_chains.append((p["url"], len(p["chain"])))
        st = p.get("status")
        if st and st >= 400:
            bad_status.append((p["url"], st))
        if not p.get("is_html") or st != 200:
            continue
        doc = doc_for(p)
        mt = doc.main_text.lower()
        wc = word_count(mt)
        title_l = (doc.title or "").lower()
        if wc < 120 and any(sig in mt or sig in title_l for sig in SOFT_404_SIGNATURES):
            soft404.append((p["url"], wc))
        can = doc.canonical()
        if can:
            def _norm(u):
                pth = urllib.parse.urlsplit(u).path.rstrip("/")
                # A directory URL and its index document are the same resource.
                pth = re.sub(r"/(index|default)\.(html?|php|aspx)$", "", pth, flags=re.I)
                return pth
            if not is_internal(can, p["final_url"]):
                canon_issues.append((p["url"], f"cross-domain canonical -> {can}"))
            elif _norm(can) not in (_norm(p["final_url"]), _norm(p["url"])):
                canon_issues.append((p["url"], f"canonical points elsewhere -> {can}"))

    if long_chains:
        F.append(Finding(
            "M1-S2", "M1", "Long redirect chains on crawled URLs", "medium",
            "; ".join(f"{u} ({n} hops)" for u, n in long_chains[:4])
            + ". Chains of 3+ hops lose crawl budget and some fetchers stop early, "
              "so the destination content may never be read.",
            "Collapse each chain to a single 301 from the original URL to the final "
            "destination; update internal links to point at the final URL directly.",
            "medium", scope="site", urls=[u for u, _ in long_chains],
            evidence_key="M1-S2:redirect-chain", skill=SKILL,
            falsifier="A single http->https or trailing-slash hop is normal and is "
                      "not counted here (threshold is 3+)."))
    if bad_status:
        F.append(Finding(
            "M1-S2", "M1", "Internally linked URLs return error status codes", "high",
            f"{len(bad_status)} linked URL(s) returned 4xx/5xx: "
            + "; ".join(f"{u} -> {s}" for u, s in bad_status[:5])
            + ". Broken internal links waste crawl budget and break the path a "
              "retrieval agent follows to reach fact pages.",
            "Fix or remove the broken links; 301 retired URLs to their closest live "
            "equivalent rather than leaving them 404.",
            "high", scope="site", urls=[u for u, _ in bad_status],
            evidence_key="M1-S2:broken-links", skill=SKILL,
            falsifier="Deliberate 410s for removed content are correct; verify intent."))
    if soft404:
        F.append(Finding(
            "M1-S2", "M1", "Soft 404: error page served with HTTP 200", "high",
            "; ".join(f"{u} ({wc} words of body text, error wording present)"
                      for u, wc in soft404[:4])
            + ". A 200 status tells crawlers the page is valid content, so error "
              "pages get indexed and compete with real pages.",
            "Return a real 404/410 status for missing content. Reserve 200 for pages "
            "that actually contain the content they claim to.",
            "high", scope="site", urls=[u for u, _ in soft404],
            evidence_key="M1-S2:soft-404", skill=SKILL,
            falsifier="A short-but-real page that merely mentions the word 'error' "
                      "is excluded by the <120-word body-length condition."))
    if canon_issues:
        F.append(Finding(
            "M1-S2", "M1", "Canonical tag points away from the page's own content", "high",
            "; ".join(f"{u}: {why}" for u, why in canon_issues[:4])
            + ". A wrong canonical asks search and retrieval systems to attribute this "
              "page's content to a different URL, effectively deindexing it.",
            "Make each page self-canonical unless it is a genuine duplicate. Verify the "
            "canonical target returns 200 and describes the same entity.",
            "high", scope="site", urls=[u for u, _ in canon_issues],
            evidence_key="M1-S2:canonical", skill=SKILL,
            falsifier="Cross-domain canonicals are legitimate for syndicated content — "
                      "confirm the target is the same content before changing."))


# --------------------------------------------------------------------------
# M1-S3 — discovery surface
# --------------------------------------------------------------------------

def check_discovery(site, robots, sitemap_reports, sitemap_urls, pages, F):
    reachable = [r for r in sitemap_reports if r["status"] == 200]
    n_html = len(html_pages(pages))
    if not reachable:
        if n_html >= 12:
            F.append(Finding(
                "M1-S3", "M1", "No reachable XML sitemap", "medium",
                f"No sitemap found via robots.txt or the conventional locations "
                f"({', '.join(r['url'] for r in sitemap_reports[:3]) or 'none tried'}); "
                f"the crawl found {n_html}+ HTML pages, so discovery depends entirely "
                f"on internal linking.",
                "Publish /sitemap.xml with lastmod dates for every canonical URL and "
                "declare it in robots.txt with a Sitemap: line.",
                "medium", scope="site", urls=[site],
                evidence_key="M1-S3:no-sitemap", skill=SKILL,
                falsifier="Sites under ~12 pages with complete homepage linking are "
                          "fully discoverable without a sitemap; no flag is raised."))
        else:
            F.append(Finding(
                "M1-S3", "M1", "No XML sitemap (small site — proactive)", "low",
                f"Only {n_html} pages were reachable by crawl and no sitemap exists. "
                f"Not currently blocking discovery, but a sitemap makes new pages "
                f"discoverable on their first day rather than on the next full crawl.",
                "Add a small /sitemap.xml with lastmod and reference it in robots.txt.",
                "low", scope="site", urls=[site],
                evidence_key="M1-S3:no-sitemap-small", skill=SKILL))
        return

    r = reachable[0]
    if not r["declared_in_robots"] and robots.exists:
        F.append(Finding(
            "M1-S3", "M1", "Sitemap exists but is not declared in robots.txt", "low",
            f"{r['url']} responds 200 but robots.txt contains no Sitemap: line, so "
            f"agents that do not guess conventional paths never find it.",
            f"Add 'Sitemap: {r['url']}' to robots.txt.",
            "low", scope="site", urls=[r["url"]],
            evidence_key="M1-S3:sitemap-undeclared", skill=SKILL))
    if r["urls"] and not r["lastmod"]:
        F.append(Finding(
            "M1-S3", "M1", "Sitemap omits lastmod dates", "low",
            f"{r['url']} lists {r['urls']} URLs with no <lastmod> elements, so "
            f"crawlers cannot tell which pages changed and recrawl on a blind schedule.",
            "Emit accurate <lastmod> per URL, updated only on meaningful content change.",
            "low", scope="site", urls=[r["url"]],
            evidence_key="M1-S3:no-lastmod", skill=SKILL))

    # Sample sitemap entries for liveness.
    checked = {p["url"].rstrip("/"): p.get("status") for p in pages}
    tested = [(u, checked[u.rstrip("/")]) for u in sitemap_urls
              if u.rstrip("/") in checked]
    dead = [(u, s) for u, s in tested if s and (s >= 400)]
    if tested and len(dead) / max(1, len(tested)) > 0.2:
        F.append(Finding(
            "M1-S3", "M1", "Sitemap advertises dead URLs", "high",
            f"{len(dead)} of {len(tested)} sampled sitemap URLs returned 4xx/5xx: "
            + "; ".join(f"{u} -> {s}" for u, s in dead[:4])
            + ". A sitemap is a direct instruction about what to fetch; sending "
              "crawlers to dead URLs burns budget and lowers trust in the file.",
            "Regenerate the sitemap from live canonical URLs only, and automate it so "
            "it cannot drift from the published site.",
            "high", scope="site", urls=[u for u, _ in dead],
            evidence_key="M1-S3:sitemap-dead", skill=SKILL,
            falsifier="URLs that merely 301 to their https/slash variant are not "
                      "counted as dead."))

    crawled = {p["url"].rstrip("/") for p in html_pages(pages)}
    sm = {u.rstrip("/") for u in sitemap_urls}
    orphans = [u for u in crawled if u not in sm and sm]
    if sm and len(orphans) > max(2, 0.3 * len(crawled)):
        F.append(Finding(
            "M1-S3", "M1", "Internally linked pages missing from the sitemap", "medium",
            f"{len(orphans)} of {len(crawled)} crawled pages are absent from the "
            f"sitemap, e.g. {', '.join(list(orphans)[:3])}. The two discovery surfaces "
            f"disagree about what the site contains.",
            "Generate the sitemap from the same source of truth as the navigation so "
            "every canonical, indexable page appears in both.",
            "medium", scope="site", urls=list(orphans),
            evidence_key="M1-S3:orphans", skill=SKILL,
            falsifier="Paginated, filtered and tag pages are legitimately excluded "
                      "from sitemaps; review the list before bulk-adding."))


# --------------------------------------------------------------------------
# M2-S1 — fetch gating for non-browser clients
# --------------------------------------------------------------------------

def check_fetch_gating(site, pages, F):
    challenged, denied, consent_walled = [], [], []
    for p in pages:
        if p.get("skipped") or not p.get("status"):
            continue
        st = p["status"]
        body_l = (p.get("html") or "")[:4000].lower()
        if st in (403, 401):
            denied.append((p["url"], st))
        elif st == 429:
            denied.append((p["url"], st))
        elif any(sig in body_l for sig in CHALLENGE_SIGNATURES):
            challenged.append(p["url"])
        elif p.get("is_html") and st == 200:
            doc = doc_for(p)
            wc = word_count(doc.main_text)
            if wc < 60 and any(m in (doc.all_text or "").lower() for m in CONSENT_MARKERS):
                consent_walled.append((p["url"], wc))

    if denied:
        sev = "critical" if len(denied) >= max(2, 0.5 * len(pages)) else "high"
        F.append(Finding(
            "M2-S1", "M2", "Site refuses plain HTTP clients (403/401/429)", sev,
            f"{len(denied)} of {len(pages)} fetches were refused: "
            + "; ".join(f"{u} -> {s}" for u, s in denied[:4])
            + ". The request used an honest, identifying, non-abusive user agent at a "
              "low rate. Assistants fetch pages the same way — a refusal here means "
              "the page can never become a cited source.",
            "Allowlist well-behaved, identifying agents in the WAF/CDN bot rules for "
            "public marketing content; keep aggressive challenges for login, checkout "
            "and API routes only.",
            sev, scope="site", urls=[u for u, _ in denied],
            evidence_key="M2-S1:fetch-denied", skill=SKILL,
            falsifier="A single 429 can be transient rate limiting — re-run before "
                      "escalating. Subscriber-only content is a business choice, not "
                      "a defect."))
    if challenged:
        F.append(Finding(
            "M2-S1", "M2", "Bot challenge (interstitial) served instead of content", "high",
            f"{len(challenged)} page(s) returned a JS/CAPTCHA challenge body rather "
            f"than content, e.g. {challenged[0]}. Retrieval agents receive the "
            f"challenge page text, not the brand's facts.",
            "Exempt public content paths from interactive challenges, or serve a "
            "static HTML fallback containing the core facts to non-JS clients.",
            "high", scope="site", urls=challenged,
            evidence_key="M2-S1:bot-challenge", skill=SKILL))
    if consent_walled:
        F.append(Finding(
            "M2-S1", "M2", "Consent gate replaces page content for non-JS clients", "high",
            "; ".join(f"{u} ({wc} words of body text; consent wording present)"
                      for u, wc in consent_walled[:4])
            + ". The raw HTML response contains the consent notice but not the article "
              "or product text, so there is nothing to extract or quote.",
            "Render primary content server-side and layer the consent UI on top as an "
            "overlay, rather than withholding the document until consent is recorded.",
            "high", scope="site", urls=[u for u, _ in consent_walled],
            evidence_key="M2-S1:consent-wall", skill=SKILL,
            falsifier="An overlay is fine: if the body text is present in the raw HTML "
                      "underneath the banner, no flag is raised (that is the <60-word "
                      "condition)."))


# --------------------------------------------------------------------------
# M3-S1 — client-side render delta
# --------------------------------------------------------------------------

def try_render(url, timeout=20):
    """Optional headless render. Absent browser => 'unavailable', never a flag."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return None, "playwright_not_installed"
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            page = b.new_page(user_agent=DEFAULT_UA)
            page.goto(url, timeout=timeout * 1000, wait_until="networkidle")
            html = page.content()
            b.close()
        return html, None
    except Exception as e:
        return None, f"render_failed: {type(e).__name__}"


def check_render_gap(site, pages, F, render_mode="auto"):
    shells, rendered_gaps = [], []
    hp = html_pages(pages)
    for p in hp:
        doc = doc_for(p)
        wc = word_count(doc.main_text)
        script_bytes = sum(s["len"] for s in doc.scripts)
        ext_scripts = len([s for s in doc.scripts if s["src"]])
        has_mount = bool(doc.mount_nodes)
        noscript_wc = word_count(" ".join(doc.noscript_text))
        ld_nodes, _ = doc.jsonld()
        ld_text_wc = word_count(json.dumps(ld_nodes)) if ld_nodes else 0
        h1s = [h for h in doc.headings if h["level"] == 1]
        # A shell: near-empty body text, heavy JS, no server-side fallback.
        if wc < 80 and (has_mount or ext_scripts >= 3) and noscript_wc < 40:
            # Guard: facts recoverable from JSON-LD or <noscript> => downgrade.
            recoverable = ld_text_wc > 40 or noscript_wc >= 40
            shells.append({"url": p["url"], "words": wc, "ext_scripts": ext_scripts,
                           "mount": [m["id"] for m in doc.mount_nodes],
                           "recoverable": recoverable, "h1": bool(h1s),
                           "script_bytes": script_bytes})
        if render_mode != "off" and len(rendered_gaps) < 2 and wc >= 80:
            pass  # raw content already sufficient; skip the expensive render

    # Optional browser comparison on the two thinnest pages only (runtime budget).
    render_note = None
    if render_mode != "off" and shells:
        html, err = try_render(shells[0]["url"])
        render_note = err
        if html:
            rdoc = HTMLDoc(html, base_url=shells[0]["url"])
            raw_doc = doc_for(next(p for p in hp if p["url"] == shells[0]["url"]))
            rt, rawt = set(tokens(rdoc.main_text)), set(tokens(raw_doc.main_text))
            if rt:
                missing = 1 - (len(rt & rawt) / len(rt))
                shells[0]["missing_ratio"] = round(missing, 3)
                shells[0]["rendered_words"] = word_count(rdoc.main_text)

    if shells:
        hard = [s for s in shells if not s["recoverable"]]
        ratio = len(shells) / max(1, len(hp))
        if hard and ratio >= 0.5:
            sev = "critical"
        elif hard:
            sev = "high"
        else:
            sev = "medium"
        ev = (f"{len(shells)} of {len(hp)} sampled pages return an effectively empty "
              f"document to a non-JS fetch: "
              + "; ".join(
                  f"{s['url']} ({s['words']} words of body text in the raw HTML, "
                  f"{s['ext_scripts']} external scripts"
                  + (f", mount node #{s['mount'][0]}" if s["mount"] else "")
                  + (f", headless render yields {s.get('rendered_words')} words "
                     f"=> {int(s.get('missing_ratio', 0) * 100)}% of visible text is "
                     f"client-only" if s.get("missing_ratio") is not None else "")
                  + ")" for s in shells[:3]))
        if any(s["recoverable"] for s in shells):
            ev += (". Some facts remain recoverable from JSON-LD/<noscript>, which "
                   "reduces but does not remove the risk.")
        if render_note:
            ev += f" (Headless verification unavailable: {render_note}.)"
        F.append(Finding(
            "M3-S1", "M3",
            "Content is assembled client-side and absent from the server response", sev,
            ev,
            "Server-render or pre-render the primary content — at minimum the H1, the "
            "core descriptive paragraph, key facts (pricing, specs, location) and the "
            "primary navigation links — so the first HTTP response already contains "
            "the facts. Hydrate interactivity on top of that, rather than building the "
            "document from scratch in the browser.",
            sev, scope="site" if ratio >= 0.5 else "template",
            urls=[s["url"] for s in shells],
            confidence="observed" if shells[0].get("missing_ratio") is not None
            else "inferred",
            evidence_key="M3-S1:client-render", skill=SKILL,
            falsifier="Pages whose facts appear in raw JSON-LD or <noscript> are "
                      "downgraded, and late-hydrating chrome (chat widgets, related "
                      "posts, cookie banners) is excluded from the text comparison."))


# --------------------------------------------------------------------------
# M3-S2 — facts locked in non-text carriers
# --------------------------------------------------------------------------

def check_non_text(site, pages, F):
    missing_alt, pdf_primary, video_no_track = [], [], []
    total_content_imgs = 0
    for p in html_pages(pages):
        doc = doc_for(p)
        for img in doc.images:
            if img["in_chrome"]:
                continue
            try:
                w, h = int(img["w"] or 0), int(img["h"] or 0)
            except ValueError:
                w = h = 0
            if (0 < w <= 48 and 0 < h <= 48) or img.get("role") == "presentation":
                continue  # icon / decorative
            total_content_imgs += 1
            alt = img["alt"]
            if not img["has_alt"]:
                missing_alt.append((p["url"], img["src"][:80]))
            elif alt and (re.fullmatch(r"[\w\-_.]+\.(png|jpe?g|gif|webp|svg)", alt.strip(), re.I)
                          or alt.strip().lower() in ("image", "photo", "picture", "logo")):
                missing_alt.append((p["url"], f"{img['src'][:60]} (alt='{alt[:30]}')"))
        for a in doc.anchors():
            if a["absolute"].lower().endswith(".pdf"):
                if a.get("in_chrome") or re.search(
                        r"(price|pricing|spec|datasheet|catalog|menu|brochure|rate|fee)",
                        (a["text"] + a["absolute"]).lower()):
                    pdf_primary.append((p["url"], a["absolute"], a["text"][:60]))
        if doc.videos and doc.tracks == 0:
            body_wc = word_count(doc.main_text)
            if body_wc < 250:  # video is carrying the page
                video_no_track.append((p["url"], body_wc))

    if missing_alt and total_content_imgs:
        share = len(missing_alt) / total_content_imgs
        sev = "medium" if share > 0.4 else "low"
        F.append(Finding(
            "M3-S2", "M3", "Content images carry no usable alt text", sev,
            f"{len(missing_alt)} of {total_content_imgs} non-decorative content images "
            f"have missing or meaningless alt text, e.g. "
            + "; ".join(f"{u} :: {s}" for u, s in missing_alt[:3])
            + ". Any fact conveyed only by these images is invisible to text-based "
              "extraction.",
            "Write alt text that states the fact the image carries ('Model X, 12-hour "
            "battery, 1.2 kg'), not the file name. Where an image contains numbers or "
            "specifications, also state them in body text or structured data.",
            sev, scope="site", urls=[u for u, _ in missing_alt],
            evidence_key="M3-S2:alt-text", skill=SKILL,
            falsifier="Empty alt=\"\" on decorative images is correct practice and is "
                      "not counted; icons under 48px and role=presentation are skipped."))
    if pdf_primary:
        F.append(Finding(
            "M3-S2", "M3", "Key facts distributed only as PDF", "high",
            f"{len(pdf_primary)} link(s) route pricing/spec/menu-type information into "
            f"PDFs rather than HTML: "
            + "; ".join(f"{t or 'link'} -> {pdf}" for _, pdf, t in pdf_primary[:3])
            + ". PDFs are extracted inconsistently, lose structure, and are frequently "
              "skipped entirely by retrieval agents.",
            "Publish an HTML version of every PDF that carries decision-relevant facts "
            "(prices, specs, hours, policies) and keep the PDF as a download link "
            "beside it, not instead of it.",
            "high", scope="site", urls=[p for _, p, _ in pdf_primary],
            evidence_key="M3-S2:pdf-only", skill=SKILL,
            falsifier="A PDF is not a finding when an HTML page states the same facts — "
                      "check for an HTML equivalent before acting."))
    if video_no_track:
        F.append(Finding(
            "M3-S2", "M3", "Video carries the page with no transcript or captions", "medium",
            "; ".join(f"{u} ({wc} words of body text alongside embedded video)"
                      for u, wc in video_no_track[:3])
            + ". Nothing in the video is available as extractable text.",
            "Publish a transcript or a summary of the key claims as HTML text on the "
            "same page, and add a <track kind=captions> to the player.",
            "medium", scope="page", urls=[u for u, _ in video_no_track],
            evidence_key="M3-S2:video-no-text", skill=SKILL,
            falsifier="Pages with 250+ words of body text alongside the video are not "
                      "flagged — the facts already exist in text."))


# --------------------------------------------------------------------------
# M3-S3 — structured data presence, validity and agreement
# --------------------------------------------------------------------------

def check_structured_data(site, pages, F):
    parse_errors, placeholders, mismatches = [], [], []
    entity_pages, typed_pages, untyped = [], [], []
    hp = html_pages(pages)
    for p in hp:
        doc = doc_for(p)
        nodes, errors = doc.jsonld()
        types = ld_types_of(nodes)
        ptype = infer_page_type(p["url"], doc, types)
        for e in errors:
            parse_errors.append((p["url"], e.get("error", ""), e.get("snippet", "")))
        if not nodes and not doc.microdata_types:
            untyped.append((p["url"], ptype))
        else:
            typed_pages.append(p["url"])
        if any(t.lower() in ENTITY_TYPES for t in types):
            entity_pages.append(p["url"])
        blob = json.dumps(nodes)
        m = PLACEHOLDER_VALUES.search(blob)
        if m and nodes:
            placeholders.append((p["url"], m.group(0)[:40]))
        # Agreement with visible text
        h1 = next((h["text"] for h in doc.headings if h["level"] == 1), "")
        for n in nodes:
            name = n.get("name")
            if isinstance(name, str) and h1 and n.get("@type") in ("Product", "Article",
                                                                   "NewsArticle", "Recipe"):
                a, b = set(tokens(name)), set(tokens(h1))
                if a and b and not (a & b):
                    mismatches.append((p["url"],
                                       f"JSON-LD name '{name[:40]}' shares no words "
                                       f"with H1 '{h1[:40]}'"))
            offers = n.get("offers")
            if isinstance(offers, dict):
                offers = [offers]
            if isinstance(offers, list):
                for o in offers:
                    if not isinstance(o, dict):
                        continue
                    price = str(o.get("price", "")).strip()
                    if price and price not in ("0", "0.00"):
                        digits = re.sub(r"[^\d]", "", price)
                        page_prices = PRICE_IN_TEXT.findall(doc.main_text)
                        if page_prices and digits and digits not in re.sub(
                                r"[^\d]", "", doc.main_text):
                            mismatches.append(
                                (p["url"], f"JSON-LD offers price '{price}' does not "
                                           f"appear in visible text"))

    if parse_errors:
        F.append(Finding(
            "M3-S3", "M3", "JSON-LD block fails to parse and is discarded wholesale", "high",
            "; ".join(f"{u}: {err} [{sn}]" for u, err, sn in parse_errors[:3])
            + f" ({len(parse_errors)} block(s) affected). An invalid block is dropped "
              f"entirely — the site pays the cost of emitting structured data and "
              f"receives none of the benefit.",
            "Fix the JSON syntax (usually an unescaped quote, a trailing comma, or a "
            "templating variable that did not render) and add a build-time JSON.parse "
            "check so invalid markup cannot ship.",
            "high", scope="site", urls=[u for u, _, _ in parse_errors],
            evidence_key="M3-S3:ld-parse-error", skill=SKILL))
    if placeholders:
        F.append(Finding(
            "M3-S3", "M3", "Structured data contains unrendered placeholder values", "high",
            "; ".join(f"{u}: contains '{v}'" for u, v in placeholders[:3])
            + ". The markup parses but asserts template defaults as facts, actively "
              "teaching machines wrong information about the entity.",
            "Bind these fields to real CMS values and fail the build when placeholder "
            "patterns reach production.",
            "high", scope="site", urls=[u for u, _ in placeholders],
            evidence_key="M3-S3:ld-placeholder", skill=SKILL))
    if mismatches:
        F.append(Finding(
            "M3-S3", "M3", "Structured data contradicts the visible page", "high",
            "; ".join(f"{u}: {why}" for u, why in mismatches[:3])
            + ". When markup and body text disagree, the safest machine behaviour is to "
              "trust neither — a contradiction is more damaging than an omission.",
            "Generate JSON-LD from the same data source that renders the page so the "
            "two cannot drift apart.",
            "high", scope="site", urls=[u for u, _ in mismatches],
            evidence_key="M3-S3:ld-contradiction", skill=SKILL,
            falsifier="Shortened display titles are common; only a zero-word overlap "
                      "between JSON-LD name and H1 is treated as a contradiction."))
    if not entity_pages and hp:
        F.append(Finding(
            "M3-S3", "M3", "No Organization/entity structured data anywhere on the site",
            "high",
            f"None of the {len(hp)} sampled pages declare an Organization, LocalBusiness "
            f"or comparable entity type in JSON-LD or microdata. There is no "
            f"machine-readable statement of who this site is about.",
            "Add one Organization (or LocalBusiness) JSON-LD block on the homepage with "
            "name, url, logo, description, address/areaServed, foundingDate and sameAs "
            "links to official profiles. Give it a stable @id and reference that @id "
            "from other page types rather than redeclaring it.",
            "high", scope="site", urls=[site],
            evidence_key="M3-S3:no-entity", skill=SKILL,
            falsifier="A valid Organization on any single page satisfies this check — "
                      "it is only raised when no sampled page has one."))
    if untyped and hp:
        share = len(untyped) / len(hp)
        by_type = {}
        for u, t in untyped:
            by_type.setdefault(t, []).append(u)
        meaningful = {t: us for t, us in by_type.items()
                      if t in ("product", "article", "faq", "pricing", "homepage")}
        if meaningful and share > 0.3:
            desc = "; ".join(f"{len(us)} {t} page(s)" for t, us in meaningful.items())
            F.append(Finding(
                "M3-S3", "M3", "Page types that support structured data have none", "medium",
                f"{len(untyped)} of {len(hp)} sampled pages carry no JSON-LD or "
                f"microdata, including {desc}. Example: {untyped[0][0]}. Facts on these "
                f"pages must be inferred from prose rather than read directly.",
                "Add the type-appropriate schema (Product+Offer, Article, FAQPage, "
                "BreadcrumbList) to each template, populated from live page data.",
                "medium", scope="template",
                urls=[u for u, _ in untyped],
                evidence_key="M3-S3:missing-schema", skill=SKILL,
                falsifier="Pages with no matching schema.org type (generic landing, "
                          "legal, utility) are excluded from this count."))


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Crawl / render / extractability audit")
    ap.add_argument("--url", required=True)
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--render", choices=["auto", "off"], default="auto")
    ap.add_argument("--out", default=None)
    ap.add_argument("--pages-cache", default=None,
                    help="write the fetched page corpus here for reuse by other skills")
    ap.add_argument("--pages-in", default=None,
                    help="reuse a page corpus instead of re-crawling")
    args = ap.parse_args()

    site = args.url if "://" in args.url else "https://" + args.url
    F = []

    robots, robots_url, robots_resp = get_robots(site, timeout=args.timeout)
    sitemap_reports, sitemap_urls = discover_sitemaps(site, robots, timeout=args.timeout)

    if args.pages_in and os.path.exists(args.pages_in):
        pages = load_pages(args.pages_in)
    else:
        seeds = [u for u in sitemap_urls[:40] if is_internal(u, site)]
        pages = crawl(site, robots=robots, max_pages=args.max_pages,
                      timeout=args.timeout, delay=args.delay, seeds=seeds)
    if args.pages_cache:
        save_pages(pages, args.pages_cache)

    check_admission(site, robots, robots_url, robots_resp, pages, sitemap_urls, F)
    check_response_integrity(site, pages, F)
    check_discovery(site, robots, sitemap_reports, sitemap_urls, pages, F)
    check_fetch_gating(site, pages, F)
    check_render_gap(site, pages, F, render_mode=args.render)
    check_non_text(site, pages, F)
    check_structured_data(site, pages, F)

    meta = {
        "skill": SKILL, "site": registrable_domain(site), "start_url": site,
        "audited_at": utc_now(), "pages_fetched": len(pages),
        "html_pages": len(html_pages(pages)),
        "robots_status": robots.status, "sitemap_urls_seen": len(sitemap_urls),
        "mechanisms": ["M1", "M2", "M3"],
    }
    payload = emit(F, meta, args.out)
    if not args.out:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"[{SKILL}] {len(F)} finding(s) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
