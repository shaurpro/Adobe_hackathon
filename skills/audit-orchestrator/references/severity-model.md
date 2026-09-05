# Severity model

One model, applied identically by every skill in the marketplace. Severity is
**computed**, not assigned by taste.

```
severity = stage_of_failure  x  blast_radius  x  certainty
```

## 1. Stage of failure

A break earlier in the pipeline makes every later fix worthless, so stage dominates.

| Stage | Mechanisms | Meaning |
|---|---|---|
| Crawl | M1 | The agent is never let in. Nothing downstream matters. |
| Read | M2, M3 | The agent is let in but cannot parse what it receives. |
| Extract | M3 | The agent can parse the page but cannot isolate the fact. |
| Trust | M4, M5 | The fact is extractable but unresolvable or uncorroborated. |
| Match | M6 | Everything works, but the content never matches this user's framing. |

This is why the report sorts by stage after severity: one robots.txt block outranks a
dozen schema gaps.

## 2. Blast radius

`site` > `template` > `page`. A defect in a base template is one fix with sitewide
effect; the same defect on one page is a content task.

## 3. Certainty

| Confidence | Meaning | Ceiling |
|---|---|---|
| `observed` | Directly measured from fetched bytes | `critical` |
| `inferred` | Deduced without direct confirmation (e.g. render gap without a browser) | `high` |
| `unverified` | Could not be checked (platform blocked the fetch) | `low` |
| `non_deterministic` | Evidence came from a personalized surface and was unstable across repeat runs | `low` |

Caps are applied by the orchestrator and always leave a trace: `severity_before_cap` and
`cap_reason`. A silent demotion is indistinguishable from a bug.

## Resulting bands

| Severity | Meaning |
|---|---|
| `critical` | Crawl-stage failure, sitewide, directly observed. The brand cannot be found at all. |
| `high` | Read/extract failure at template scope, **or any contradiction**. |
| `medium` | Extract-stage degradation, single-template gaps, trust-stage gaps, or anything whose evidence is personalized. |
| `low` | Cosmetic, single-page, weak-signal, unverifiable, or proactive. |

## Why a contradiction outranks an absence

When a site states two different founding years, or JSON-LD disagrees with the visible
price, the safest machine response is to trust **neither** value. A contradiction
therefore suppresses the fact more effectively than never stating it would, and it can
propagate wrong data into answers. Contradictions are `high` even when the equivalent
absence would be `medium`.

## Ceiling for search-derived evidence

Mechanism 6 says identical queries return different results per user. Any finding whose
evidence comes from a live search surface is capped at `medium` and must be stable across
two runs, or it is demoted to `low` and marked `non_deterministic`. This is the single
most important guard against unfalsifiable findings in the whole marketplace.
