#!/usr/bin/env python3
"""
engagement_audit.py — deterministic checks for on-site friction (signals E1-E6).

Engagement is not a separate theory. A human landing on a page runs the same
three-stage gate a crawler does, just slower and with less patience:

  let in (E3 entry friction)  ->  able to read (E2 structure, E4 noise, E6 labels)
                              ->  able to extract the one fact they came for
                                  (E1 orientation, E5 next step)

So each signal below is a human-parser instantiation of M1/M2/M3, and several
produce evidence that overlaps a crawl-render finding. That overlap is
deliberate and is resolved by the orchestrator's supersedes table, not here:
this skill reports what the visitor experiences and declares the overlap via
`supersedes_hint`.

Read-only. Reuses the shared page corpus; performs no fetches of its own when
--pages-in is supplied.

Usage:
  python3 engagement_audit.py --url https://example.com \
      [--pages-in pages.json] [--out findings.json] [--max-pages 20]
"""

import argparse
import json
import os
import re
import statistics
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from auditlib import (  # noqa: E402
    Finding, crawl, discover_sitemaps, doc_for, emit, get_robots, html_pages,
    is_internal, load_pages, norm_text, registrable_domain, save_pages, tokens,
    utc_now,
)

SKILL = "engagement-audit"

# High-confidence modal/interstitial markers. Deliberately narrow: a generic
# "modal" class name appears in half the CSS frameworks on earth and would
# generate false positives on sites with no popup at all.
INTERSTITIAL_MARKERS = re.compile(
    r"(newsletter[- _]?(popup|modal|overlay)|exit[- _]?intent|"
    r"subscribe[- _]?(popup|modal|overlay)|age[- _]?(gate|verification)|"
    r"welcome[- _]?mat|interstitial|lightbox[- _]?signup|"
    r"data-popup|id=[\"']popup|class=[\"'][^\"']*popup-overlay)", re.I)

AGE_GATE_TEXT = re.compile(
    r"\b(are you (over|at least) \d{2}|confirm your age|enter your date of birth|"
    r"you must be \d{2})\b", re.I)

GENERIC_LINK_TEXT = {
    "click here", "here", "read more", "more", "learn more", "this", "link",
    "continue", "details", "more info", "find out more", "see more", "go",
}

CTA_RE = re.compile(
    r"\b(get started|start (?:free|now)|book (?:a|your)|request (?:a|your)|"
    r"contact us|talk to|schedule|sign ?up|try (?:it|for)|buy|order|subscribe|"
    r"download|get (?:a )?quote|apply|register|demo|free trial|add to (?:cart|bag)|"
    r"call us|email us|enquire|get in touch)\b", re.I)

CONTACT_RE = re.compile(
    r"(mailto:|tel:|\b(contact|support|help|get in touch|enquiries)\b)", re.I)

# A quotable claim binds entity + category + differentiator in one sentence.
# We test structurally (a copula plus a category noun) rather than semantically.
COPULA_RE = re.compile(
    r"\b(is|are|provides?|offers?|builds?|makes?|helps?|delivers?|specialis[ez]es?|"
    r"designs?|creates?|sells?|supplies|manufactures?|serves?|enables?)\b", re.I)
CATEGORY_RE = re.compile(
    r"\b(company|agency|studio|firm|platform|software|tool|service|services|"
    r"consultancy|clinic|practice|shop|store|restaurant|charity|nonprofit|"
    r"manufacturer|supplier|retailer|marketplace|app|solution|provider|brand|"
    r"school|institute|team|business|startup)\b", re.I)

LEGAL_PATH_RE = re.compile(
    r"/(privacy|terms|legal|cookie|gdpr|accessibility|imprint|impressum|"
    r"disclaimer|policy|policies)", re.I)
LISTING_PATH_RE = re.compile(
    r"/(blog|news|category|categories|tag|tags|archive|shop|products|"
    r"collections|search|index|sitemap|directory|listing)s?(/|$)", re.I)

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def page_kind(url, doc):
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    if path in ("", "/"):
        return "homepage"
    if LEGAL_PATH_RE.search(path):
        return "legal"
    if LISTING_PATH_RE.search(path):
        return "listing"
    return "content"


def first_n_words(text, n=200):
    t = text.split()
    return " ".join(t[:n])


def has_quotable_claim(text):
    """A self-contained declarative sentence binding entity + category."""
    for s in SENTENCE_SPLIT.split(text)[:12]:
        w = len(tokens(s))
        if 6 <= w <= 60 and COPULA_RE.search(s) and CATEGORY_RE.search(s):
            return s.strip()
    return None


# --------------------------------------------------------------------------
# E1 — Orientation: can a visitor tell what this is, in the first screen?
# --------------------------------------------------------------------------

def check_orientation(site, pages, F):
    hp = html_pages(pages)
    home = [p for p in hp if urllib.parse.urlsplit(p["url"]).path.rstrip("/") in ("", "/")]
    target = home[0] if home else (hp[0] if hp else None)
    if not target:
        return
    doc = doc_for(target)
    above = first_n_words(doc.main_text, 200)
    claim_above = has_quotable_claim(above)
    if claim_above:
        return  # orientation present — nothing to report

    # Guard: search the WHOLE machine-readable surface before flagging as missing.
    elsewhere = []
    whole = doc.main_text
    claim_anywhere = has_quotable_claim(whole)
    if claim_anywhere:
        elsewhere.append(("further down the same page", claim_anywhere))
    md = doc.meta_value(name="description")
    if md and has_quotable_claim(md):
        elsewhere.append(("meta description", md))
    nodes, _ = doc.jsonld()
    for n in nodes:
        d = n.get("description")
        if isinstance(d, str) and has_quotable_claim(d):
            elsewhere.append(("JSON-LD description", d))
            break
    for p in hp:
        if p is target:
            continue
        if re.search(r"/(about|company|who-we-are|our-story)", p["url"], re.I):
            c = has_quotable_claim(doc_for(p).main_text)
            if c:
                elsewhere.append((f"about page ({p['url']})", c))
                break

    wc = len(tokens(doc.main_text))
    if elsewhere:
        where, sample = elsewhere[0]
        F.append(Finding(
            "E1", "M2",
            "The core positioning statement exists but not where visitors look first",
            "medium",
            f"The first 200 words of {target['url']} contain no self-contained sentence "
            f"stating what this organisation is. One does exist in the {where}: "
            f"\"{sample[:120]}\". A visitor deciding whether to stay, and an assistant "
            f"looking for one clean sentence to quote, both read the top of the page "
            f"first.",
            "Move the positioning sentence into the first screen, as body text (not an "
            "image, not a slogan). One sentence naming the entity, its category and its "
            "differentiator: '<Name> is a <category> that <does what> for <whom>'.",
            "medium", scope="page", urls=[target["url"]],
            evidence_key="E1:claim-buried", skill=SKILL))
    else:
        sev = "high"
        note = ""
        if wc < 80:
            note = (f" Note: the page carries only {wc} words of body text in the server "
                    f"response, so this may be a downstream effect of client-side "
                    f"rendering rather than a copy problem.")
        F.append(Finding(
            "E1", "M2",
            "No plain-language statement of what this organisation is", sev,
            f"No sentence on {target['url']} — in body text, meta description, JSON-LD "
            f"description, or the about page — binds the entity to a category and a "
            f"differentiator. Body text sampled: \"{above[:160]}\".{note} A visitor "
            f"cannot orient in the first seconds, and an assistant has nothing "
            f"quotable to lift.",
            "Write one explicit sentence near the top of the homepage: '<Name> is a "
            "<category> that <does what> for <whom>, based in <where>.' Mirror it in the "
            "meta description and the Organization JSON-LD description so the human and "
            "machine versions cannot drift apart.",
            "high", scope="page", urls=[target["url"]],
            evidence_key="E1:no-claim", skill=SKILL,
            falsifier="Suppressed if any such sentence exists anywhere machine-readable "
                      "— that case is reported as 'buried', not 'missing'.")
            | ({"supersedes_hint": "M3-S1:client-render"} if wc < 80 else {}))


# --------------------------------------------------------------------------
# E2 — Answer-shaped structure
# --------------------------------------------------------------------------

def check_structure(site, pages, F):
    unstructured, heading_problems, wall_of_text = [], [], []
    for p in html_pages(pages):
        doc = doc_for(p)
        kind = page_kind(p["url"], doc)
        if kind in ("legal", "listing"):
            continue  # guard: these page types are legitimately different
        wc = len(tokens(doc.main_text))
        if wc < 150:
            continue  # too thin to judge structure; other signals cover it
        body_headings = [h for h in doc.headings if not h["in_chrome"]]
        subs = [h for h in body_headings if h["level"] >= 2]
        paras = [b for b in doc.main_blocks if len(tokens(b)) > 15]
        mean_para = statistics.mean([len(tokens(b)) for b in paras]) if paras else 0

        if len(subs) == 0 and wc >= 400:
            unstructured.append((p["url"], wc))
        if mean_para > 120 and len(paras) >= 3:
            wall_of_text.append((p["url"], int(mean_para)))

        h1s = [h for h in doc.headings if h["level"] == 1]
        if len(h1s) == 0:
            heading_problems.append((p["url"], "no H1"))
        elif len(h1s) > 1:
            heading_problems.append((p["url"], f"{len(h1s)} H1 elements"))
        else:
            levels = [h["level"] for h in doc.headings]
            for a, b in zip(levels, levels[1:]):
                if b - a > 1:
                    heading_problems.append((p["url"], f"heading jumps H{a}->H{b}"))
                    break

    if unstructured:
        F.append(Finding(
            "E2", "M2", "Long pages with no sub-headings to navigate by", "medium",
            "; ".join(f"{u} ({wc} words, zero H2/H3)" for u, wc in unstructured[:4])
            + ". Visitors scan for the heading that matches their question and leave "
              "when they cannot find one; extraction systems use headings to isolate "
              "which passage answers which question.",
            "Break long pages with question-shaped sub-headings ('How much does it "
            "cost?', 'Who is it for?') and answer each one in the first sentence "
            "beneath it.",
            "medium", scope="template", urls=[u for u, _ in unstructured],
            evidence_key="E2:no-subheadings", skill=SKILL,
            falsifier="Legal and listing templates are excluded, as are pages under 400 "
                      "words where a flat structure is appropriate."))
    if wall_of_text:
        F.append(Finding(
            "E2", "M2", "Dense paragraphs reduce scanability", "low",
            "; ".join(f"{u} (mean {n} words per paragraph)" for u, n in wall_of_text[:4])
            + ". Long unbroken paragraphs bury the specific fact inside prose that must "
              "be read linearly.",
            "Split paragraphs at one idea each, and lift specifications, prices and "
            "comparisons into lists or tables where the value is the point.",
            "low", scope="template", urls=[u for u, _ in wall_of_text],
            evidence_key="E2:dense-paragraphs", skill=SKILL,
            falsifier="Long-form editorial and narrative pages legitimately run long; "
                      "this is reported at low severity for that reason."))
    if heading_problems:
        by_kind = {}
        for u, why in heading_problems:
            by_kind.setdefault(why.split()[0], []).append((u, why))
        sev = "medium" if len(heading_problems) >= 3 else "low"
        F.append(Finding(
            "E2", "M2", "Heading hierarchy is broken or missing", sev,
            f"{len(heading_problems)} page(s) affected: "
            + "; ".join(f"{u} ({why})" for u, why in heading_problems[:4])
            + ". The heading outline is the document's table of contents for both "
              "screen readers and extraction systems.",
            "Give every page exactly one H1 naming the page's subject, and nest H2/H3 "
            "without skipping levels.",
            sev, scope="site", urls=[u for u, _ in heading_problems],
            evidence_key="E2:heading-hierarchy", skill=SKILL))


# --------------------------------------------------------------------------
# E3 — Entry friction: is the human let in?
# --------------------------------------------------------------------------

def check_entry_friction(site, pages, F):
    interstitials, age_gates, no_viewport, heavy = [], [], [], []
    for p in html_pages(pages):
        html = p.get("html", "")
        doc = doc_for(p)
        wc = len(tokens(doc.main_text))
        if INTERSTITIAL_MARKERS.search(html):
            interstitials.append((p["url"], wc))
        if AGE_GATE_TEXT.search(doc.all_text):
            age_gates.append(p["url"])
        if not doc.viewport:
            no_viewport.append(p["url"])
        blocking = [s for s in doc.scripts
                    if s["src"] and not s["async"] and not s["defer"]]
        if len(blocking) >= 8 or p.get("raw_len", 0) > 1_500_000:
            heavy.append((p["url"], len(blocking), p.get("raw_len", 0)))

    if interstitials:
        # Guard: an overlay over readable content is friction, not a wall.
        walls = [(u, wc) for u, wc in interstitials if wc < 60]
        sev = "high" if walls else "medium"
        detail = ("; ".join(f"{u} (only {wc} words of body text behind it)"
                            for u, wc in walls[:3]) if walls
                  else "; ".join(f"{u} (content readable underneath)"
                                 for u, _ in interstitials[:3]))
        F.append(Finding(
            "E3", "M1", "Interstitial or popup intercepts arriving visitors", sev,
            f"{len(interstitials)} page(s) ship high-confidence popup/interstitial "
            f"markup: {detail}. An overlay shown before the visitor has read anything "
            f"asks for commitment before delivering value, and is the single most "
            f"common cause of immediate bounce."
            + (" Here the content behind it is also effectively empty, so the "
               "interstitial is a wall rather than an overlay." if walls else ""),
            "Delay any subscribe/exit-intent prompt until the visitor has scrolled or "
            "spent meaningful time on the page, suppress it on first arrival from "
            "search or an assistant, and never let it cover the primary content on "
            "mobile.",
            sev, scope="site", urls=[u for u, _ in interstitials],
            evidence_key="E3:interstitial", skill=SKILL,
            falsifier="Cookie/consent banners layered over readable content are not "
                      "counted as walls — only as friction. Only narrowly-scoped "
                      "popup markers are matched, not generic 'modal' class names."))
    if age_gates:
        F.append(Finding(
            "E3", "M1", "Age or eligibility gate blocks first view", "medium",
            f"Age-verification wording found on {len(age_gates)} page(s), e.g. "
            f"{age_gates[0]}. Where legally required this is correct; where it is a "
            f"blanket site-entry gate it prevents both visitors and retrieval agents "
            f"from reaching content.",
            "Apply the gate only on the pages that legally require it, and keep "
            "category and informational pages readable without it.",
            "medium", scope="site", urls=age_gates,
            evidence_key="E3:age-gate", skill=SKILL,
            falsifier="Legally mandated for alcohol, tobacco, gambling and adult "
                      "categories — confirm the regulatory context before acting."))
    if no_viewport:
        F.append(Finding(
            "E3", "M1", "No mobile viewport declared", "medium",
            f"{len(no_viewport)} of {len(html_pages(pages))} pages omit "
            f"<meta name=viewport>, e.g. {no_viewport[0]}. Mobile browsers fall back to "
            f"a desktop-width layout, forcing pinch-and-zoom on the majority of traffic.",
            "Add <meta name=\"viewport\" content=\"width=device-width, "
            "initial-scale=1\"> to the base template.",
            "medium", scope="site", urls=no_viewport,
            evidence_key="E3:no-viewport", skill=SKILL))
    if heavy:
        F.append(Finding(
            "E3", "M1", "Render-blocking scripts or heavy documents delay first paint",
            "low",
            "; ".join(f"{u} ({n} render-blocking scripts, {b // 1024} KB document)"
                      for u, n, b in heavy[:3])
            + ". Every blocking script postpones the moment the visitor sees anything.",
            "Add async/defer to non-critical scripts, move third-party tags behind "
            "consent or interaction, and inline only the CSS needed for the first screen.",
            "low", scope="site", urls=[u for u, _, _ in heavy],
            evidence_key="E3:render-blocking", skill=SKILL,
            falsifier="Measured from markup only, not from real load timings — treat as "
                      "an indicator and confirm with a field performance tool."))


# --------------------------------------------------------------------------
# E4 — Structural noise
# --------------------------------------------------------------------------

def check_noise(site, pages, F):
    link_heavy, chrome_heavy, nav_bloat = [], [], []
    for p in html_pages(pages):
        doc = doc_for(p)
        if page_kind(p["url"], doc) in ("listing", "legal"):
            continue  # guard: listing pages are supposed to be link-dense
        main_wc = len(tokens(doc.main_text))
        all_wc = len(tokens(doc.all_text))
        if main_wc < 150:
            continue
        body_links = [a for a in doc.anchors() if not a.get("in_chrome")]
        link_words = sum(len(tokens(a["text"])) for a in body_links)
        if main_wc and link_words / main_wc > 0.5 and len(body_links) > 25:
            link_heavy.append((p["url"], len(body_links), int(100 * link_words / main_wc)))
        if all_wc and main_wc / all_wc < 0.35 and all_wc > 250:
            chrome_heavy.append((p["url"], int(100 * main_wc / all_wc)))
        chrome_links = [a for a in doc.anchors() if a.get("in_chrome")]
        if len(chrome_links) > 60:
            nav_bloat.append((p["url"], len(chrome_links)))

    if link_heavy:
        F.append(Finding(
            "E4", "M3", "Body content is mostly links rather than prose", "medium",
            "; ".join(f"{u} ({n} in-body links, {pct}% of body words are link text)"
                      for u, n, pct in link_heavy[:3])
            + ". Pages that are mostly navigation give the visitor another decision "
              "instead of an answer, and give extraction systems little connected prose "
              "to quote.",
            "Reduce in-body links to the ones that genuinely continue the reader's task, "
            "and make sure each page answers its own question before offering onward "
            "routes.",
            "medium", scope="template", urls=[u for u, _, _ in link_heavy],
            evidence_key="E4:link-density", skill=SKILL,
            falsifier="Category, blog-index, shop and sitemap templates are excluded — "
                      "being link-dense is their function."))
    if chrome_heavy:
        F.append(Finding(
            "E4", "M3", "Page chrome outweighs the actual content", "medium",
            "; ".join(f"{u} (only {pct}% of page text is main content)"
                      for u, pct in chrome_heavy[:3])
            + ". Navigation, footers and promotional furniture dominate, so the visitor "
              "has to hunt for the part that answers their question.",
            "Trim global navigation and footer link inventories, and give primary "
            "content clear visual and structural dominance inside a <main> landmark.",
            "medium", scope="site", urls=[u for u, _ in chrome_heavy],
            evidence_key="E4:chrome-ratio", skill=SKILL))
    if nav_bloat:
        F.append(Finding(
            "E4", "M3", "Navigation offers too many choices", "low",
            "; ".join(f"{u} ({n} links in header/footer/nav)" for u, n in nav_bloat[:3])
            + ". Very large link inventories increase the cost of every decision and "
              "dilute which pages the site is signalling as important.",
            "Group navigation into a small number of top-level intents and demote the "
            "long tail into section pages.",
            "low", scope="site", urls=[u for u, _ in nav_bloat],
            evidence_key="E4:nav-bloat", skill=SKILL,
            falsifier="Large retailers and publishers legitimately carry deep "
                      "navigation; reported at low severity for that reason."))


# --------------------------------------------------------------------------
# E5 — Next step
# --------------------------------------------------------------------------

def check_next_step(site, pages, F):
    dead_ends, no_cta = [], []
    hp = html_pages(pages)
    for p in hp:
        doc = doc_for(p)
        kind = page_kind(p["url"], doc)
        if kind == "legal":
            continue
        wc = len(tokens(doc.main_text))
        if wc < 100:
            continue
        surface = " ".join([a["text"] for a in doc.anchors()]
                           + [b for b in doc.main_blocks])
        body_links = [a for a in doc.anchors(internal_only=True)
                      if not a.get("in_chrome")]
        if not CTA_RE.search(surface) and not CONTACT_RE.search(surface):
            no_cta.append((p["url"], wc))
        if not body_links and kind != "homepage":
            dead_ends.append((p["url"], wc))

    if no_cta and len(no_cta) >= max(2, 0.4 * len(hp)):
        F.append(Finding(
            "E5", "M2", "Content pages offer no next step", "high",
            f"{len(no_cta)} of {len(hp)} substantive pages contain no call to action and "
            f"no contact route in body text or link text: "
            + "; ".join(f"{u} ({wc} words)" for u, wc in no_cta[:3])
            + ". A visitor who is convinced has nowhere to go, so intent decays at "
              "exactly the moment it peaks.",
            "Close every substantive page with one specific next step matched to that "
            "page's intent — book a demo, see pricing, contact the team — rather than "
            "relying on the global navigation to carry conversion.",
            "high", scope="site", urls=[u for u, _ in no_cta],
            evidence_key="E5:no-cta", skill=SKILL,
            falsifier="Legal and policy pages are excluded, as are pages under 100 "
                      "words. Fires only when it affects 40%+ of substantive pages, so "
                      "one deliberately quiet page does not trigger it."))
    if dead_ends:
        F.append(Finding(
            "E5", "M2", "Dead-end pages with no onward path in the content", "medium",
            "; ".join(f"{u} ({wc} words, zero in-body internal links)"
                      for u, wc in dead_ends[:4])
            + ". The only way onward is the global navigation, which means re-orienting "
              "from scratch rather than continuing a task.",
            "Add contextual links from each page to the two or three pages a reader of "
            "that page most plausibly wants next.",
            "medium", scope="template", urls=[u for u, _ in dead_ends],
            evidence_key="E5:dead-end", skill=SKILL))


# --------------------------------------------------------------------------
# E6 — Labels a scanner can read
# --------------------------------------------------------------------------

def check_labels(site, pages, F):
    generic_links, missing_desc, dup_titles = [], [], {}
    long_titles = []
    hp = html_pages(pages)
    for p in hp:
        doc = doc_for(p)
        for a in doc.anchors():
            t = norm_text(a["text"]).lower().strip(" .!→>»")
            if t in GENERIC_LINK_TEXT and not a.get("aria_label"):
                generic_links.append((p["url"], t))
        if not doc.meta_value(name="description"):
            missing_desc.append(p["url"])
        title = norm_text(doc.title)
        if title:
            dup_titles.setdefault(title, []).append(p["url"])
            if len(title) > 70:
                long_titles.append((p["url"], len(title)))

    dups = {t: us for t, us in dup_titles.items() if len(us) > 2}
    if dups:
        t, us = next(iter(dups.items()))
        F.append(Finding(
            "E6", "M3", "Multiple pages share an identical title", "medium",
            f"{sum(len(u) for u in dups.values())} pages across {len(dups)} title "
            f"group(s) reuse the same <title>, e.g. \"{t[:60]}\" on {len(us)} pages "
            f"({', '.join(us[:3])}). Titles are the label a person sees in search "
            f"results, tabs and shared links; identical titles make pages "
            f"indistinguishable.",
            "Generate titles from page content: '<Page subject> — <Brand>'. Reserve the "
            "brand-only title for the homepage.",
            "medium", scope="site", urls=[u for us in dups.values() for u in us],
            evidence_key="E6:duplicate-titles", skill=SKILL,
            falsifier="Only fires when 3+ pages share a title, so paginated variants of "
                      "one page do not trigger it."))
    if generic_links and len(generic_links) >= 5:
        F.append(Finding(
            "E6", "M3", "Uninformative link text", "low",
            f"{len(generic_links)} links use generic text with no aria-label, e.g. "
            + "; ".join(f"'{t}' on {u}" for u, t in generic_links[:4])
            + ". Link text is read out of context by scanners, screen readers and "
              "extraction systems, so 'read more' conveys nothing about the destination.",
            "Rewrite link text to name the destination ('read the 2026 pricing "
            "breakdown'), or attach an aria-label where visual design requires short "
            "text.",
            "low", scope="site", urls=[u for u, _ in generic_links],
            evidence_key="E6:generic-links", skill=SKILL,
            falsifier="Links carrying an aria-label are excluded; the threshold of 5 "
                      "avoids flagging a single stray 'learn more'."))
    if missing_desc and len(missing_desc) >= max(2, 0.5 * len(hp)):
        F.append(Finding(
            "E6", "M3", "Pages have no meta description", "low",
            f"{len(missing_desc)} of {len(hp)} pages omit a meta description, e.g. "
            f"{missing_desc[0]}. This is the one-sentence summary shown in search "
            f"results and link previews — without it, an automatically-chosen fragment "
            f"is shown instead.",
            "Write a specific one-sentence description per page stating what the page "
            "answers. Do not template it from the brand tagline.",
            "low", scope="site", urls=missing_desc,
            evidence_key="E6:no-meta-description", skill=SKILL))
    if long_titles and len(long_titles) >= 3:
        F.append(Finding(
            "E6", "M3", "Titles are long enough to be truncated in previews", "low",
            "; ".join(f"{u} ({n} chars)" for u, n in long_titles[:3])
            + ". The distinguishing part of a long title is usually the part that gets "
              "cut.",
            "Front-load the distinguishing words and keep titles near 60 characters.",
            "low", scope="site", urls=[u for u, _ in long_titles],
            evidence_key="E6:long-titles", skill=SKILL))


def main():
    ap = argparse.ArgumentParser(description="On-site engagement / friction audit")
    ap.add_argument("--url", required=True)
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--out", default=None)
    ap.add_argument("--pages-in", default=None)
    ap.add_argument("--pages-cache", default=None)
    args = ap.parse_args()

    site = args.url if "://" in args.url else "https://" + args.url
    F = []

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

    check_orientation(site, pages, F)
    check_structure(site, pages, F)
    check_entry_friction(site, pages, F)
    check_noise(site, pages, F)
    check_next_step(site, pages, F)
    check_labels(site, pages, F)

    meta = {
        "skill": SKILL, "site": registrable_domain(site), "start_url": site,
        "audited_at": utc_now(), "pages_analysed": len(html_pages(pages)),
        "mechanisms": ["M1", "M2", "M3"],
        "note": "Engagement signals are human-parser instantiations of M1-M3; "
                "overlaps with crawl-render-audit are declared via supersedes_hint "
                "and resolved by the orchestrator.",
    }
    payload = emit(F, meta, args.out)
    if not args.out:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"[{SKILL}] {len(F)} finding(s) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
