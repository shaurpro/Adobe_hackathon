# freshness-corroboration — signal definitions

Every signal states: the mechanism it tests, how it is tested, what evidence
**falsifies** the flag (the false-positive guard), and the severity rationale.

Severity across this marketplace is `stage_of_failure × blast_radius × certainty`.
This skill sits at the **trust** stage — later than crawl/read/extract — so its ceiling
is lower by construction: a corroboration gap cannot outrank a locked door. Anything
whose evidence comes from a live search surface is additionally capped at `medium`
(see M6-S3 in the orchestrator).

---

## M4-S1 — Independent restatement of core facts *(probe)*

**Mechanism.** M4: facts repeated consistently across independent sources are trusted
more than facts living in one place only.

**Test.** Not scriptable — delegated to the agent as probe `P-CORROBORATION`. Extract the
entity's primary identity claim (name + category + differentiator). Search for it. Count
**distinct registrable domains**, excluding the audited domain, its subdomains and its
CDN, that state the same claim.

**Independence filter.** Before counting, discard:
- press-release syndication (the same wording on many domains — compare 5-gram shingles;
  Jaccard > 0.8 means one source, not many),
- scraped directory clones,
- the brand's own social profiles (those are *declaration*, not corroboration — they are
  M4-S2's job).

**Falsified by.** Any independent domain restating the claim. Also: a young brand, a
deliberately private company, or a site under ~10 pages — expectation scales to
observable size, and thin coverage there is a growth opportunity, not a defect.

**Severity.** `medium` ceiling even at zero corroboration, because the evidence is
personalized and non-reproducible. Pair with a **high-priority** proactive action —
this is usually the highest-leverage recommendation in the whole report, since it is the
one thing on-page work cannot fix.

---

## M4-S2 — Declared-identity round trip

**Mechanism.** M4. `sameAs` is the site's own claim about where else this entity exists.
An unverifiable claim adds nothing; a broken one actively weakens the identity graph.

**Test.** Collect `sameAs` from Organization-type JSON-LD plus social/profile links in
page chrome. Fetch each (capped, rate-limited). Classify:

| Result | Meaning |
|---|---|
| 2xx, brand-name tokens present | verified |
| 4xx/5xx | dead |
| 2xx, zero brand-name token overlap | possible mismatch |
| 403 / 429 / 999 / network error | **unverified** — no conclusion drawn |

**Falsified by.** Platform-level bot blocking (403/999) — extremely common on major
social networks and never counted as a defect. Client-side-rendered profile pages look
empty to a plain fetch, so a name mismatch is reported at `medium` with
`confidence: "inferred"` and an explicit instruction to confirm manually. A business with
no social presence is not defective — the fix is a registry or directory entry.

**Severity.** Dead `sameAs` → `high` (the site asserts a corroborating source that does
not exist). Mismatch → `medium`. Profiles linked but not declared in `sameAs` →
`medium`. No external identity at all → `medium`. Unverifiable → `low`.

---

## M4-S3 — Internal contradiction and time decay

**Mechanism.** M4. Corroboration begins at home: a site that disagrees with itself
cannot be corroborated by anything.

**Test — contradiction.** Compare **like-typed fields only** across sampled pages:
`foundingDate`, Organization `PostalAddress`, Organization `telephone`, and
general-enquiry email addresses (`info@`, `hello@`, `contact@`, `sales@`, `support@`).
Flag when a single field carries more than one value at Organization level.

**Test — decay.** Extract: footer copyright year; `as of <year>` claims; forward-looking
copy (`coming in Q3 2024`, `launching 2023`) whose date has passed; newness language on
aged pages. Compare against the run year.

**Falsified by.**
- Department-specific phone numbers and regional office addresses are **not**
  contradictions — only Organization-level like-typed fields are compared.
- Archive-shaped URLs (`/blog/`, `/news/`, `/press/`, `/2019/`) are excluded from decay
  entirely. Old posts are supposed to be old.
- **A stale copyright year alone is never sufficient.** It emits only at `low` as a weak
  signal. A `medium` staleness finding requires a *second* independent signal.

**Severity.** Contradiction → `high`. Decay with two corroborating signals, or any
expired forward-looking claim → `medium` (expired copy actively misinforms). Copyright
year alone → `low`.

---

## M5-S1 — Ambiguity risk versus disambiguator supply

**Mechanism.** M5: shared or generic names get confused with other entities unless
something distinguishes this one.

**Test.** Two halves, and **both must fail** before the `high` finding fires.

1. *Ambiguity risk (script):* is the name a single common dictionary word, or two common
   words? Conservative by design — a missed ambiguity costs nothing, a false "your name
   is confusing" costs credibility.
2. *Disambiguator supply (script):* count hard disambiguators **co-located with the name
   in machine-readable form**: `legalName` or a legal suffix (Ltd/Inc/GmbH), `address`,
   `areaServed`, `foundingDate`, a category `description`, a stable `@id`, or a
   registration identifier (`taxID`, `leiCode`, `duns`, `vatID`, `tickerSymbol`).

Probe `P-AMBIGUITY` confirms a real-world collision before the finding is allowed to
escalate.

**Falsified by.** A coined, high-entropy name (no flag regardless of disambiguator
count — only the `low` proactive variant applies). Two or more disambiguators present.
Disambiguators count **wherever they are machine-readable** — an `address` in the
Organization node satisfies this even when the H1 is a bare brand word.

**Severity.** Common-word name **and** fewer than two disambiguators → `high`.
Distinctive name with thin disambiguators → `low`, framed as future-proofing.
Search-confirmed collision → capped at `medium` per M6-S3.

---

## M5-S2 — Canonical entity anchor

**Mechanism.** M5. Repeated evidence about *one* entity resolves it; scattered anonymous
assertions do not.

**Test.** Collect Organization-type nodes across the corpus. Group by `@id`. Flag when
one `@id` carries conflicting `name` or `url` values. Separately, check whether an entity
hub page exists (`/about`, `/company`, `/contact`, `/our-story`, `/team`).

**Falsified by.** Multiple Organization nodes with **distinct** `@id`s and distinct roles
(publisher vs parent company vs article author) are correct modelling, not conflict.
Single-page sites that state identity facts on the homepage satisfy the hub requirement —
the hub check requires 4+ crawled pages before it fires.

**Severity.** Conflicting values under one `@id` → `high` (a corrupted identifier is
worse than none). No entity hub on a 4+ page site → `medium`. No `@id` at all → `low`.

---

## M5-S3 — Naming consistency across owned surfaces

**Mechanism.** M5. If the brand's own surfaces cannot agree on its name, nothing
downstream can reconcile the variants.

**Test.** Normalize brand strings from `<title>`, `og:site_name` and JSON-LD `name`.
Group into families by stripping legal suffixes, then count undeclared variants per
family.

**Falsified by.** Variants already declared in `alternateName` — the machine can
reconcile those, so they are excluded. Localized names under correct `hreflang` are
correct. Marketing taglines appended to `<title>` are not name variants and are filtered
by requiring token overlap with the resolved entity name.

**Severity.** Three or more undeclared variants → `medium`. Two → `low`.

---

## M6-S1 — Intent-variant coverage

**Mechanism.** M6: identical questions surface different results per user, so there is no
single "correct" query to optimize for. The defence is breadth — cover the question
*shapes*, not one phrasing.

**Test.** Map titles, headings, nav link text and URLs against six buckets:
`what_it_is`, `who_it_is_for`, `how_much`, `where`, `how_it_compares`, `how_to_start`.
Report uncovered buckets.

**Falsified by.** Sites under 4 crawled pages are exempt — a single-product microsite
owes no comparison page. **At most the two weakest buckets** are ever reported, so the
finding cannot balloon into a content-marketing wishlist. `how_much` is treated as a
recommendation, never a defect: withholding pricing is a legitimate enterprise-sales
strategy, and the suggested action is to state a pricing *model*, not numbers.

**Severity.** Two or more uncovered buckets on a 4+ page site → `medium`. One → `low`,
proactive.

---

## M6-S2 — Context legibility

**Mechanism.** M6: personalization is matched against context signals — who the user is,
where they are, what language they read.

**Test.** `html[lang]` presence; `hreflang` reciprocity where multiple languages exist;
`areaServed` / `PostalAddress` / `location` in structured data, or an explicit coverage
sentence in body text; `audience` / `targetAudience` or an explicit "for &lt;who&gt;"
statement.

**Falsified by.** Any `areaServed`, address, or explicit "serving X" sentence suppresses
the geography finding **entirely** — it is not downgraded, it is not emitted.
Single-language sites need no `hreflang` and are never flagged. Broken `hreflang`
reciprocity is only a finding when alternate-language pages actually exist.

**Severity.** No geography signal anywhere → `medium` (location is a primary
personalization axis). Multiple languages with no `hreflang` → `medium`. Missing `lang`
→ `low`. Unstated audience → `low`, proactive.

---

## Suggested-action principles

1. **Fix the source, not the symptom.** Contradictory addresses across four surfaces is
   one defect — no single source of truth — not four defects. The action targets the CMS
   entity record, not the four templates.
2. **Corroboration actions must be earnable.** Do not recommend "get press coverage."
   Recommend the specific, obtainable surfaces: company registry entry, industry
   association directory, Wikidata item where notability is genuinely met, conference or
   partner listings, customer case studies published on the customer's own domain.
3. **Disambiguators go where machines read them.** Recommending "mention the city on the
   about page" is weak; recommending `address` + `foundingDate` + `legalName` inside the
   Organization node, co-located with the name, is the actual fix.
4. **Name the falsifier in the action.** Where the finding could be intentional
   (withheld pricing, no social presence, deliberate low profile), the action must say so
   and offer the alternative rather than assuming a defect.
