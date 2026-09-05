# crawl-render-audit — signal definitions

Every signal states: the mechanism it tests, how it is tested, the **falsifier** that
must not hold before it may emit, and the severity logic. A signal whose falsifier is met
does not emit at reduced severity — it does not emit at all.

Severity throughout: `severity = stage_of_failure × blast_radius × certainty`.
Crawl-stage > read-stage > extract-stage; site > template > page; observed > inferred >
unverified.

---

## M1-S1 — Crawler admission

**Mechanism.** M1, stage 1. If the crawler is not let in, nothing else matters.

**How tested.** Parse `/robots.txt` into per-user-agent groups, preserving group
membership. Evaluate `/` against each of: `GPTBot`, `OAI-SearchBot`, `ChatGPT-User`,
`ClaudeBot`, `Claude-User`, `anthropic-ai`, `PerplexityBot`, `Perplexity-User`, `CCBot`,
`Google-Extended`, `Googlebot`, `Bingbot`, `Applebot`, `meta-externalagent`, `Amazonbot`,
`Bytespider`. Matching uses longest-rule-wins with `Allow` beating `Disallow` on ties, and
supports `*` and `$` wildcards. Separately: check every sitemapped or nav-linked URL
against the rules, and check each sampled page for `noindex` in `X-Robots-Tag` or
`<meta name="robots">` / `<meta name="googlebot">`.

**Sub-cases and severity.**

| Condition | Severity | Why |
|---|---|---|
| robots.txt 5xx or unreachable | critical | Major crawlers treat a persistently failing robots.txt as a site-wide disallow. The site disappears wholesale. |
| ≥4 AI/search agents denied `/` | critical | Assistant traffic blocked as a class, sitewide, directly observed. |
| 1–3 agents denied `/` | medium | Narrow, and often a deliberate policy choice. |
| Linked/sitemapped **content** URL disallowed | high | The site advertises a door it has locked. Wastes crawl budget and hides real pages. |
| `noindex` on the homepage | critical | Removes the entity's primary anchor from every index. |
| `noindex` on ≥3 content pages | high | Template-level. |
| `noindex` on 1–2 pages | medium | Localized, often intentional. |

**Falsifiers.**
- Utility paths are excluded before counting: `/wp-admin`, `/admin`, `/api`, `/cart`,
  `/checkout`, `/account`, `/login`, `/search`, `/preview`, `/static`, `/_next`,
  `/cdn-cgi`, and any URL carrying `utm_`, `sort=`, `filter=`, `session`, `sid=`.
  Blocking these is correct hygiene.
- A `Disallow:` with an empty value means *allow all* and is not a block.
- Thank-you pages, paginated duplicates and staging URLs are legitimately `noindex`ed —
  the finding names the URLs so intent can be confirmed before acting.
- A deliberate policy of staying out of AI answers falsifies the *severity*, not the
  observation. Report the consequence; let the owner decide.

**404 on robots.txt is not a defect.** It means "crawl everything" and is valid.

---

## M1-S2 — Response integrity along the fetch path

**Mechanism.** M1, stages 1–2. A fetch that never lands on real content cannot be read.

**How tested.** Record status, hop count and final URL for every crawled page. Detect
soft 404s (HTTP 200 + error wording in title or body + fewer than 120 words of body
text). Parse `rel=canonical`, resolve it, and compare normalized paths.

**Severity.** Canonical pointing to a 404 or an unrelated domain → **high** (silently
deindexes the page). Soft 404 on a linked page → **high** (error pages get indexed and
compete with real ones). Internally linked 4xx/5xx → **high**. Redirect chain ≥3 hops →
**medium**.

**Falsifiers.**
- One hop for `http→https` or trailing-slash normalization is normal. Threshold is
  **three** hops.
- A directory URL and its index document (`/` vs `/index.html`, `/default.php`) are the
  same resource and are never reported as a canonical mismatch.
- Cross-domain canonicals are legitimate for syndicated content — verify the target is
  the same content before changing anything.
- A short page that merely contains the word "error" is excluded by the word-count
  condition.
- Deliberate `410 Gone` for retired content is correct.

---

## M1-S3 — Discovery surface completeness

**Mechanism.** M1, stage 1. The sitemap is a direct instruction about what to fetch.

**How tested.** Look for `Sitemap:` in robots.txt, then `/sitemap.xml`,
`/sitemap_index.xml`, `/sitemap-index.xml`. Parse `<loc>` and `<lastmod>`, follow one
level of index (first five children). Cross-check sitemap entries against crawl results,
and crawled URLs against the sitemap set.

**Severity.** >20% of sampled sitemap entries returning 4xx/5xx → **high** (actively
misdirects crawl budget). No sitemap on a site with 12+ pages → **medium**. Crawled pages
missing from the sitemap beyond a 30% threshold → **medium**. Sitemap undeclared in
robots.txt, or missing `lastmod` → **low**.

**Falsifiers.**
- Sites under ~12 reachable pages with complete internal linking are fully discoverable
  without a sitemap. The finding degrades to a **low** proactive suggestion rather than a
  defect.
- Sitemap URLs that merely 301 to their https or trailing-slash variant are not counted
  as dead.
- Paginated, filtered and tag pages are legitimately excluded from sitemaps, so the
  orphan finding lists the URLs rather than prescribing a bulk add.

---

## M2-S1 — Fetch gating for non-browser clients

**Mechanism.** M2. Assistants build answers from what they can fetch *at that moment*.
A refusal here means the page can never become a cited source.

**How tested.** Every request uses an honest, identifying, non-abusive user agent at a
low rate. Record 401/403/429. Scan the first 4 KB of each body for challenge signatures
(`just a moment`, `checking your browser`, `captcha`, `cf-browser-verification`,
`attention required`, `access denied`). Detect consent walls: a 200 response with fewer
than 60 words of body text where consent wording is present.

**Severity.** Refusals on ≥50% of fetches → **critical** (sitewide stage-1 failure).
Fewer → **high**. Bot challenge served as content → **high**. Consent wall withholding
the document → **high**.

**Falsifiers.**
- **An overlay is not a gate.** If the consent, cookie or newsletter modal is a DOM
  overlay and the body text is still present in the raw HTML underneath, no finding is
  raised. That is exactly what the 60-word condition encodes.
- A single 429 may be transient rate limiting. Re-run before escalating.
- Genuinely subscriber-only content behind a paywall is a business decision, not a defect.

---

## M3-S1 — Client-side render delta

**Mechanism.** M3. Content assembled after load is invisible to a fetcher that does not
execute JavaScript, even though it looks complete on screen.

**How tested.** Two paths, and the script uses whichever is available:

1. **Raw-only (always available).** Flag a page as a shell when the raw HTTP response
   yields fewer than 80 words of *main* body text, and either an empty mount node
   (`#root`, `#app`, `#__next`, `#___gatsby`) is present or there are three or more
   external scripts, and `<noscript>` provides fewer than 40 words of fallback.
2. **Headless comparison (when Playwright is present).** Compute
   `missing_ratio = 1 − |raw_tokens ∩ rendered_tokens| / |rendered_tokens|` over the main
   content region, and assert that the H1 and primary nav exist in the raw bytes.

**Severity.** Shells on ≥50% of sampled pages with no recoverable fallback → **critical**.
Some pages → **high**. Facts recoverable from JSON-LD or `<noscript>` → **medium**.
Confidence is `observed` when a headless comparison ran and `inferred` when only the
raw-HTML heuristic was available; the evidence string states which.

**Falsifiers.**
- The comparison covers the **main content region only**. Cookie banners, chat widgets,
  ad slots, comment threads and lazy-loaded "related posts" legitimately hydrate late and
  are excluded from the token diff.
- If the missing facts are present in raw JSON-LD or `<noscript>`, the machine still gets
  them — the finding is downgraded, and the evidence says so explicitly.
- If the plain-client fetch already contains the text (UA-conditional SSR), nothing fires.
- A missing browser is recorded as `render_check unavailable`, never as a defect.

---

## M3-S2 — Facts locked in non-text carriers

**Mechanism.** M3. A fact inside an image, PDF or video is not extractable text.

**How tested.** For non-chrome content images: count missing `alt`, and treat
filename-shaped alts (`chart.png`) or generic alts (`image`, `photo`, `logo`) as missing.
Detect PDF links whose anchor text or URL matches price/spec/datasheet/catalog/menu/rate/
fee vocabulary, or that sit in the page chrome. Detect `<video>` with no `<track>` on a
page carrying under 250 words of body text.

**Severity.** Decision-relevant facts (pricing, specs, hours, policies) available only as
PDF → **high**. Missing alt across >40% of content images → **medium**, below that
→ **low**. Video with no transcript carrying a thin page → **medium**.

**Falsifiers.**
- **`alt=""` on decorative images is correct accessibility practice and is never
  counted.** Neither are images under 48×48 px or `role="presentation"`.
- A PDF is not a finding when an HTML page states the same facts — check for the HTML
  equivalent first.
- Pages with 250+ words of body text alongside a video already carry the facts in text.

---

## M3-S3 — Structured data: presence, validity **and** agreement

**Mechanism.** M3. Structured data is the most direct path from page to extractable fact
— but only when it parses and tells the truth.

**How tested.** Extract every `script[type="application/ld+json"]` plus microdata
`itemtype`. `json.loads` each block and flatten `@graph`. Infer page type from URL shape
and declared `@type`. Scan for placeholder patterns (`YOUR_COMPANY`, `example.com`,
`lorem ipsum`, `{{`, `${`, `changeme`, `TODO`). Cross-check against the visible page:
JSON-LD `name` versus H1, and `offers.price` versus prices in body text.

**Severity.** Parse failure → **high** (an invalid block is discarded wholesale, so the
site pays the cost and gets none of the benefit). Placeholder values → **high** (actively
asserts false facts). Contradiction with visible text → **high**. No entity markup
anywhere on the site → **high**. Type-appropriate schema missing on >30% of eligible
templates → **medium**.

**Falsifiers.**
- Page type is inferred **first**. Absent `Product` schema on an about page is not a
  finding.
- A valid `Organization`/`WebSite` on the homepage satisfies the entity requirement even
  if leaf pages carry only `BreadcrumbList`.
- Multiple JSON-LD blocks are normal (CMS + plugin). Only conflicts are reported.
- Shortened display titles are common, so only a **zero-word overlap** between JSON-LD
  `name` and H1 counts as a contradiction.
- Pages with no matching schema.org type (generic landing, legal, utility) are excluded
  from the coverage count.

---

## Suggested-action principles

Every action names the mechanism it repairs and the smallest change that repairs it.

- Prefer the fix that unblocks the earliest stage. One robots.txt line beats a schema
  rollout.
- Prefer fixes that cannot regress: generate JSON-LD from the same data source that
  renders the page, so markup and text cannot drift; add a build-time `JSON.parse` check
  so invalid markup cannot ship; automate the sitemap from live canonical URLs.
- Never recommend "add schema" generically. Name the type, the properties, and the source
  of the values.
- Where a defect reflects a policy choice (blocked AI agents, paywalled content), present
  the trade-off rather than a directive.
