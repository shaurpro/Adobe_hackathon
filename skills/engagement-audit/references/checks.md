# engagement-audit — signal definitions

Each signal states the mechanism it instantiates, the test, the evidence that
**falsifies** a flag, and the severity rationale.

**Page classification runs first.** Every check below is applied only to the templates it
makes sense for. This single step removes the majority of engagement false positives.

| Kind | Detected by | Excluded from |
|---|---|---|
| `homepage` | path is `/` | E5 dead-end |
| `legal` | `/privacy`, `/terms`, `/cookie`, `/imprint`, `/accessibility`, `/policy` | E2, E4, E5 |
| `listing` | `/blog`, `/category`, `/tag`, `/shop`, `/products`, `/archive`, `/search` | E2, E4 |
| `content` | everything else | — |

---

## E1 — Orientation *(instantiates M2)*

**Mechanism.** M2: a source is used when a clear fact can be extracted from it in the
moment. The human version is identical — a visitor decides within seconds whether this
page is about their problem.

**Test.** Take the first ~200 words of main content on the homepage. Look for a
self-contained declarative sentence, 6–60 words, containing a copula or action verb
**and** a category noun (`company`, `platform`, `agency`, `clinic`, `retailer`, …). This
is a structural test, not a semantic one, so it does not require judging tone.

**Falsified by.** Before reporting the claim as *missing*, the check searches the whole
main text, the meta description, the JSON-LD `description`, and the about page. Finding
it anywhere converts the finding to `E1:claim-buried` (`medium`). Slogans that fail the
category test are correctly not counted — "Think different" is not an orientation
statement.

**Severity.** Nowhere on any machine-readable surface → `high`. Present but below the
first screen → `medium`. When the page body has fewer than 80 words, the evidence says
so and attaches `supersedes_hint: M3-S1:client-render`, because the copy may exist and
simply never reach the response.

---

## E2 — Answer-shaped structure *(instantiates M2)*

**Mechanism.** M2. Headings are how a scanner — human or machine — finds which passage
answers which question.

**Test.** On `content` pages of 150+ words:
- zero H2/H3 on a page of 400+ words → `E2:no-subheadings`
- mean paragraph length > 120 words across 3+ paragraphs → `E2:dense-paragraphs`
- zero H1, multiple H1s, or a level skip (H1→H3) → `E2:heading-hierarchy`

**Falsified by.** Legal and listing templates are excluded entirely. Pages under 400
words are not expected to carry sub-headings. Dense paragraphs are reported at `low`
because long-form editorial and narrative writing legitimately run long — this is advice,
not a defect.

**Severity.** Unstructured long pages → `medium`. Heading hierarchy → `medium` at 3+
pages (a template-level defect), `low` below that. Paragraph density → `low`.

---

## E3 — Entry friction *(instantiates M1)*

**Mechanism.** M1 stage 1, human version: the visitor has to be let in before anything
else matters.

**Test.**
- **Interstitials:** narrow markers only — `newsletter-popup`, `exit-intent`,
  `age-gate`, `welcome-mat`, `interstitial`, `popup-overlay`, `data-popup`. Generic
  `modal` class names are deliberately **not** matched.
- **Age gates:** explicit verification wording in body text.
- **Mobile:** absence of `<meta name="viewport">`.
- **First paint:** 8+ render-blocking external scripts, or a document over 1.5 MB.

**Falsified by.** The overlay-versus-wall distinction: an interstitial over readable
content is `medium`; the same over a body of fewer than 60 words is `high`. Cookie and
consent banners layered over readable content are never counted as walls. Age gates are
legally required in several categories, and the finding says so and asks for scope
narrowing rather than removal. The performance check is markup-derived, so it is capped
at `low` and explicitly labelled as an indicator needing field confirmation.

**Severity.** Wall → `high`. Overlay, age gate, missing viewport → `medium`.
Render-blocking → `low`.

---

## E4 — Structural noise *(instantiates M3)*

**Mechanism.** M3: a fact surrounded by low-value filler is harder to isolate. The
inbox-summary problem in the brief's appendix (section F) is the same failure — substance
drowned by surroundings.

**Test.** On `content` pages of 150+ words:
- link text > 50% of body words **and** 25+ in-body links → `E4:link-density`
- main content < 35% of all page text, on pages over 250 words → `E4:chrome-ratio`
- 60+ links in header/nav/footer → `E4:nav-bloat`

**Falsified by.** Listing and legal templates are excluded — link density is the entire
function of a category or blog-index page, and flagging it there is the classic
engagement-audit false positive. Both thresholds must be met for link density (a page
with 100 short links and lots of prose is fine). Nav bloat is `low` because large
retailers and publishers legitimately carry deep navigation.

**Severity.** Link density and chrome ratio → `medium`. Nav bloat → `low`.

---

## E5 — Next step *(instantiates M2)*

**Mechanism.** M2. The visitor extracted the fact they came for; if there is nothing to
do next, intent decays exactly when it peaks.

**Test.** On non-legal pages of 100+ words: is there any call-to-action phrase or contact
route (including `mailto:`/`tel:`) in body or link text? Separately, do any in-body
internal links exist, or is the page a dead end reachable only via global navigation?

**Falsified by.** Legal pages are excluded. Pages under 100 words are excluded. The
no-CTA finding fires only when it affects **two or more pages and at least 40%** of
substantive pages — one deliberately quiet page is an editorial choice, not a defect. The
homepage is exempt from the dead-end check, since its navigation *is* its onward path.

**Severity.** Sitewide absence of any next step → `high` (this is the direct cause of
arrival-without-conversion). Dead-end pages → `medium`.

---

## E6 — Labels a scanner can read *(instantiates M3)*

**Mechanism.** M3. Titles, link text and descriptions are the metadata both people and
machines use to decide what a page is before opening it.

**Test.**
- 3+ pages sharing an identical `<title>` → `E6:duplicate-titles`
- 5+ links whose text is generic (`click here`, `read more`, `learn more`) and which
  carry no `aria-label` → `E6:generic-links`
- meta description missing on 50%+ of pages → `E6:no-meta-description`
- 3+ titles over 70 characters → `E6:long-titles`

**Falsified by.** Links with an `aria-label` are excluded — the accessible name exists
even when the visible text is short by design. Duplicate titles need three pages so
paginated variants of one page do not trigger it. Thresholds throughout require a
pattern, not an instance.

**Severity.** Duplicate titles → `medium` (pages become indistinguishable in every
preview surface). Everything else → `low`.

---

## Suggested-action principles

1. **Match the fix to the stage.** An orientation problem is a copywriting fix; an
   interstitial is a configuration fix; a chrome-ratio problem is an information
   architecture fix. Never recommend a rewrite for something caused by rendering.
2. **One next step, not a menu.** The E5 action names a *specific* next step matched to
   the page's intent. Recommending "add more CTAs" recreates the E4 noise problem.
3. **Write the sentence, don't describe it.** The E1 action gives the exact template —
   `<Name> is a <category> that <does what> for <whom>, based in <where>` — and requires
   it to be mirrored into the meta description and Organization `description`, so the
   human-facing and machine-facing versions cannot drift.
4. **Beyond-defect improvements this skill should recommend even when nothing fails:**
   add an FAQ block using the questions support actually receives; put the single most
   asked-for fact (price, hours, location, lead time) in plain text above the fold;
   give fact-bearing pages a visible "last reviewed" date; state the intended audience
   explicitly in one sentence.
