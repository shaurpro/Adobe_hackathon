---
name: freshness-corroboration
description: Audit whether a brand is resolvable and believed once a machine can already read its pages — cross-source corroboration of core identity facts, declared-identity round trips through sameAs profiles, contradictions between a site's own pages, decayed and expired claims, generic or ambiguous entity names lacking disambiguators, unstable entity identifiers, undeclared name variants, and missing context signals (geography, language, audience) that personalized assistants match against. Use when a brand is found but described wrongly, confused with a similarly-named entity, cited with stale facts, or surfaced for some users and not others. Recommend-only — never modifies the audited site.
license: MIT
allowed-tools:
  - http-fetch
  - web-search
  - file-read
  - file-write
  - shell
---

# Freshness & Corroboration Audit (entity resolution + trust)

## When to use

Run this **after** `crawl-render-audit`. It assumes a machine can already reach and parse
the pages, and asks the next questions: *is this entity resolvable, is it corroborated,
and are its facts still true?*

This is the half most audits skip. A site can be perfectly crawlable, perfectly
structured, and still be invisible in practice — because the entity cannot be told apart
from three other companies with the same name, or because every fact about it exists in
exactly one place and nothing on the wider web agrees.

Covers mechanisms **M4** (cross-source corroboration), **M5** (entity disambiguation) and
**M6** (personalization).

## Inputs

- `url` (required) — domain or full URL.
- `pages_in` — path to the page corpus published by `crawl-render-audit`. **Strongly
  preferred**: reusing it avoids a second crawl and keeps the marketplace inside the
  5-minute budget.
- `max_profiles` (default 8) — ceiling on outbound `sameAs` verification fetches.
- `no_external` — skip outbound fetches entirely (offline / rate-limited environments).

## Safety

Read-only GETs. The only outbound requests beyond the audited domain are to profile URLs
**the site itself declares** via `sameAs` or footer links — this skill does not roam the
web unprompted. No authentication, no POSTs, no writes to the audited site. Outbound
fetches are rate-limited and capped.

## Procedure

1. **Load or build the page corpus.** Prefer `--pages-in` from the sibling skill.
2. **Resolve the entity name** in priority order: `Organization` JSON-LD `name` →
   `og:site_name` → `<title>` suffix → registrable domain. **Always record which source
   won** (`meta.entity_name_source`). A name resolved from the domain is a weak
   foundation, and every downstream judgement inherits that weakness — say so rather than
   pretending the name is authoritative.
3. **Round-trip the declared identity** (M4-S2): fetch each `sameAs` target, classify as
   live / dead / name-mismatched / unverifiable.
4. **Compare the site against itself** (M4-S3): like-typed fields across pages —
   `foundingDate`, `PostalAddress`, `telephone`, general-enquiry emails — plus date decay.
5. **Test entity resolvability** (M5-S1/S2/S3): ambiguity risk versus disambiguator
   supply, `@id` stability, entity-hub presence, undeclared name variants.
6. **Test context legibility** (M6-S1/S2): intent coverage, `lang`, `hreflang`,
   `areaServed`, stated audience.
7. **Emit findings plus `manual_probes`** — see below.

```bash
python3 scripts/corroboration_probe.py --url <URL> \
    [--pages-in pages.json] [--out findings.json] \
    [--max-profiles 8] [--no-external]
```

No third-party dependencies (Python 3.8+, standard library only).

## The script/agent split — and why it exists

Some checks in this skill **cannot be made deterministic**, and pretending otherwise
would be the single biggest source of false positives in the whole marketplace.

Mechanism 6 says identical queries return different results for different users. So a
check phrased as *"assistants don't cite this brand"* is not reproducible: it was true for
one session, in one location, with one account's history. Encoding that as a finding
means shipping an unfalsifiable claim.

The split:

- **The script emits `findings[]`** — everything observable from the site's own bytes and
  from the profiles it declares. Reproducible, defensible, citable.
- **The script emits `manual_probes[]`** — a specification for the search-dependent half:
  the exact queries, the counting method, the flag condition, and a severity ceiling. The
  agent executes these, **twice**, and reports both result sets.

**Probe handling rules:**

- A probe finding whose result is **stable across both runs** may be emitted at up to its
  `severity_ceiling` (never above `medium`, per M6-S3).
- A probe finding that is **unstable across runs** is emitted at `low` with
  `non_deterministic: true`, and the evidence must record both result sets verbatim with
  timestamps.
- Never emit a probe finding phrased as a claim about a specific assistant's behaviour.
  Phrase it as a property of the web: *"no independent domain restates the identity
  claim"*, not *"ChatGPT ignores this brand."*

Current probes: `P-CORROBORATION` (M4-S1, counts independent restating domains) and
`P-AMBIGUITY` (M5-S1, confirms a real-world name collision before the ambiguity finding
is allowed to escalate).

## Checks

Full definitions, evidence formats and falsifiers: `references/checks.md`.

| Signal | Mechanism | Tests |
|---|---|---|
| M4-S1 | M4 | Independent restatement of core facts across distinct registrable domains *(probe)* |
| M4-S2 | M4 | Declared-identity round trip: `sameAs` targets live, reachable, and naming the same entity |
| M4-S3 | M4 | Internal contradiction on like-typed fields; time decay in dated and forward-looking claims |
| M5-S1 | M5 | Ambiguity risk versus disambiguator supply *(script + probe)* |
| M5-S2 | M5 | Canonical entity anchor: stable `@id`, no conflicting values, an entity hub page exists |
| M5-S3 | M5 | Undeclared brand-name variants across title / `og:site_name` / JSON-LD |
| M6-S1 | M6 | Intent-variant coverage across six common question shapes |
| M6-S2 | M6 | Context legibility: `lang`, `hreflang` reciprocity, `areaServed`, stated audience |

## Reasoning rules

- **A contradiction outranks an absence.** When a site states two founding years, a
  retrieval system's safest move is to trust neither, so the fact is suppressed *more*
  effectively than if it had never been stated. Contradiction findings are `high`;
  comparable gaps are `medium`.
- **Unverifiable is not broken.** Major platforms return 403 or 999 to automated fetches.
  That is a property of the platform, not a defect in the site. Record `unverified` with
  `confidence: "unverified"` and severity `low` — never convert a blocked fetch into a
  dead-profile finding.
- **Thin coverage is not always a defect.** A young or deliberately low-profile brand
  legitimately has little external corroboration. Scale the expectation to observable site
  size, and prefer a proactive high-priority action over a defect finding.
- **Old is not stale.** A blog post from 2019 is *supposed* to be from 2019. Only
  undated, evergreen, fact-bearing pages decay. Archive-shaped URLs are excluded, and a
  stale copyright year alone never justifies a staleness finding — a second independent
  signal is required.
- **Do not invent a name.** If the entity name resolved from the domain rather than from
  structured data, that itself is the finding worth reporting; downstream ambiguity
  judgements built on a guessed name are weak and should be phrased as such.
- **Withheld pricing is a strategy, not a bug.** Some intent gaps (notably `how_much`)
  are deliberate commercial choices. Recommend stating a pricing *model* without numbers;
  never file it as a defect.

## Output

`{ "meta": {...}, "findings": [...] }`. `meta` carries `entity_name`,
`entity_name_source`, `pages_analysed`, `mechanisms`, and `manual_probes`. Findings use
the same envelope as every skill in this marketplace: required report fields plus
`signal`, `mechanism`, `scope`, `confidence`, `evidence_key`, `affected_urls`,
`non_deterministic` and `falsified_by`. `id` is left null — the orchestrator assigns it.
