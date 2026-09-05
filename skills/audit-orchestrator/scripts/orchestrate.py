#!/usr/bin/env python3
"""
orchestrate.py — entrypoint composer for the brand-ai-readiness-audit marketplace.

Invokes the three analysis skills against one shared page corpus, merges their
envelopes, dedupes by root cause, recomputes severity under the marketplace's
severity model, prioritises, appends beyond-defect recommendations, and emits a
single schema-conformant audit report.

Skill paths are resolved from marketplace.json, not hardcoded: the manifest is
the single source of truth for what this marketplace contains.

Read-only end to end. Never modifies the audited site.

Usage:
  python3 orchestrate.py --url https://example.com [--out report.json]
      [--max-pages 20] [--render auto|off] [--no-external] [--timeout 12]
      [--budget-seconds 270]
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from auditlib import SEVERITY_RANK, registrable_domain, utc_now  # noqa: E402

# Stage ordering: a failure earlier in the pipeline makes later fixes worthless,
# so it sorts first regardless of how many later findings exist.
MECHANISM_STAGE = {"M1": 0, "M2": 1, "M3": 2, "M4": 3, "M5": 4, "M6": 5}
SCOPE_WEIGHT = {"site": 0, "template": 1, "page": 2}
CONFIDENCE_WEIGHT = {"observed": 0, "inferred": 1, "unverified": 2}

# Confidence ceilings. A finding can never outrank the quality of its evidence.
CONFIDENCE_CEILING = {"observed": "critical", "inferred": "high", "unverified": "low"}

REQUIRED_FINDING_FIELDS = ("id", "title", "severity", "evidence", "suggested_action")


# --------------------------------------------------------------------------
# Beyond-defect recommendations
# --------------------------------------------------------------------------
# Emitted only when the corresponding defect was NOT found — these are
# improvements for sites that already pass, not restatements of failures.
# Each names the mechanism it strengthens so it is auditable like any finding.

PROACTIVE = [
    {
        "key": "P-FAQ",
        "skip_if_any": ["M2-S3:no-answer-structure", "E2:no-subheadings"],
        "title": "Publish an FAQ block built from questions support actually receives",
        "mechanism": "M2",
        "evidence": "No FAQ or question-shaped heading block was detected in the "
                    "sampled pages. Assistants assemble answers from sources where a "
                    "question and its answer sit adjacent in plain text; an FAQ is the "
                    "cheapest structure that guarantees that adjacency.",
        "action": "Collect the questions sales and support are actually asked, publish "
                  "them verbatim as headings with a direct one-sentence answer beneath "
                  "each, and mark the block up as FAQPage JSON-LD.",
        "priority": "medium",
    },
    {
        "key": "P-FACTS",
        "skip_if_any": [],
        "title": "Publish a canonical facts page for the entity",
        "mechanism": "M4",
        "evidence": "Core facts (founding year, size, locations, leadership, key "
                    "figures) are typically spread across prose on several pages. A "
                    "single page that states them plainly gives every downstream "
                    "system one URL to reach, read and quote — and gives the brand one "
                    "place to keep current.",
        "action": "Create a /about/facts (or press kit) page listing the entity's core "
                  "facts as short labelled statements and a table, in plain HTML text, "
                  "with a visible last-reviewed date. Link it from the footer and "
                  "reference it from the Organization JSON-LD.",
        "priority": "medium",
    },
    {
        "key": "P-CORROB",
        "skip_if_any": ["M4-S1:no-corroboration"],
        "title": "Earn independent restatement of the core identity claim",
        "mechanism": "M4",
        "evidence": "On-page work cannot create cross-source agreement. Facts repeated "
                    "consistently across unrelated domains are treated as more reliable "
                    "than facts that exist in one place only.",
        "action": "Target obtainable, independent surfaces rather than press coverage: "
                  "the national company register, two or three industry association or "
                  "trade directories, a Wikidata item if notability is genuinely met, "
                  "conference and partner listings, and customer case studies published "
                  "on the customer's own domain. Use identical name, category and "
                  "location wording on every one.",
        "priority": "high",
    },
    {
        "key": "P-REVIEWED",
        "skip_if_any": ["M4-S3:staleness"],
        "title": "Add a visible last-reviewed date to fact-bearing pages",
        "mechanism": "M4",
        "evidence": "No dated freshness signal was found on evergreen pages. Undated "
                    "evergreen content cannot be assessed for currency, so a retrieval "
                    "system has no way to prefer it over an older competing source.",
        "action": "Show 'Last reviewed: <month year>' on pricing, product, policy and "
                  "about pages, back it with dateModified in structured data, and put a "
                  "recurring review date in the content calendar so the label stays true.",
        "priority": "low",
    },
    {
        "key": "P-ALTNAME",
        "skip_if_any": ["M5-S3:name-variants", "M5-S1:ambiguous-name"],
        "title": "Declare name variants and a stable entity identifier",
        "mechanism": "M5",
        "evidence": "Even where naming is currently consistent, declaring the variants "
                    "explicitly makes the entity resolvable as the brand grows and as "
                    "similarly-named entities appear.",
        "action": "Add alternateName for every legitimate variant (trading name, legal "
                  "name, common abbreviation) and give the Organization a stable @id "
                  "referenced from every other node.",
        "priority": "low",
    },
]


# --------------------------------------------------------------------------
# Skill invocation
# --------------------------------------------------------------------------

def load_manifest(root):
    with open(os.path.join(root, "marketplace.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def marketplace_root():
    # scripts/ -> audit-orchestrator/ -> skills/ -> <root>
    return os.path.abspath(os.path.join(HERE, "..", "..", ".."))


def skill_script(root, manifest, skill_id, filename):
    for s in manifest.get("skills", []):
        if s["id"] == skill_id:
            return os.path.join(root, s["path"], "scripts", filename)
    raise KeyError(f"skill '{skill_id}' is not listed in marketplace.json")


def run_skill(path, args, budget):
    """Run a sibling skill's script. A skill that fails degrades the audit; it
    does not abort it — a partial report with an explicit gap is more useful
    than no report."""
    if not os.path.exists(path):
        return None, f"script not found: {path}"
    cmd = [sys.executable, path] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=budget)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {budget}s"
    if proc.returncode != 0:
        return None, f"exit {proc.returncode}: {proc.stderr.strip()[:300]}"
    return True, None


# --------------------------------------------------------------------------
# Merge / dedupe / prioritise
# --------------------------------------------------------------------------

def cap_by_confidence(f):
    ceiling = CONFIDENCE_CEILING.get(f.get("confidence", "observed"), "critical")
    if f.get("non_deterministic"):
        ceiling = "low"
    if SEVERITY_RANK[f["severity"]] > SEVERITY_RANK[ceiling]:
        f["severity_before_cap"] = f["severity"]
        f["severity"] = ceiling
        f["cap_reason"] = (
            f"severity capped at '{ceiling}' because evidence confidence is "
            f"'{f.get('confidence', 'observed')}'"
            + (" and the result was unstable across repeat runs"
               if f.get("non_deterministic") else ""))
    return f


def merge_identical(findings):
    """Rule 1 — identical evidence_key means the same defect seen twice."""
    merged = {}
    for f in findings:
        k = f.get("evidence_key") or f"{f.get('signal')}:{f.get('scope')}"
        if k not in merged:
            merged[k] = dict(f)
            continue
        m = merged[k]
        if SEVERITY_RANK[f["severity"]] > SEVERITY_RANK[m["severity"]]:
            m["severity"] = f["severity"]
            m["suggested_action"]["priority"] = f["suggested_action"].get(
                "priority", f["severity"])
        urls = list(dict.fromkeys(m.get("affected_urls", []) + f.get("affected_urls", [])))
        m["affected_urls"] = urls[:10]
        m["affected_count"] = max(m.get("affected_count", 0), f.get("affected_count", 0))
        m.setdefault("merged_from", [m.get("skill")])
        if f.get("skill") not in m["merged_from"]:
            m["merged_from"].append(f.get("skill"))
        if f["evidence"] not in m["evidence"]:
            m["evidence"] += f" Also observed by {f.get('skill')}: {f['evidence']}"
    return list(merged.values())


def apply_supersedes(findings):
    """
    Rule 2 — cross-skill root-cause collapse.

    A skill that detects a symptom whose cause another skill has already proven
    declares `supersedes_hint`. If the cause is present in the merged set, the
    symptom is folded into it as a related effect rather than counted twice.
    """
    by_key = {f.get("evidence_key"): f for f in findings}
    kept, folded = [], 0
    for f in findings:
        hint = f.get("supersedes_hint")
        if hint and hint in by_key and by_key[hint] is not f:
            cause = by_key[hint]
            cause.setdefault("related_effects", []).append({
                "title": f["title"],
                "skill": f.get("skill"),
                "evidence_key": f.get("evidence_key"),
                "note": "Same root cause; folded into this finding to avoid "
                        "double-counting. Fixing the cause resolves it.",
            })
            folded += 1
            continue
        kept.append(f)
    return kept, folded


def rollup_templates(findings, threshold=3):
    """
    Rule 3 — page-level findings of the same signal across many pages become one
    template-level finding. Safety net: the analysis skills already aggregate,
    but a future skill emitting per-page findings must not flood the report.
    """
    buckets, out = {}, []
    for f in findings:
        if f.get("scope") != "page":
            out.append(f)
            continue
        buckets.setdefault(f.get("signal"), []).append(f)
    for signal, group in buckets.items():
        if len(group) < threshold:
            out.extend(group)
            continue
        lead = max(group, key=lambda x: SEVERITY_RANK[x["severity"]])
        lead = dict(lead)
        urls = []
        for g in group:
            urls.extend(g.get("affected_urls", []))
        lead["scope"] = "template"
        lead["affected_urls"] = list(dict.fromkeys(urls))[:10]
        lead["affected_count"] = len(set(urls))
        lead["evidence"] = (f"Pattern across {len(group)} pages. " + lead["evidence"])
        lead["rolled_up_from"] = len(group)
        out.append(lead)
    return out


def priority_key(f):
    return (
        -SEVERITY_RANK[f["severity"]],
        MECHANISM_STAGE.get(f.get("mechanism", "M6"), 9),
        SCOPE_WEIGHT.get(f.get("scope", "page"), 3),
        CONFIDENCE_WEIGHT.get(f.get("confidence", "observed"), 3),
        f.get("title", ""),
    )


PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def add_proactive(findings, site):
    """
    Beyond-defect recommendations, kept OUT of findings[].

    A recommendation is not a detected problem, and counting it as one inflates
    the summary and makes a clean site look defective. They are also ordered by
    their own action priority, so a high-priority improvement is not buried
    beneath low-severity defects.
    """
    present = {f.get("evidence_key") for f in findings}
    out = []
    for rule in PROACTIVE:
        if any(k in present for k in rule["skip_if_any"]):
            continue
        out.append({
            "id": None,
            "title": rule["title"],
            "evidence": rule["evidence"],
            "suggested_action": {"summary": rule["action"],
                                 "priority": rule["priority"]},
            "mechanism": rule["mechanism"],
            "signal": rule["key"],
            "skill": "audit-orchestrator",
            "scope": "site",
            "confidence": "observed",
            "affected_urls": [site],
            "affected_count": 1,
            "evidence_key": f"proactive:{rule['key']}",
            "finding_type": "proactive",
        })
    out.sort(key=lambda r: PRIORITY_RANK.get(r["suggested_action"]["priority"], 4))
    for i, r in enumerate(out, start=1):
        r["id"] = f"R-{i:03d}"
    return out


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Brand AI-readiness audit orchestrator")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", default="report.json")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=12)
    ap.add_argument("--delay", type=float, default=0.4)
    ap.add_argument("--render", choices=["auto", "off"], default="auto")
    ap.add_argument("--no-external", action="store_true")
    ap.add_argument("--budget-seconds", type=int, default=270)
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()

    started = time.time()
    site_url = args.url if "://" in args.url else "https://" + args.url
    root = marketplace_root()
    manifest = load_manifest(root)

    work = args.workdir or os.path.join(
        os.path.sep + "tmp", f"audit-{int(time.time())}")
    os.makedirs(work, exist_ok=True)
    pages = os.path.join(work, "pages.json")
    outs = {k: os.path.join(work, f"{k}.json")
            for k in ("crawl-render-audit", "freshness-corroboration", "engagement-audit")}

    skill_errors = {}

    # 1. crawl-render-audit publishes the shared corpus. Every page is fetched once.
    ok, err = run_skill(
        skill_script(root, manifest, "crawl-render-audit", "crawl_render_audit.py"),
        ["--url", site_url, "--max-pages", str(args.max_pages),
         "--timeout", str(args.timeout), "--delay", str(args.delay),
         "--render", args.render, "--pages-cache", pages,
         "--out", outs["crawl-render-audit"]],
        budget=max(30, args.budget_seconds - int(time.time() - started)))
    if err:
        skill_errors["crawl-render-audit"] = err

    # 2 + 3. Siblings consume the corpus; neither re-crawls.
    fc_args = ["--url", site_url, "--out", outs["freshness-corroboration"],
               "--timeout", str(args.timeout)]
    if os.path.exists(pages):
        fc_args += ["--pages-in", pages]
    if args.no_external:
        fc_args.append("--no-external")
    ok, err = run_skill(
        skill_script(root, manifest, "freshness-corroboration", "corroboration_probe.py"),
        fc_args, budget=max(20, args.budget_seconds - int(time.time() - started)))
    if err:
        skill_errors["freshness-corroboration"] = err

    en_args = ["--url", site_url, "--out", outs["engagement-audit"]]
    if os.path.exists(pages):
        en_args += ["--pages-in", pages]
    ok, err = run_skill(
        skill_script(root, manifest, "engagement-audit", "engagement_audit.py"),
        en_args, budget=max(20, args.budget_seconds - int(time.time() - started)))
    if err:
        skill_errors["engagement-audit"] = err

    # ---- merge -----------------------------------------------------------
    raw, metas, probes = [], {}, []
    for skill_id, path in outs.items():
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            env = json.load(fh)
        metas[skill_id] = env.get("meta", {})
        probes.extend(env.get("meta", {}).get("manual_probes", []))
        for f in env.get("findings", []):
            f.setdefault("skill", skill_id)
            raw.append(f)

    raw = [cap_by_confidence(f) for f in raw]
    findings = merge_identical(raw)
    findings, folded = apply_supersedes(findings)
    findings = rollup_templates(findings)
    proactive = add_proactive(findings, site_url)
    findings.sort(key=priority_key)

    for i, f in enumerate(findings, start=1):
        f["id"] = f"F-{i:03d}"
        f["suggested_action"].setdefault("priority", f["severity"])

    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1

    site_meta = metas.get("crawl-render-audit", {})
    report = {
        "site": registrable_domain(site_url) or urllib.parse.urlsplit(site_url).netloc,
        "audited_at": utc_now(),
        "summary": {
            "total_findings": len(findings),
            "proactive_recommendations": len(proactive),
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
            "low": counts["low"],
        },
        "findings": findings,
        "proactive_recommendations": proactive,
        "audit_scope": {
            "start_url": site_url,
            "pages_fetched": site_meta.get("pages_fetched"),
            "html_pages_analysed": site_meta.get("html_pages"),
            "robots_status": site_meta.get("robots_status"),
            "entity_name": metas.get("freshness-corroboration", {}).get("entity_name"),
            "entity_name_source": metas.get("freshness-corroboration", {}).get(
                "entity_name_source"),
            "render_check": args.render,
            "runtime_seconds": round(time.time() - started, 1),
            "skills_run": [k for k in outs if k in metas],
            "skills_failed": skill_errors,
            "findings_folded_into_root_causes": folded,
            "mode": "recommend-only; no site modified",
        },
        "pending_probes": probes,
    }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)

    print(f"[audit-orchestrator] {len(findings)} finding(s) "
          f"({counts['critical']} critical, {counts['high']} high, "
          f"{counts['medium']} medium, {counts['low']} low) "
          f"+ {len(proactive)} recommendation(s) "
          f"in {report['audit_scope']['runtime_seconds']}s -> {args.out}",
          file=sys.stderr)
    if skill_errors:
        print(f"[audit-orchestrator] degraded: {skill_errors}", file=sys.stderr)
    if probes:
        print(f"[audit-orchestrator] {len(probes)} probe(s) require agent execution "
              f"(see pending_probes)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
