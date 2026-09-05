# brand-ai-readiness-audit

An Agent Skill Marketplace that points a general agent at any website and returns one
report: what is stopping AI assistants from finding, reading and citing the brand, and
what is stopping human visitors from staying once they arrive.

**Recommend-only.** Nothing here writes to, authenticates against, or otherwise alters
the audited site. Every request is a read-only GET, robots.txt is obeyed for the
marketplace's own fetching, and the whole audit is budgeted to finish in under five
minutes on a typical site.

---

## Why these four skills

Every check in this marketplace is derived from one of six mechanisms describing how
retrieval systems actually behave, not from an off-the-shelf SEO checklist. The
mechanisms are the unit of decomposition, which is why the skills split where they do:

| Mechanism | What it says | Skill that owns it |
|---|---|---|
| **M1** Crawl → Read → Extract | A fact is usable only if the crawler is let in, can parse the page, and can isolate that fact in plain text. Failure at any stage = invisible. | `crawl-render-audit` |
| **M2** Answers are built in the moment | Sources must be easy to reach, easy to read, easy to quote a clear fact from. | `crawl-render-audit` (reachability) + `engagement-audit` (quotability) |
| **M3** Machine ≠ human readability | Client-assembled, image/PDF-bound, or merely implied content is missed even when it looks present. | `crawl-render-audit` |
| **M4** Cross-source corroboration | Facts repeated consistently across independent sources are trusted; facts living in one place are fragile. | `freshness-corroboration` |
| **M5** Entity disambiguation | Shared or generic names get confused with other entities unless something distinguishes this one. | `freshness-corroboration` |
| **M6** Personalization | Identical queries surface different results per user, so no single "correct" answer exists to validate against. | `freshness-corroboration` (site-side) + `audit-orchestrator` (determinism gate) |

Engagement is deliberately **not** given its own theory. A human landing on a page is a
slow, impatient crawler running the same three-stage gate: they must be let in
(interstitials, consent walls, load weight), be able to read (orientation, hierarchy,
noise), and extract the one fact they came for (what is this, is it for me, what next).
`engagement-audit` therefore re-instantiates M1/M2/M3 with a human parser. That is also
why the same underlying defect can legitimately produce two findings — and why the
orchestrator carries real dedupe logic rather than string matching.

---

## The skills

### `crawl-render-audit` — can a machine reach, parse and extract?
Checks crawler admission per user agent (including AI retrieval agents), robots.txt
health, noindex directives, redirect chains, soft 404s, canonical correctness, sitemap
liveness and coverage, refusal of plain HTTP clients (403/429/bot challenges/consent
walls), client-side render gaps, facts locked in images/PDFs/video, and structured data
— not just whether JSON-LD exists, but whether it parses, contains real values rather
than template placeholders, and agrees with the visible page.

Ships `scripts/crawl_render_audit.py`, which performs the deterministic half of this
work with zero third-party dependencies.

### `freshness-corroboration` — is the entity resolvable and believed?
The declared-identity round trip (does every `sameAs` target resolve and describe the
same entity?), internal contradiction detection across footer/contact/JSON-LD fields,
time decay (expired forward-looking copy, stale "as of" claims), name-ambiguity risk
weighed against the disambiguators the site actually supplies, entity-anchor integrity
(`@id` stability), naming consistency, intent coverage, and context legibility
(`lang`, `hreflang`, `areaServed`, stated audience).

Because M4 and M5 partly depend on live web search — a personalized, non-reproducible
surface — this skill splits its output. Anything it observed directly becomes a finding.
Anything requiring search becomes a **manual probe**: an explicit query set the agent
runs *twice*, with a severity ceiling attached.

### `engagement-audit` — does the human who arrives stay?
Above-the-fold orientation, presence of a self-contained quotable claim, heading
structure and scannability, structural noise (interstitials, link density, chrome-to-
content ratio), page weight and render-blocking resources, and whether a next step
exists and is reachable.

### `audit-orchestrator` — the entrypoint
Invokes the three analysis skills against a single shared page corpus (each page is
fetched at most once, which is what keeps the run inside the five-minute budget), merges
their findings, dedupes, applies the determinism gate, computes severity and summary
counts, and emits the final report.

---

## How the entrypoint composes them

```
                 audit request (URL)
                          │
              ┌───────────▼────────────┐
              │   audit-orchestrator   │  1. normalize URL, fetch robots.txt
              │      (entrypoint)      │  2. build ONE shared page corpus
              └───────────┬────────────┘
                          │  pages.json (fetched once, reused by all)
      ┌───────────────────┼───────────────────┐
      ▼                   ▼                   ▼
crawl-render-audit  freshness-corroboration  engagement-audit
  M1, M2, M3           M4, M5, M6              M1/M2/M3 (human parser)
      │                   │                    │
      └───────────────────┼────────────────────┘
                          ▼
              merge → dedupe → determinism gate
              → severity → prioritize → summary
                          ▼
                  audit report (JSON)
```

Each analysis skill is independently runnable and emits the same intermediate envelope:

```json
{ "meta": { "skill": "...", "mechanisms": ["M1"], "manual_probes": [] },
  "findings": [ { "signal": "M1-S1", "mechanism": "M1", "scope": "site",
                  "confidence": "observed", "evidence_key": "...", ... } ] }
```

The extension fields beyond the required report schema (`signal`, `mechanism`, `scope`,
`confidence`, `evidence_key`, `non_deterministic`) exist so the orchestrator can merge
deterministically instead of guessing from title similarity. They are preserved in the
final report because they make a finding auditable, but the required floor schema is
always present.

---

## How findings are deduped and prioritized

Dedupe runs in five passes, in order:

0. **Confidence ceiling** — applied before any merging, so later passes compare capped
   severities. A finding may never outrank the quality of its evidence: `observed` can
   reach `critical`, `inferred` is capped at `high`, `unverified` and anything
   non-deterministic at `low`. Every cap records `severity_before_cap` and a
   `cap_reason`, because a silent demotion is indistinguishable from a bug.
1. **Identical `evidence_key`** — two skills observed the same defect. Merge into one
   finding, keep the highest severity, union the affected URLs, and concatenate the
   distinct evidence strings.
2. **Root-cause supersession** — a declared table maps downstream symptoms to upstream
   causes. If a page's body text is absent from the server response (`M3-S1`), the
   engagement finding "no orientation above the fold" is a *consequence*, not an
   independent problem. The upstream finding survives; the downstream one is attached to
   it as a `related_effect` so the impact is still visible without inflating the count.
3. **Template roll-up** — page-level findings sharing a template signature (URL shape
   with digits and slugs collapsed, plus the heading skeleton) collapse into one
   template-scoped finding once three or more pages match, with the count carried in the
   evidence rather than emitted as N near-duplicate rows.
4. **Determinism gate (M6-S3)** — any finding whose evidence came from a live
   third-party surface is required to have appeared in both probe runs. Stable findings
   keep their computed severity, capped at `medium`. Unstable ones are demoted to `low`,
   marked `non_deterministic: true`, and carry the raw query and timestamps so a reader
   can judge for themselves. No finding is ever phrased as "assistant X doesn't cite
   you", because that observation is not reproducible for a different user.

Severity is computed, never assigned by taste:

```
severity = stage_of_failure  ×  blast_radius  ×  certainty
```

A crawl-stage break outranks a read-stage break, which outranks an extract-stage break,
because an earlier failure makes every later fix worthless. Sitewide outranks
template-level outranks single-page. Directly observed outranks inferred outranks
unverified. A **contradiction outranks an absence** at equal scope: when markup and page
disagree, the safest machine behaviour is to trust neither, so a contradiction suppresses
a fact more effectively than silence does.

Prioritization of `suggested_action` follows the same order, with one override: a
low-effort fix that unblocks a whole stage (a single robots.txt line, a dynamic
copyright year) is promoted above an equally-severe fix requiring a re-platform.

### Findings versus recommendations

Detected problems live in `findings[]`. Beyond-defect improvements live in a separate
`proactive_recommendations[]` array, ordered by their own action priority. A
recommendation is not a detected problem: counting it as one inflates the summary and
makes a clean site look broken. Each recommendation is suppressed automatically when it
would merely restate a defect that was already found — so a site missing structured data
gets the *finding*, not the polite suggestion.

---

## False positives

The rubric penalizes false positives, so every check carries an explicit falsifier — a
condition that proves the flag wrong. When a falsifier is met, the check **does not
emit**; it does not merely lower severity. Findings that survive carry their falsifier
in the report as `falsified_by`, so a reader can immediately see what would make the
finding wrong. Representative guards:

- Blocking `/cart`, `/admin`, `/search?` in robots.txt is hygiene, not a defect.
- `alt=""` on decorative images is correct accessibility and is never flagged.
- A cookie banner rendered as a DOM overlay is fine — the finding fires only when the
  body text is genuinely absent from the raw HTML underneath it.
- A single `http→https` redirect hop is normal; the threshold is three.
- Long paragraphs are legitimate in long-form editorial and legal pages, so readability
  checks are scoped by inferred page type.
- A PDF is not a finding when an HTML page states the same facts.
- Old blog posts are supposed to be old; dated archive URLs are excluded from staleness.
- Coined, high-entropy brand names do not need heavy disambiguation.
- A 403 or 429 from a social platform is recorded as `unverified`, never as `broken`.

---

## Running a test audit

Each analysis skill is a standalone script with **no third-party dependencies** (Python
3.8+, standard library only). A headless browser is optional: when Playwright is absent
the render check falls back to raw-HTML SPA-shell detection and records why, rather than
guessing.

```bash
# End-to-end, via the entrypoint
python3 skills/audit-orchestrator/scripts/orchestrate.py \
    --url https://example.com --out report.json

# Always validate the emitted report
python3 skills/audit-orchestrator/scripts/validate_report.py report.json

# Or run one analysis skill on its own
python3 skills/crawl-render-audit/scripts/crawl_render_audit.py \
    --url https://example.com --max-pages 20 --render off
```

To test without touching a live site, run the bundled suite:

```bash
bash testing/smoke_test.sh
```

It builds two local fixture sites — one deliberately broken, one deliberately clean —
then asserts that the manifest and every skill's frontmatter are valid, that all planted
defect classes are detected, that the clean site produces **zero** critical/high findings,
and that two runs are byte-identical. Full instructions, including how to hand-trace the
merge logic without an agent framework and how to review findings on real sites, are in
`TESTING.md`.

---

## Layout

```
brand-ai-readiness-audit/            <- marketplace root (this is what you zip)
├── marketplace.json                 <- manifest: 4 skills, exactly one entrypoint
├── README.md
├── TESTING.md                       <- run it by hand, hand-trace the merge, review real sites
├── testing/
│   ├── make_fixtures.py             <- builds a broken + a clean fixture site
│   └── smoke_test.sh                <- 5 gates incl. the false-positive control
└── skills/
    ├── audit-orchestrator/          <- ENTRYPOINT
    │   ├── SKILL.md
    │   ├── scripts/
    │   │   ├── orchestrate.py       <- invokes siblings, merges, prioritizes, emits report
    │   │   ├── validate_report.py   <- enforces the required schema
    │   │   └── auditlib.py
    │   └── references/
    │       ├── severity-model.md
    │       ├── dedupe-rules.md
    │       └── example-report.json
    ├── crawl-render-audit/          <- M1-M3
    │   ├── SKILL.md
    │   ├── scripts/{crawl_render_audit.py, auditlib.py}
    │   └── references/checks.md
    ├── freshness-corroboration/     <- M4-M6
    │   ├── SKILL.md
    │   ├── scripts/{corroboration_probe.py, auditlib.py}
    │   └── references/checks.md
    └── engagement-audit/            <- E1-E6 (human-parser instantiation of M1-M3)
        ├── SKILL.md
        ├── scripts/{engagement_audit.py, auditlib.py}
        └── references/checks.md
```

`auditlib.py` is duplicated into each skill's `scripts/` folder on purpose: the
agentskills.io format requires each skill folder to be independently valid and portable,
so no skill may depend on a file living outside itself.

## Requirements

Python 3.8+, standard library only. No pip install. A headless browser (Playwright) is
optional — when absent, the render check falls back to raw-HTML shell detection and
records *why* the browser comparison is missing rather than inferring a defect from a
missing tool.
