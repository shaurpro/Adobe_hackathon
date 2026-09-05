---
name: audit-orchestrator
description: Entrypoint for the brand-ai-readiness-audit marketplace. Given a website URL, runs the crawl-render, freshness-corroboration and engagement audits against one shared page corpus, merges their findings, collapses duplicates and symptoms into root causes, recomputes severity under a single model, prioritizes by failure stage and blast radius, appends beyond-defect recommendations, and emits one schema-conformant JSON audit report of evidence-backed findings with prioritized suggested actions. Use whenever someone asks why a brand is missing, misquoted or ignored by AI assistants, why visitors who arrive don't stay, or asks for a website AI-readiness or discoverability audit. Recommend-only — never modifies the audited site.
license: MIT
allowed-tools:
  - http-fetch
  - web-search
  - file-read
  - file-write
  - shell
  - headless-browser
---

# Audit Orchestrator (entrypoint)

## When to use

This is the skill invoked for an audit request. It owns the whole run: it decides what
the sibling skills are given, what their output means together, and what the final report
says. The sibling skills observe; this skill judges.

## Inputs

- `url` (required) — domain or full URL of the site to audit.
- `max_pages` (default 20) — page sample ceiling.
- `render` (`auto` | `off`) — allow a headless render comparison.
- `no_external` — suppress outbound `sameAs` verification.
- `budget_seconds` (default 270) — hard ceiling, keeping the run under the 5-minute limit.

## Safety

Recommend-only. Every skill it invokes is read-only, obeys robots.txt, avoids
authenticated and destructive actions, and rate-limits itself. This skill writes exactly
one file — the report — and never touches the audited site.

## Procedure

1. **Run `crawl-render-audit` first, with `--pages-cache`.** It publishes the shared page
   corpus. This ordering is not arbitrary: it front-loads the only skill that must fetch,
   so every page is fetched **once** for the whole marketplace. The siblings then read the
   corpus from disk and make zero requests of their own.
2. **Run `freshness-corroboration`** with `--pages-in`. Collect its `manual_probes`.
3. **Run `engagement-audit`** with `--pages-in`.
4. **Merge, dedupe, re-score, prioritize** (rules below).
5. **Execute pending probes** (see *Probes*), if search tooling is available.
6. **Append beyond-defect recommendations** that do not restate a detected defect.
7. **Emit and validate** the report.

```bash
python3 scripts/orchestrate.py --url <URL> --out report.json \
    [--max-pages 20] [--render auto|off] [--no-external] [--budget-seconds 270]

python3 scripts/validate_report.py report.json     # always run this
```

Skill paths are resolved from `marketplace.json`, not hardcoded — the manifest is the
single source of truth for what this marketplace contains.

**Degrade, don't abort.** If a sibling skill fails or times out, the run continues and the
failure is recorded in `audit_scope.skills_failed`. A partial report with an explicit,
named gap is more useful than no report; silently dropping a skill's coverage is not.

## Merge rules

Applied in order. Full worked examples: `references/dedupe-rules.md`.

1. **Confidence ceiling** — a finding may never outrank the quality of its evidence.
   `observed` → up to `critical`; `inferred` → capped at `high`; `unverified` → capped at
   `low`; anything `non_deterministic` → capped at `low`. Capping records
   `severity_before_cap` and a `cap_reason`, so the demotion is visible rather than silent.
2. **Identical `evidence_key`** — the same defect seen by two skills. Merge into one:
   keep the highest severity, union the affected URLs, and append the second skill's
   evidence rather than discarding it.
3. **`supersedes_hint`** — cross-skill root-cause collapse. When a skill reports a symptom
   whose cause another skill has proven, the symptom folds into the cause as a
   `related_effect` and stops being counted. The canonical case: a client-rendered page is
   both an extraction failure (`M3-S1`) and an orientation failure (`E1`). One root cause,
   one fix, one finding.
4. **Template rollup** — three or more page-scoped findings of the same signal become one
   template-scoped finding with the count in evidence. A report with forty near-identical
   entries is unreadable, and unreadable reports do not get acted on.
5. **Prioritize** by: severity → **failure stage** (M1 → M6) → blast radius (site →
   template → page) → confidence. Stage beats volume: one robots.txt block outranks a
   dozen schema gaps, because fixing the schema on a site no crawler may enter changes
   nothing.
6. **Assign ids** `F-001…` in final priority order, so id order *is* fix order.

## Probes

`freshness-corroboration` returns `manual_probes` — checks that depend on live search and
are therefore personalized and non-reproducible (mechanism 6). Handle them like this:

- Run each probe's queries **twice**.
- Stable across both runs → emit at up to the probe's `severity_ceiling` (never above
  `medium`).
- Unstable → emit at `low` with `non_deterministic: true`, recording both result sets and
  timestamps in the evidence.
- Never phrase a probe finding as a claim about one assistant's behaviour. *"No
  independent domain restates the identity claim"* is a property of the web and is
  checkable. *"ChatGPT ignores this brand"* is neither.
- If no search tooling is available, leave the probes in `pending_probes` and say so.
  An unrun check is a stated gap, not a passed check.

## Beyond-defect recommendations

Kept in `proactive_recommendations[]`, **separate from `findings[]`**, and ordered by
their own action priority. A recommendation is not a detected problem — counting it as one
inflates the summary and makes a clean site look broken. Each is suppressed when it would
merely restate a defect that was already found.

Current set: publish an FAQ built from real support questions; publish a canonical facts
page; earn independent corroboration through obtainable surfaces (registry, trade
directories, Wikidata where notability holds, customer-hosted case studies); add visible
last-reviewed dates; declare `alternateName` and a stable `@id`.

## Reporting rules

- **Evidence must be checkable.** Every finding states what was measured: the status code,
  the matched robots rule, the word count, the parse error and its line. Absence findings
  must say how much was searched — "no `areaServed` across 5 sampled pages", not "no
  `areaServed`". The validator warns when a finding's evidence contains no measured value.
- **One root cause, one finding.** If two symptoms share a fix, they are one finding with
  the other listed as a related effect.
- **Write for a non-expert.** The action says what to change and where. "Add
  `Product`/`Offer` JSON-LD to the product template, generated from the same data that
  renders the price" — not "improve structured data".
- **Name the falsifier when a finding could be intentional.** Blocked AI crawlers,
  withheld pricing, no social presence and age gates all have legitimate versions. Report
  the consequence precisely and let the owner decide.

## Output

The required schema (floor), plus extensions:

```json
{
  "site": "example.com",
  "audited_at": "2026-08-31T12:00:00Z",
  "summary": { "total_findings": 17, "proactive_recommendations": 5,
               "critical": 1, "high": 6, "medium": 5, "low": 5 },
  "findings": [
    { "id": "F-001", "title": "...", "severity": "critical",
      "evidence": "...", "suggested_action": { "summary": "...", "priority": "critical" },
      "mechanism": "M1", "signal": "M1-S1", "skill": "crawl-render-audit",
      "scope": "site", "confidence": "observed", "affected_urls": ["..."],
      "evidence_key": "M1-S1:root-block", "falsified_by": "...",
      "related_effects": [], "non_deterministic": false }
  ],
  "proactive_recommendations": [ { "id": "R-001", "title": "...", "suggested_action": {...} } ],
  "audit_scope": { "pages_fetched": 12, "entity_name": "...", "runtime_seconds": 42.1,
                   "skills_run": [...], "skills_failed": {}, "mode": "recommend-only" },
  "pending_probes": [ ... ]
}
```

`audit_scope` is not decoration. A reader needs to know what was *not* checked — how many
pages were sampled, whether the render comparison ran, which skills failed, which probes
are outstanding — before trusting an absence finding.

Always run `validate_report.py` before returning a report: it enforces the required
fields, the severity enum, unique ids, and consistency between the summary counts and the
findings array.
