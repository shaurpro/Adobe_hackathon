#!/usr/bin/env python3
"""
corroboration_probe.py — deterministic checks for mechanisms M4-M6.

M4 Cross-source corroboration (declared-identity round trip, internal
   contradiction, time decay)
M5 Entity disambiguation (ambiguity risk vs disambiguator supply, canonical
   entity anchor, naming consistency)
M6 Personalization (intent-variant coverage, context legibility)

Two classes of output:
  * findings[]      - things this script observed directly and can defend.
  * manual_probes[] - checks that require a live web search, which is
                      personalised and therefore not reproducible from a
                      script. The agent runs these, twice, and the
                      orchestrator demotes anything unstable (M6-S3).

Read-only. Outbound fetches are limited to profile URLs the site itself
declares via sameAs or footer links.

Usage:
  python3 corroboration_probe.py --url https://example.com \
      [--pages-in pages.json] [--out findings.json] [--max-profiles 8]
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auditlib import (  # noqa: E402
    Finding, crawl, discover_sitemaps, doc_for, emit, fetch, get_robots,
    html_pages, is_internal, load_pages, norm_text, registrable_domain,
    save_pages, tokens, utc_now,
)

SKILL = "freshness-corroboration"
YEAR_NOW = int(time.strftime("%Y"))

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?)?\d{3,4}[\s.-]?\d{3,4}(?:[\s.-]?\d{2,4})?")
COPYRIGHT_RE = re.compile(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", re.I)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
FUTURE_CLAIM_RE = re.compile(
    r"\b(coming (?:soon|in)|launching|available from|from)\s+"
    r"(q[1-4]\s*)?((19|20)\d{2})\b", re.I)
AS_OF_RE = re.compile(r"\bas of\s+(?:[a-z]+\s+)?((19|20)\d{2})\b", re.I)
NEWNESS_RE = re.compile(r"\b(new|brand[- ]new|just launched|newly released|latest)\b", re.I)
LEGAL_SUFFIX_RE = re.compile(
    r"\b(inc|inc\.|llc|ltd|ltd\.|limited|plc|gmbh|s\.a\.|sa|bv|nv|pty|pvt|"
    r"corp|corporation|co\.|company|ag|oy|ab|as|srl|sarl|kk|llp)\b", re.I)

# Small embedded list — enough to spot names that are ordinary words, without
# shipping a dictionary. Conservative by design: a miss here costs nothing,
# a false "your name is ambiguous" flag costs credibility.
COMMON_WORDS = set("""
apple orange summit vertex apex nova atlas nimbus cedar maple river delta echo
prism arc arcade orbit beacon anchor bridge compass forge harbor lantern lumen
meridian north pillar quarry ridge shore signal spark stone summit thread tide
vista willow canopy current element ember field frame grove haven horizon
impact keystone lattice legacy matrix motion mosaic outpost pace pinnacle
pioneer pivot plateau pulse quantum radius realm reef relay reserve rise
sequoia slate solstice sphere spire stack stream terrace thrive torch trail
trellis vantage venture vector vessel vibe vine vault wave wren zenith zephyr
core edge flow link loop node peak port rock sail sage scope shift stack swift
""".split())

INTENT_BUCKETS = {
    "what_it_is": re.compile(
        r"\b(what (?:we|it) (?:is|do|does)|about|overview|who we are|our story|"
        r"platform|product|solution|introduc)", re.I),
    "who_it_is_for": re.compile(
        r"\b(who (?:it|this) is for|for (?:teams|businesses|developers|agencies|"
        r"retailers|enterprises|schools|clinics)|use cases?|industries|customers|"
        r"audience|built for)\b", re.I),
    "how_much": re.compile(r"\b(pricing|price|plans?|cost|quote|rates?|fees?|tariff)\b", re.I),
    "where": re.compile(
        r"\b(contact|location|locations|address|find us|visit|offices?|"
        r"where we|coverage|areas? (?:we )?serve|shipping)\b", re.I),
    "how_it_compares": re.compile(
        r"\b(compare|comparison|vs\.?|versus|alternative|why (?:choose|us)|"
        r"differen(?:ce|tiator))\b", re.I),
    "how_to_start": re.compile(
        r"\b(get started|getting started|book a|demo|sign ?up|free trial|"
        r"how it works|onboard|request a|talk to)\b", re.I),
}

ENTITY_TYPES = {"organization", "corporation", "localbusiness", "onlinestore", "ngo",
                "educationalorganization", "governmentorganization", "brand",
                "restaurant", "store", "professionalservice", "medicalorganization"}

ARCHIVE_URL_RE = re.compile(r"/(blog|news|press|archive|20\d{2})/", re.I)


def ld_nodes_all(pages):
    out = []
    for p in html_pages(pages):
        doc = doc_for(p)
        nodes, _ = doc.jsonld()
        for n in nodes:
            out.append((p["url"], n))
    return out


def type_of(node):
    t = node.get("@type")
    if isinstance(t, list):
        return [str(x).lower() for x in t]
    return [str(t).lower()] if t else []


def brand_name(pages, site):
    """Best available machine-readable name for the entity, with its source."""
    for url, n in ld_nodes_all(pages):
        if set(type_of(n)) & ENTITY_TYPES and isinstance(n.get("name"), str):
            return n["name"].strip(), f"Organization JSON-LD on {url}"
    for p in html_pages(pages):
        doc = doc_for(p)
        v = doc.meta_value(prop="og:site_name")
        if v:
            return v.strip(), f"og:site_name on {p['url']}"
    for p in html_pages(pages):
        doc = doc_for(p)
        if doc.title:
            part = re.split(r"\s[|\-–—]\s", doc.title)[-1].strip()
            if 2 <= len(part) <= 40:
                return part, f"<title> suffix on {p['url']}"
    return registrable_domain(site).split(".")[0], "domain name"


# --------------------------------------------------------------------------
# M4-S2 — declared-identity round trip
# --------------------------------------------------------------------------

def check_sameas(site, pages, name, F, max_profiles=8, timeout=10):
    declared = []
    for url, n in ld_nodes_all(pages):
        sa = n.get("sameAs")
        if isinstance(sa, str):
            sa = [sa]
        if isinstance(sa, list):
            for s in sa:
                if isinstance(s, str) and s.startswith("http"):
                    declared.append(s)
    # Footer/header profile links as a fallback declaration surface.
    social_hosts = ("linkedin.", "x.com", "twitter.", "facebook.", "instagram.",
                    "youtube.", "github.", "crunchbase.", "wikipedia.", "tiktok.",
                    "mastodon", "threads.", "glassdoor.", "yelp.")
    footer_profiles = []
    for p in html_pages(pages)[:4]:
        for a in doc_for(p).anchors():
            if any(h in a["absolute"].lower() for h in social_hosts):
                footer_profiles.append(a["absolute"])
    declared = list(dict.fromkeys(declared))
    footer_profiles = list(dict.fromkeys(footer_profiles))

    if not declared:
        if footer_profiles:
            F.append(Finding(
                "M4-S2", "M4",
                "Official profiles are linked but not declared as sameAs", "medium",
                f"{len(footer_profiles)} external profile link(s) exist in the page "
                f"chrome (e.g. {footer_profiles[0]}) but no Organization sameAs array "
                f"declares them. The identity link is visual only, so a machine cannot "
                f"confirm that this site and that profile are the same entity.",
                "Add a sameAs array to the Organization JSON-LD listing every official "
                "profile URL, and ensure each profile links back to the site.",
                "medium", scope="site", urls=footer_profiles,
                evidence_key="M4-S2:no-sameas-declared", skill=SKILL))
        else:
            F.append(Finding(
                "M4-S2", "M4", "No declared identity links to any external profile", "medium",
                f"Neither Organization sameAs nor external profile links were found "
                f"across {len(html_pages(pages))} sampled HTML page(s). The site makes "
                f"no machine-readable claim about where else this entity exists, so "
                f"corroboration has no starting point.",
                "Claim and link the official profiles that already exist for this entity "
                "(company registry, LinkedIn, industry directories, Wikidata if "
                "notable) and list them in sameAs.",
                "medium", scope="site", urls=[site],
                evidence_key="M4-S2:no-external-identity", skill=SKILL,
                falsifier="A business with a deliberate no-social-presence policy is "
                          "not defective; the fix then is a registry/directory entry, "
                          "not a social account."))
        return

    broken, mismatched, unverified, ok = [], [], [], []
    name_toks = {t for t in tokens(name) if len(t) > 2}
    for u in declared[:max_profiles]:
        r = fetch(u, timeout=timeout)
        if r["status"] in (403, 429, 999) or r["error"]:
            unverified.append((u, r["status"] or r["error"]))
            continue
        if r["status"] and r["status"] >= 400:
            broken.append((u, r["status"]))
            continue
        page_toks = set(tokens(r["body"][:80_000]))
        if name_toks and not (name_toks & page_toks):
            mismatched.append((u, r["status"]))
        else:
            ok.append(u)
        time.sleep(0.2)

    if broken:
        F.append(Finding(
            "M4-S2", "M4", "Declared sameAs profile does not resolve", "high",
            "; ".join(f"{u} -> HTTP {s}" for u, s in broken[:4])
            + f". The site asserts these URLs are the same entity; a machine following "
              f"them to corroborate finds nothing, which weakens rather than "
              f"strengthens the identity claim.",
            "Remove dead profile URLs from sameAs and replace them with live ones. "
            "Re-verify sameAs targets on each release.",
            "high", scope="site", urls=[u for u, _ in broken],
            evidence_key="M4-S2:sameas-dead", skill=SKILL))
    if mismatched:
        F.append(Finding(
            "M4-S2", "M4", "Declared sameAs profile does not mention the brand name",
            "medium",
            "; ".join(f"{u} (HTTP {s}) contains none of the brand-name tokens "
                      f"'{' '.join(sorted(name_toks))}'" for u, s in mismatched[:3])
            + ". The declared corroborating source may point at the wrong account or a "
              "renamed entity.",
            "Verify each sameAs target is the current official profile and that the "
            "profile's display name matches the site's declared legal/trading name.",
            "medium", scope="site", urls=[u for u, _ in mismatched],
            confidence="inferred", evidence_key="M4-S2:sameas-mismatch", skill=SKILL,
            falsifier="Platforms that render profile names client-side will look empty "
                      "to a plain fetch — verify manually before acting."))
    if unverified:
        F.append(Finding(
            "M4-S2", "M4", "sameAs targets could not be verified automatically", "low",
            "; ".join(f"{u} -> {s}" for u, s in unverified[:4])
            + ". Recorded as unverified rather than broken: these platforms block "
              "automated fetches, so no conclusion is drawn.",
            "Confirm manually that these profiles are live and describe the same entity.",
            "low", scope="site", urls=[u for u, _ in unverified],
            confidence="unverified", evidence_key="M4-S2:sameas-unverified", skill=SKILL))


# --------------------------------------------------------------------------
# M4-S3 — internal contradiction and time decay
# --------------------------------------------------------------------------

def check_contradiction_and_staleness(site, pages, name, F):
    emails, phones, years_founded, addresses = {}, {}, {}, {}
    stale_pages, future_claims, copyright_years = [], [], {}
    hp = html_pages(pages)

    for p in hp:
        doc = doc_for(p)
        text = doc.all_text
        for e in set(EMAIL_RE.findall(text)):
            if not re.search(r"\.(png|jpg|gif|webp|svg)$", e, re.I):
                emails.setdefault(e.lower(), []).append(p["url"])
        m = COPYRIGHT_RE.search(text)
        if m:
            copyright_years.setdefault(int(m.group(1)), []).append(p["url"])
        for mm in AS_OF_RE.finditer(doc.main_text):
            y = int(mm.group(1))
            if y < YEAR_NOW - 1:
                stale_pages.append((p["url"], f"'as of {y}' claim, {YEAR_NOW - y} years old"))
        for mm in FUTURE_CLAIM_RE.finditer(doc.main_text):
            y = int(mm.group(3))
            if y < YEAR_NOW:
                future_claims.append((p["url"], f"'{mm.group(0)[:40]}' — date now past"))
        nodes, _ = doc.jsonld()
        for n in nodes:
            if set(type_of(n)) & ENTITY_TYPES:
                fd = n.get("foundingDate")
                if isinstance(fd, str):
                    ym = YEAR_RE.search(fd)
                    if ym:
                        years_founded.setdefault(ym.group(0), []).append(p["url"])
                addr = n.get("address")
                if isinstance(addr, dict):
                    key = norm_text(" ".join(str(addr.get(k, "")) for k in (
                        "streetAddress", "addressLocality", "postalCode"))).lower()
                    if key.strip():
                        addresses.setdefault(key, []).append(p["url"])
                tel = n.get("telephone")
                if isinstance(tel, str) and tel.strip():
                    phones.setdefault(re.sub(r"\D", "", tel)[-10:], []).append(p["url"])

    contradictions = []
    if len(years_founded) > 1:
        contradictions.append(
            f"foundingDate declared as {', '.join(sorted(years_founded))} on different pages")
    if len(addresses) > 1:
        contradictions.append(
            f"{len(addresses)} different postal addresses declared in Organization markup")
    if len(phones) > 1:
        contradictions.append(
            f"{len(phones)} different telephone numbers declared in Organization markup")
    generic_emails = {e for e in emails if re.match(
        r"^(info|hello|contact|sales|support|enquiries|admin)@", e)}
    if len(generic_emails) > 2:
        contradictions.append(
            f"{len(generic_emails)} different general-enquiry email addresses "
            f"({', '.join(sorted(generic_emails)[:4])})")

    if contradictions:
        F.append(Finding(
            "M4-S3", "M4", "The site contradicts itself on core entity facts", "high",
            "; ".join(contradictions)
            + ". Retrieval systems resolve conflicts by lowering confidence in all "
              "versions, so a contradiction suppresses the fact more effectively than "
              "silence would.",
            "Pick one authoritative value per field, hold it in a single source of "
            "truth (the CMS entity record), and render every surface — footer, contact "
            "page, JSON-LD, external profiles — from that record.",
            "high", scope="site", urls=[p["url"] for p in hp[:5]],
            evidence_key="M4-S3:internal-contradiction", skill=SKILL,
            falsifier="Department-specific phone numbers and regional offices are not "
                      "contradictions; only like-typed Organization-level fields are "
                      "compared here."))

    # Staleness: require a second corroborating signal beyond the copyright year.
    stale_signals = []
    if copyright_years:
        newest = max(copyright_years)
        if newest < YEAR_NOW - 1:
            stale_signals.append(
                f"footer copyright reads {newest} on {len(copyright_years[newest])} page(s)")
    non_archive_stale = [(u, why) for u, why in stale_pages
                         if not ARCHIVE_URL_RE.search(u)]
    if non_archive_stale:
        stale_signals.append(
            "dated claims: " + "; ".join(f"{u} ({why})" for u, why in non_archive_stale[:3]))
    if future_claims:
        stale_signals.append(
            "expired forward-looking copy: "
            + "; ".join(f"{u} ({why})" for u, why in future_claims[:3]))

    if len(stale_signals) >= 2 or future_claims:
        sev = "medium"
        F.append(Finding(
            "M4-S3", "M4", "On-page claims have visibly decayed", sev,
            " | ".join(stale_signals)
            + ". Visible staleness lowers the odds a retrieval system prefers this page "
              "as a current source, and expired forward-looking copy directly "
              "misinforms anyone who does read it.",
            "Rewrite or remove expired claims, replace 'as of <year>' phrasing with a "
            "dated statement you commit to updating, automate the footer year, and put "
            "a visible 'last reviewed' date on fact-bearing pages.",
            sev, scope="site",
            urls=[u for u, _ in (non_archive_stale + future_claims)][:8] or [site],
            evidence_key="M4-S3:staleness", skill=SKILL,
            falsifier="Dated blog/press URLs are excluded — old posts are supposed to "
                      "be old. A stale copyright year alone is not enough to flag; a "
                      "second independent signal is required."))
    elif len(stale_signals) == 1 and copyright_years and max(copyright_years) < YEAR_NOW - 1:
        F.append(Finding(
            "M4-S3", "M4", "Footer copyright year is out of date (weak signal)", "low",
            f"{stale_signals[0]}. On its own this is cosmetic, but it is the cheapest "
            f"visible freshness cue a reader or crawler encounters.",
            "Render the copyright year dynamically.",
            "low", scope="site", urls=[site],
            evidence_key="M4-S3:copyright-only", skill=SKILL))


# --------------------------------------------------------------------------
# M5-S1 / M5-S2 / M5-S3 — entity disambiguation
# --------------------------------------------------------------------------

def check_entity(site, pages, name, name_source, F, probes):
    hp = html_pages(pages)
    nodes = ld_nodes_all(pages)
    org_nodes = [(u, n) for u, n in nodes if set(type_of(n)) & ENTITY_TYPES]

    # -- M5-S1: ambiguity risk vs disambiguator supply
    name_tokens = tokens(name)
    ambiguous = (
        len(name_tokens) == 1 and name_tokens[0] in COMMON_WORDS
    ) or (
        len(name_tokens) == 2 and all(t in COMMON_WORDS for t in name_tokens)
    )
    disambiguators = {}
    for u, n in org_nodes:
        if n.get("foundingDate"):
            disambiguators["foundingDate"] = u
        if n.get("address"):
            disambiguators["address"] = u
        if n.get("areaServed"):
            disambiguators["areaServed"] = u
        if n.get("@id"):
            disambiguators["@id"] = u
        for k in ("naics", "iso6523Code", "taxID", "leiCode", "duns",
                  "identifier", "tickerSymbol", "vatID", "legalName"):
            if n.get(k):
                disambiguators[k] = u
        if LEGAL_SUFFIX_RE.search(str(n.get("name", "")) + str(n.get("legalName", ""))):
            disambiguators["legal_suffix"] = u
        if n.get("description") or n.get("knowsAbout") or n.get("industry"):
            disambiguators["category_descriptor"] = u
    if LEGAL_SUFFIX_RE.search(name):
        disambiguators.setdefault("legal_suffix", name_source)

    if ambiguous and len(disambiguators) < 2:
        F.append(Finding(
            "M5-S1", "M5", "Generic brand name with no machine-readable disambiguators",
            "high",
            f"The entity name '{name}' (source: {name_source}) is composed of ordinary "
            f"dictionary words, and only {len(disambiguators)} disambiguating property "
            f"({', '.join(disambiguators) or 'none'}) is present in structured data. "
            f"Nothing distinguishes this entity from others sharing the name, so "
            f"assistants can attribute a competitor's or an unrelated organisation's "
            f"facts to this brand.",
            "Co-locate at least two hard disambiguators with the name wherever it is "
            "declared: legal name with suffix, city/country, founding year, industry "
            "descriptor, and a registration identifier where one exists. Use a stable "
            "@id URI for the Organization and reference it everywhere.",
            "high", scope="site", urls=[site],
            evidence_key="M5-S1:ambiguous-name", skill=SKILL,
            falsifier="Coined, high-entropy names do not need this; the check only "
                      "fires when the name is made of common words AND fewer than two "
                      "disambiguators exist."))
    elif len(disambiguators) < 2 and org_nodes:
        F.append(Finding(
            "M5-S1", "M5", "Entity markup carries few disambiguating properties", "low",
            f"'{name}' is declared with {len(disambiguators)} disambiguating property "
            f"({', '.join(disambiguators) or 'none'}). The name itself is distinctive "
            f"enough that confusion is unlikely today, but adding location, founding "
            f"year and a category descriptor makes the entity resolvable as the brand "
            f"grows and as similarly-named entities appear.",
            "Add address/areaServed, foundingDate and a one-line description to the "
            "Organization node.",
            "low", scope="site", urls=[site],
            evidence_key="M5-S1:thin-disambiguators", skill=SKILL))

    # -- M5-S2: canonical entity anchor
    ids = {}
    for u, n in org_nodes:
        nid = n.get("@id")
        if nid:
            ids.setdefault(nid, []).append((u, n))
    conflicting = []
    for nid, items in ids.items():
        names = {str(n.get("name", "")).strip() for _, n in items if n.get("name")}
        urls = {str(n.get("url", "")).strip() for _, n in items if n.get("url")}
        if len(names) > 1 or len(urls) > 1:
            conflicting.append((nid, names, urls))
    if conflicting:
        F.append(Finding(
            "M5-S2", "M5", "One entity @id carries conflicting values", "high",
            "; ".join(f"@id {nid} declared with names {sorted(ns)} and urls {sorted(us)}"
                      for nid, ns, us in conflicting[:2])
            + ". A shared identifier with drifting values makes the entity harder to "
              "resolve than having no identifier at all.",
            "Emit the Organization node once from a shared template and reference it by "
            "@id elsewhere, rather than re-declaring it per page.",
            "high", scope="site", urls=[site],
            evidence_key="M5-S2:id-conflict", skill=SKILL))
    elif org_nodes and not ids:
        F.append(Finding(
            "M5-S2", "M5", "Organization markup has no stable @id", "low",
            f"Organization-type markup exists on {len(org_nodes)} page(s) but declares "
            f"no @id, so each occurrence is a separate anonymous node rather than "
            f"repeated evidence about one entity.",
            "Give the Organization a stable @id (e.g. https://site/#organization) and "
            "reference that @id from Product, Article and Breadcrumb nodes.",
            "low", scope="site", urls=[site],
            evidence_key="M5-S2:no-id", skill=SKILL))

    has_hub = any(re.search(r"/(about|company|who-we-are|our-story|team|contact)",
                            p["url"], re.I) for p in hp)
    if not has_hub and len(hp) >= 4:
        F.append(Finding(
            "M5-S2", "M5", "No canonical entity page (about/company)", "medium",
            f"None of the {len(hp)} crawled pages is an about/company/contact page. "
            f"There is no single URL that states who the entity is, where it is and "
            f"what it does — the page a retrieval system would most naturally cite for "
            f"identity facts.",
            "Publish one entity hub page carrying the legal name, founding year, "
            "location, category, leadership and contact details in plain text, and link "
            "it from the primary navigation.",
            "medium", scope="site", urls=[site],
            evidence_key="M5-S2:no-entity-hub", skill=SKILL,
            falsifier="Single-page sites that state these facts on the homepage satisfy "
                      "this; the check requires 4+ crawled pages before firing."))

    # -- M5-S3: naming consistency
    variants = {}
    for u, n in org_nodes:
        if isinstance(n.get("name"), str):
            variants.setdefault(norm_text(n["name"]), []).append("JSON-LD name")
    alternates = set()
    for _, n in org_nodes:
        alt = n.get("alternateName")
        if isinstance(alt, str):
            alternates.add(norm_text(alt))
        elif isinstance(alt, list):
            alternates |= {norm_text(str(a)) for a in alt}
    for p in hp[:6]:
        doc = doc_for(p)
        sn = doc.meta_value(prop="og:site_name")
        if sn:
            variants.setdefault(norm_text(sn), []).append("og:site_name")
        if doc.title:
            head = re.split(r"\s[|\-–—]\s", doc.title)[0].strip()
            tail = re.split(r"\s[|\-–—]\s", doc.title)[-1].strip()
            for cand in {head, tail}:
                if cand and set(tokens(cand)) & set(name_tokens):
                    variants.setdefault(norm_text(cand), []).append("<title>")

    def _key(s):
        return " ".join(t for t in tokens(s) if not LEGAL_SUFFIX_RE.fullmatch(t))
    families = {}
    for v, srcs in variants.items():
        families.setdefault(_key(v), set()).add(v)
    undeclared = []
    for fam, vs in families.items():
        vs = {v for v in vs if v}
        if len(vs) >= 2 and not (vs & alternates):
            undeclared.append(sorted(vs))
    if undeclared and max(len(v) for v in undeclared) >= 3:
        F.append(Finding(
            "M5-S3", "M5", "Brand name appears in several undeclared variants", "medium",
            "Variants found across title/og:site_name/JSON-LD: "
            + "; ".join(str(v) for v in undeclared[:2])
            + ". None are declared as alternateName, so a machine cannot tell whether "
              "these are one entity or several.",
            "Choose one canonical name for the Organization node and declare every "
            "legitimate variant (trading name, abbreviation, legal name) in "
            "alternateName.",
            "medium", scope="site", urls=[site],
            evidence_key="M5-S3:name-variants", skill=SKILL,
            falsifier="Variants already listed in alternateName are reconcilable and "
                      "are not counted; localised names under correct hreflang are "
                      "also excluded."))

    # -- M4-S1 / M5-S1 external half: hand off to the agent as replayable probes
    probes.append({
        "id": "P-CORROBORATION",
        "signal": "M4-S1",
        "purpose": "Count independent domains that restate the entity's core identity claim.",
        "queries": [f'"{name}" {registrable_domain(site)}',
                    f'"{name}" company profile',
                    f'what is "{name}"'],
        "method": "Run each query. Count DISTINCT registrable domains, excluding "
                  f"{registrable_domain(site)} and its subdomains, that state the same "
                  "category/identity claim. Discard press-release syndication copies "
                  "and scraped directory clones (near-identical wording).",
        "flag_if": "0 independent domains restate the primary identity claim.",
        "severity_ceiling": "medium",
        "reason_for_ceiling": "Evidence comes from a personalised, non-reproducible "
                              "surface (M6-S3), so severity is capped and the finding "
                              "must be stable across two runs.",
        "run_twice": True,
    })
    probes.append({
        "id": "P-AMBIGUITY",
        "signal": "M5-S1",
        "purpose": "Confirm real-world name collision before flagging ambiguity.",
        "queries": [f'"{name}"', f'{name} {registrable_domain(site)}'],
        "method": "Inspect the first page of results. Record how many distinct "
                  "entity types/organisations share this name.",
        "flag_if": "Two or more unrelated entities share the name AND the site "
                   "supplies fewer than two disambiguators (see M5-S1 above).",
        "severity_ceiling": "medium",
        "run_twice": True,
    })


# --------------------------------------------------------------------------
# M6-S1 / M6-S2 — personalization readiness
# --------------------------------------------------------------------------

def check_personalization(site, pages, F):
    hp = html_pages(pages)
    covered = {}
    for p in hp:
        doc = doc_for(p)
        surface = " ".join(
            [doc.title or ""]
            + [h["text"] for h in doc.headings]
            + [a["text"] for a in doc.anchors()]
            + [p["url"]])
        for bucket, rx in INTENT_BUCKETS.items():
            if rx.search(surface):
                covered.setdefault(bucket, []).append(p["url"])
    missing = [b for b in INTENT_BUCKETS if b not in covered]

    if len(hp) >= 4 and len(missing) >= 2:
        worst = missing[:2] if len(missing) > 2 else missing
        readable = {
            "what_it_is": "what the product/organisation actually is",
            "who_it_is_for": "who it is for",
            "how_much": "what it costs (even a pricing model without numbers)",
            "where": "where it operates / how to reach it",
            "how_it_compares": "how it compares to alternatives",
            "how_to_start": "how to get started",
        }
        F.append(Finding(
            "M6-S1", "M6", "Common question intents have no page or heading that answers them",
            "medium",
            f"Across {len(hp)} crawled pages, no title, heading or nav link addresses: "
            + ", ".join(readable[b] for b in worst)
            + f". Covered intents: {', '.join(covered) or 'none'}. Because assistants "
              f"answer differently-framed versions of the same question depending on who "
              f"is asking, an uncovered intent is a class of query this site can never "
              f"be the answer to.",
            "Add a page or a clearly-headed section per uncovered intent, phrased as the "
            "question a person would actually ask, with the answer stated in the first "
            "sentence beneath it.",
            "medium", scope="site", urls=[site],
            evidence_key="M6-S1:intent-gap", skill=SKILL,
            falsifier="Single-product microsites are exempt (4+ pages required), and no "
                      "more than the two weakest buckets are reported. Withholding "
                      "pricing may be a deliberate sales strategy — treat that one as a "
                      "recommendation, not a defect."))
    elif len(missing) == 1 and len(hp) >= 4:
        F.append(Finding(
            "M6-S1", "M6", "One question intent is unaddressed (proactive)", "low",
            f"No heading, title or nav link addresses '{missing[0]}'. Everything else is "
            f"covered.",
            "Add a short, clearly-headed section answering that question directly.",
            "low", scope="site", urls=[site],
            evidence_key="M6-S1:intent-gap-minor", skill=SKILL))

    # M6-S2 — context legibility
    langs, hreflang_pairs, geo_declared, audience_declared = set(), [], False, False
    for p in hp:
        doc = doc_for(p)
        if doc.lang:
            langs.add(doc.lang.lower())
        hreflang_pairs.extend(doc.hreflangs())
        nodes, _ = doc.jsonld()
        for n in nodes:
            if n.get("areaServed") or n.get("address") or n.get("location"):
                geo_declared = True
            if n.get("audience") or n.get("targetAudience"):
                audience_declared = True
        if re.search(r"\b(serving|based in|offices in|available in|we serve|"
                     r"shipping to|clients across)\b", doc.main_text, re.I):
            geo_declared = True

    missing_lang = [p["url"] for p in hp if not doc_for(p).lang]
    if missing_lang:
        F.append(Finding(
            "M6-S2", "M6", "Pages do not declare a language", "low",
            f"{len(missing_lang)} of {len(hp)} pages have no lang attribute on <html> "
            f"(e.g. {missing_lang[0]}). Language is one of the cheapest signals a "
            f"system uses to decide whether this page suits a given user.",
            "Set <html lang> on every page from the CMS locale.",
            "low", scope="site", urls=missing_lang,
            evidence_key="M6-S2:no-lang", skill=SKILL))
    if not geo_declared:
        F.append(Finding(
            "M6-S2", "M6", "No machine-readable statement of where the entity operates",
            "medium",
            f"No areaServed, PostalAddress or explicit coverage statement was found in "
            f"structured data or body text across {len(hp)} sampled page(s). Location is a "
            "primary personalisation axis: a system deciding whether to surface this "
            "brand for a user in a given place has nothing to match against.",
            "Declare areaServed (or a PostalAddress for a physical business) in the "
            "Organization JSON-LD and state the served geography in plain text on the "
            "homepage and contact page.",
            "medium", scope="site", urls=[site],
            evidence_key="M6-S2:no-geo", skill=SKILL,
            falsifier="Any areaServed/address property or an explicit 'serving X' "
                      "sentence suppresses this entirely."))
    if len(langs) > 1 and not hreflang_pairs:
        F.append(Finding(
            "M6-S2", "M6", "Multiple content languages with no hreflang mapping", "medium",
            f"Pages declare {sorted(langs)} but no rel=alternate hreflang links connect "
            f"them, so equivalent pages compete rather than being recognised as "
            f"localised versions of one another.",
            "Add reciprocal hreflang annotations (including x-default) across every "
            "localised set.",
            "medium", scope="site", urls=[site],
            evidence_key="M6-S2:no-hreflang", skill=SKILL,
            falsifier="Single-language sites need no hreflang and are not flagged."))
    if not audience_declared:
        F.append(Finding(
            "M6-S2", "M6", "Intended audience is never stated explicitly (proactive)", "low",
            f"No audience/targetAudience property and no explicit 'for <who>' statement "
            f"was detected across {len(hp)} sampled page(s). Stating the audience "
            f"plainly helps a personalising system decide which users this brand is a "
            f"good answer for.",
            "Add one sentence naming the intended customer ('for operations teams at "
            "retailers with 10-200 stores') near the top of the homepage, and mirror it "
            "in the Organization description.",
            "low", scope="site", urls=[site],
            evidence_key="M6-S2:no-audience", skill=SKILL))


def main():
    ap = argparse.ArgumentParser(description="Corroboration, entity and context audit")
    ap.add_argument("--url", required=True)
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--max-profiles", type=int, default=8)
    ap.add_argument("--out", default=None)
    ap.add_argument("--pages-in", default=None)
    ap.add_argument("--pages-cache", default=None)
    ap.add_argument("--no-external", action="store_true",
                    help="skip outbound sameAs verification fetches")
    args = ap.parse_args()

    site = args.url if "://" in args.url else "https://" + args.url
    F, probes = [], []

    if args.pages_in and os.path.exists(args.pages_in):
        pages = load_pages(args.pages_in)
    else:
        robots, _, _ = get_robots(site, timeout=args.timeout)
        _, sm_urls = discover_sitemaps(site, robots, timeout=args.timeout)
        pages = crawl(site, robots=robots, max_pages=args.max_pages,
                      timeout=args.timeout, delay=args.delay,
                      seeds=[u for u in sm_urls[:40] if is_internal(u, site)])
        if args.pages_cache:
            save_pages(pages, args.pages_cache)

    name, name_source = brand_name(pages, site)
    if not args.no_external:
        check_sameas(site, pages, name, F, max_profiles=args.max_profiles,
                     timeout=args.timeout)
    check_contradiction_and_staleness(site, pages, name, F)
    check_entity(site, pages, name, name_source, F, probes)
    check_personalization(site, pages, F)

    meta = {
        "skill": SKILL, "site": registrable_domain(site), "start_url": site,
        "audited_at": utc_now(), "entity_name": name, "entity_name_source": name_source,
        "pages_analysed": len(html_pages(pages)),
        "mechanisms": ["M4", "M5", "M6"],
        "manual_probes": probes,
    }
    payload = emit(F, meta, args.out)
    if not args.out:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"[{SKILL}] {len(F)} finding(s), {len(probes)} probe(s) -> {args.out}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
