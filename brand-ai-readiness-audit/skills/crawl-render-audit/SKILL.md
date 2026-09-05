---
name: crawl-render-audit
description: Audit whether a machine can reach, parse and extract facts from a website — crawler admission and robots.txt health, noindex directives, redirect and canonical integrity, sitemap liveness, refusal of plain HTTP clients (403/429, bot challenges, consent walls), client-side render gaps where content never reaches the server response, facts locked in images/PDFs/video, and structured data that is missing, unparseable, placeholder-filled, or contradicting the visible page. Use when diagnosing why a brand is absent from or misquoted by AI assistants, why pages look fine to humans but return nothing useful to a fetcher, or as the first stage of a wider AI-readiness audit. Recommend-only — never modifies the audited site.
license: MIT
allowed-tools:
  - http-fetch
  - file-read
  - file-write
  - shell
  - headless-browser
---

# Crawl / Render / Extract Audit

## When to use

Run this first in any AI-readiness audit. It tests the three-stage gate every retrieval
system passes through — **crawl → read → extract** — and a failure here makes every
downstream improvement worthless. There is no point corroborating a fact across the web
if a crawler is never let in to read it.

Covers mechanisms **M1** (the three-stage gate), **M2** (fetchability of a quotable
source) and **M3** (machine readability ≠ human readability). Corroboration, entity
disambiguation and human-side friction belong to sibling skills.

## Inputs

- `url` (required) — a domain or full URL. Scheme is optional; `https://` is assumed.
- `max_pages` (default 20) — page sample ceiling. Keeps the run inside the 5-minute budget.
- `render` (`auto` | `off`, default `auto`) — whether to attempt a headless comparison.
- `pages_in` / `pages_cache` — reuse or publish a shared page corpus so the marketplace
  fetches each page at most once.

## Safety

Read-only GETs only. Obeys robots.txt for its own fetching, even while auditing whether
*other* agents are admitted. Never authenticates, never POSTs, never follows destructive
links, rate-limits itself with a delay between fetches, and identifies honestly in the
User-Agent. Never modifies the audited site.

## Procedure

1. **Normalize** the input to an absolute origin. Derive the registrable domain for
   scoping (IP literals and single-label hosts are handled as-is).
2. **Fetch robots.txt** and parse it into per-user-agent groups. Keep the groups intact:
   reporting "GPTBot is blocked but Googlebot is not" requires knowing which group a rule
   came from, which a collapsed parser cannot tell you.
3. **Discover the sitemap** via the `Sitemap:` directive, falling back to conventional
   paths. Follow one level of sitemap index.
4. **Crawl** breadth-first within the registrable domain, seeded by the sitemap, up to
   `max_pages`, skipping any URL this skill's own user agent is disallowed from. Cache
   the corpus for sibling skills.
5. **Run the seven check families** below. Each one must satisfy its falsifier before it
   is allowed to emit.
6. **Emit** the intermediate envelope: `{ "meta": {...}, "findings": [...] }`. Findings
   are data, not failures — the exit code reflects whether the audit ran, not what it found.

Run the bundled script for the deterministic work:

```bash
python3 scripts/crawl_render_audit.py --url <URL> \
    [--max-pages 20] [--render auto|off] [--out findings.json] \
    [--pages-cache pages.json] [--pages-in pages.json]
```

It has **no third-party dependencies** (Python 3.8+, standard library only). If
Playwright is installed the render check uses it; if not, the check falls back to
raw-HTML SPA-shell detection and records *why* the browser comparison is missing rather
than inferring a defect from a missing tool.

## Checks

Full signal definitions, evidence formats and falsifiers are in
`references/checks.md`. Read that file when you need to justify or tune a finding. The
summary:

| Signal | Mechanism | Tests |
|---|---|---|
| M1-S1 | M1 | Crawler admission: AI/search agents blocked at root, robots.txt erroring, public content paths disallowed, noindex on content pages |
| M1-S2 | M1 | Response integrity: redirect chains ≥3, 4xx/5xx on linked URLs, soft 404s, canonicals pointing away from the page |
| M1-S3 | M1 | Discovery surface: sitemap presence, liveness of its entries, lastmod, agreement with internal linking |
| M2-S1 | M2 | Fetch gating: 403/401/429 to honest clients, bot challenges served as content, consent walls that withhold the document |
| M3-S1 | M3 | Client-side render gap: body text, H1 and nav absent from the server response |
| M3-S2 | M3 | Facts in non-text carriers: missing/meaningless alt on content images, PDF-only pricing or specs, video with no transcript |
| M3-S3 | M3 | Structured data presence **and** validity **and** agreement with the visible page |

## Reasoning rules

These are the judgements the script cannot make alone. Apply them when reviewing output.

- **Stage order beats symptom count.** One robots.txt block outranks a dozen schema
  gaps. Report in stage order; never let a long tail of extract-stage findings bury a
  crawl-stage one.
- **A contradiction is worse than an absence.** JSON-LD that disagrees with the visible
  page (`M3-S3`) is more damaging than no JSON-LD at all, because the safest machine
  response to a conflict is to trust neither source. Severity reflects that.
- **Absence of a tool is not evidence of a defect.** If the headless browser is
  unavailable, say so in the evidence. Never convert "I couldn't check" into "it's broken."
- **Blocking AI agents may be deliberate.** If robots.txt denies retrieval agents,
  report the consequence precisely and let the owner decide. Frame the action as a policy
  choice — allow retrieval agents on public pages, keep bulk-training crawlers blocked if
  that is the preference — not as an unconditional "unblock everything."
- **Quote the observation, not the conclusion.** Evidence strings must contain the
  fetched fact: the status code, the rule that matched, the word count, the parse error
  with its line number. A finding a reader cannot verify is not actionable.

## Output

An envelope with `meta` (skill, site, `audited_at`, pages fetched, robots status,
mechanisms covered) and `findings`. Each finding carries the required report fields
(`title`, `severity`, `evidence`, `suggested_action`) plus the extension fields the
orchestrator needs to merge cleanly: `signal`, `mechanism`, `scope`
(`site`/`template`/`page`), `confidence` (`observed`/`inferred`/`unverified`),
`evidence_key`, `affected_urls`, and `falsified_by`.

The orchestrator assigns final `id` values, so `id` is left null here — ids must be
unique across the merged report, which only the entrypoint can guarantee.
