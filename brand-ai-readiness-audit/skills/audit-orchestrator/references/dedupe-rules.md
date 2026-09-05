# Dedupe and prioritisation rules

Applied in this order by `scripts/orchestrate.py`. Each rule exists because a specific
class of unreadable or misleading report exists without it.

---

## Rule 1 — Confidence ceiling (before any merging)

Applied first so that later merges compare capped severities. See
`severity-model.md`. Records `severity_before_cap` and `cap_reason`.

**Worked example.** `crawl-render-audit` reports a client-side render gap at `critical`.
Playwright was unavailable, so confidence is `inferred`. The orchestrator caps it to
`high` and writes: *"severity capped at 'high' because evidence confidence is
'inferred'"*. The finding stays, the reader learns exactly how sure to be.

---

## Rule 2 — Identical `evidence_key`

Two skills observing the same defect produce the same `evidence_key`. Merge into one
finding: highest severity wins, `affected_urls` are unioned, `merged_from` lists the
contributing skills, and the second skill's evidence is appended rather than dropped.

Keys are structured `SIGNAL:cause` (e.g. `M1-S1:root-block`, `E3:interstitial`), never
free text — string-similarity matching on titles produces both false merges and missed
merges.

---

## Rule 3 — `supersedes_hint` (root-cause collapse)

The rule that makes the decomposition honest.

A skill that detects a **symptom** whose **cause** belongs to another skill declares
`supersedes_hint` naming the cause's `evidence_key`. If that cause is present in the
merged set, the symptom is folded into it as a `related_effect` and stops being counted.

**Worked example.** A React site ships an empty `<div id="root">`.

- `crawl-render-audit` → `M3-S1:client-render`: the server response contains 12 words, no
  H1, three external scripts. *(cause)*
- `engagement-audit` → `E1:no-claim`: no sentence states what the organisation is, and
  the body has under 80 words, so it attaches `supersedes_hint: M3-S1:client-render`.
  *(symptom)*

Without this rule the report shows two findings and implies two fixes, one of which
("write a positioning sentence") is wrong — the sentence may already exist inside a
JavaScript bundle. After the rule: one finding, one fix, with the orientation impact
listed as a related effect so the reader still sees the human cost.

Counted in `audit_scope.findings_folded_into_root_causes`.

---

## Rule 4 — Template rollup

Three or more `page`-scoped findings sharing a signal collapse into one `template`-scoped
finding, with the page count in the evidence and up to ten example URLs.

The analysis skills already aggregate, so this is mostly a safety net — but it means a
future skill that emits per-page findings cannot flood the report. Forty near-identical
entries is an unreadable report, and an unreadable report does not get acted on.

---

## Rule 5 — Prioritisation

Sort key, in order:

1. severity (desc)
2. **failure stage** — M1 → M2 → M3 → M4 → M5 → M6
3. blast radius — `site` → `template` → `page`
4. confidence — `observed` → `inferred` → `unverified`
5. title (stable tiebreak, so identical inputs always produce identical output)

Stage beats volume. A site with one robots.txt block and twelve schema gaps sorts the
block first, because fixing the schema on a site no crawler may enter changes nothing.

---

## Rule 6 — Id assignment

`F-001…` assigned **after** sorting, so id order is fix order. Recommendations are
numbered separately (`R-001…`) and never collide with finding ids — the validator asserts
this.

---

## What is deliberately not deduped

- **The same signal at different scopes.** A sitewide noindex and a single-page noindex
  are different problems with different fixes.
- **Findings sharing a mechanism but not a cause.** `M3-S2` (facts in images) and `M3-S3`
  (missing schema) both serve extractability and both stay.
- **A finding and its own proactive counterpart.** These never coexist: the recommendation
  is suppressed by `skip_if_any` when the defect is present.
