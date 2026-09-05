---
name: engagement-audit
description: Audit the on-site friction a visitor hits after they arrive — whether the page states plainly what the organisation is above the fold, whether long pages are broken up by question-shaped headings, whether interstitials, age gates, missing mobile viewports or render-blocking scripts intercept the first view, whether navigation and link density drown the actual content, whether pages offer a next step or dead-end, and whether titles, link text and descriptions label anything a scanner can use. Use when traffic arrives but bounces, when a page reads well to its author but not to a stranger, or as the engagement half of a wider AI-readiness audit. Recommend-only — never modifies the audited site.
license: MIT
allowed-tools:
  - http-fetch
  - file-read
  - file-write
  - shell
---

# Engagement Audit (on-site friction)

## When to use

Run alongside `crawl-render-audit` and `freshness-corroboration`, using the same page
corpus. This skill answers the second half of the brief: *the visitor arrived — why
didn't they stay?*

## The design claim: engagement is the same gate, slower

Engagement is **not** a separate theory requiring its own mechanisms. A human landing on
a page runs the same three-stage gate a crawler does, with less patience and a lower
tolerance for ambiguity:

| Stage | Crawler version | Human version | Signals |
|---|---|---|---|
| **Let in** | robots.txt, 403, bot challenge | interstitial, age gate, mobile-hostile layout, slow first paint | `E3` |
| **Able to read** | JS-render gap, non-text carriers | wall of text, no headings, chrome drowning content | `E2`, `E4`, `E6` |
| **Extract the fact** | isolate a quotable claim | *what is this, is it for me, what now* | `E1`, `E5` |

Two consequences follow, and both are deliberate:

1. Each engagement signal maps back to **M1, M2 or M3** — the same mechanisms, a
   different parser. No mechanism is invented for this skill.
2. Some evidence legitimately supports **two findings at once**. A page that renders its
   text client-side is both an extraction failure (`M3-S1`) and an orientation failure
   (`E1`). This skill reports the visitor's experience and declares the overlap with
   `supersedes_hint`; the orchestrator collapses the pair so the report never
   double-counts one root cause.

## Inputs

- `url` (required) — domain or full URL.
- `pages_in` — the shared page corpus. Preferred: this skill then performs **zero**
  network requests of its own.
- `max_pages` (default 20) — only used when building its own corpus.

## Safety

Read-only. No fetches at all when `--pages-in` is supplied. Never submits a form, never
authenticates, never clicks a CTA it finds, never modifies the audited site.

## Procedure

1. **Load the page corpus** (prefer `--pages-in`).
2. **Classify each page** as `homepage`, `content`, `listing` or `legal`. This runs
   before every other check, because most false positives in engagement auditing come
   from judging a page against the wrong template's expectations — link density is a
   defect on an article and the entire point of a category page.
3. **Run E1-E6.** Each check must clear its guard before emitting.
4. **Emit** the standard envelope.

```bash
python3 scripts/engagement_audit.py --url <URL> \
    [--pages-in pages.json] [--out findings.json]
```

No third-party dependencies (Python 3.8+, standard library only).

## Checks

Full definitions, thresholds and falsifiers: `references/checks.md`.

| Signal | Maps to | Tests |
|---|---|---|
| E1 | M2 | Orientation: a self-contained sentence binding entity + category + differentiator, in the first screen |
| E2 | M2 | Answer-shaped structure: sub-headings on long pages, one H1, no level skips, paragraph density |
| E3 | M1 | Entry friction: interstitials, age gates, missing mobile viewport, render-blocking scripts |
| E4 | M3 | Structural noise: in-body link density, chrome-to-content ratio, navigation inventory |
| E5 | M2 | Next step: a call to action or contact route, and contextual onward links |
| E6 | M3 | Labels: duplicate/overlong titles, generic link text, missing meta descriptions |

## Reasoning rules

- **Classify the template before judging the page.** A category page is link-dense by
  design; a privacy policy has no call to action by design; a long-form essay runs long
  paragraphs by design. Listing and legal templates are excluded from the checks they
  would structurally fail.
- **"Missing" must mean missing everywhere.** Before reporting the positioning statement
  as absent, search body text, meta description, JSON-LD description **and** the about
  page. If it exists anywhere machine-readable, the finding is *buried* (`medium`,
  "hoist it"), not *missing* (`high`, "write it"). These are different problems with
  different fixes.
- **An overlay is not a wall.** A cookie or newsletter banner layered over readable
  content is friction worth reporting at `medium`. The same markup over an empty
  document is a wall at `high`. The distinguishing evidence is the word count of the
  content behind it — measure it, don't assume.
- **Prefer narrow markers over broad ones.** The interstitial check matches specific
  popup patterns, not any element with a `modal` class, because half the CSS frameworks
  in use ship a `modal` class that no page ever displays.
- **Aggregate before escalating.** Single-page quirks are noise. `E5` fires only when a
  next step is absent from 40%+ of substantive pages; `E6` link text needs five
  instances; duplicate titles need three pages. One quiet page is a choice, a pattern is
  a defect.
- **Say when a finding is downstream.** If the page is empty because it renders
  client-side, the fix is server rendering, not copywriting. Attach `supersedes_hint`
  and say so in the evidence rather than recommending a rewrite of text that already
  exists in a JavaScript bundle.

## Output

`{ "meta": {...}, "findings": [...] }` in the marketplace's shared envelope. Findings may
additionally carry `supersedes_hint`, naming the `evidence_key` of a crawl-render finding
that explains this one. `id` is left null — the orchestrator assigns it.
