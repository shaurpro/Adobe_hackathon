# TESTING.md — running and sanity-checking this marketplace without an agent framework

Everything here runs with **Python 3.8+ and bash**. No pip install, no agent runtime, no
network access required for the fixture tests.

The point of this document is to let you verify the marketplace before submitting or
deploying it, and — more importantly — to let you check the thing that actually matters:
**that the checks don't fire on sites that are fine.**

---

## 0. One-command check

```bash
bash testing/smoke_test.sh
```

This is the whole test suite. It validates the manifest and every skill's frontmatter,
builds two local fixture sites, runs the full audit against both, and asserts:

| Gate | What it proves |
|---|---|
| 1. Structure | manifest parses, exactly one entrypoint, every skill has valid frontmatter whose `name` matches its folder |
| 2. Fixtures | both fixture sites are serving |
| 3. Detection | every planted defect class is found, and the report is schema-valid |
| 4. **False positives** | the clean site produces **zero** critical/high findings |
| 5. Determinism | two identical runs produce identical findings |

Expected output ends with five `PASS` lines and exit code 0. Gate 4 is the one to watch:
a check that fires on the clean fixture is wrong, however sensible it looked when written.

---

## 1. Running the marketplace by hand

The entrypoint is a normal script. You do not need an agent to run it.

```bash
python3 skills/audit-orchestrator/scripts/orchestrate.py \
    --url https://example.com --out report.json

python3 skills/audit-orchestrator/scripts/validate_report.py report.json
```

Useful flags while testing:

| Flag | Use |
|---|---|
| `--max-pages 8` | faster runs while iterating |
| `--render off` | skip the headless browser entirely |
| `--no-external` | never fetch third-party profile URLs (offline / rate-limited) |
| `--delay 0.05` | only for local fixtures — **keep the default 0.4s for real sites** |
| `--workdir ./work` | keep the shared page corpus and per-skill outputs for inspection |

With `--workdir`, you can inspect exactly what each skill saw:

```
work/pages.json                    <- the shared corpus, fetched once
work/crawl-render-audit.json       <- that skill's raw envelope
work/freshness-corroboration.json
work/engagement-audit.json
```

---

## 2. Running one skill in isolation

Each skill's script is standalone and prints JSON to stdout when `--out` is omitted. This
is how you debug a single finding without re-running everything.

```bash
# crawl / render / extract only
python3 skills/crawl-render-audit/scripts/crawl_render_audit.py \
    --url https://example.com --max-pages 10 --render off

# reuse a corpus so the siblings make zero network requests
python3 skills/crawl-render-audit/scripts/crawl_render_audit.py \
    --url https://example.com --pages-cache /tmp/pages.json --out /tmp/cr.json
python3 skills/freshness-corroboration/scripts/corroboration_probe.py \
    --url https://example.com --pages-in /tmp/pages.json --out /tmp/fc.json
python3 skills/engagement-audit/scripts/engagement_audit.py \
    --url https://example.com --pages-in /tmp/pages.json --out /tmp/en.json
```

Skim findings quickly:

```bash
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); \
[print(f\"[{f['severity']:8}] {f['signal']:8} {f['title']}\") for f in d['findings']]" /tmp/cr.json
```

---

## 3. Hand-tracing the orchestrator logic

If you want to check the merge logic rather than trust it, trace it against the three
envelopes produced above. The rules are in
`skills/audit-orchestrator/references/dedupe-rules.md`; walk them in order:

1. **Confidence ceiling** — find any finding with `confidence: "inferred"` or
   `"unverified"`. In the final report it must carry `cap_reason` and a
   `severity_before_cap` no higher than its ceiling.
   *Fixture check:* with `--render off`, the client-render finding is `inferred` and must
   appear as `high`, never `critical`.
2. **Identical `evidence_key`** — collect every `evidence_key` across the three
   envelopes. Any key appearing twice must appear **once** in the report, with
   `merged_from` naming both skills.
3. **`supersedes_hint`** — grep the envelopes for `supersedes_hint`. For each one whose
   target key also exists, that finding must be **absent** from `findings[]` and present
   inside the target's `related_effects`. The count must equal
   `audit_scope.findings_folded_into_root_causes`.
   *Fixture check:* on the broken site this is exactly 1 — the orientation finding folds
   into the client-render finding.
4. **Ordering** — `findings[]` must be sorted by severity, then mechanism stage
   (M1→M6), then scope (site→template→page). Ids are assigned after sorting, so
   `F-001` is always the thing to fix first.
5. **Counts** — `summary` must match the array. `validate_report.py` asserts this, but
   check it once by hand so you trust the validator.

```bash
# quick trace helper
python3 -c "
import json,sys
r=json.load(open('report.json'))
print('folded:', r['audit_scope']['findings_folded_into_root_causes'])
for f in r['findings']:
    print(f\"{f['id']} [{f['severity']:8}] {f['mechanism']} {f['signal']:12} {f['title'][:60]}\")
    if f.get('cap_reason'): print('    CAP:', f['cap_reason'])
    if f.get('related_effects'): print('    FOLDED:', [e['title'] for e in f['related_effects']])
    if f.get('merged_from'): print('    MERGED FROM:', f['merged_from'])
"
```

---

## 4. Testing against real sites

Fixtures prove the plumbing. Real sites prove generalisation.

**Pick deliberately, not randomly.** A useful test set has four shapes:

| Shape | What it stresses | What should happen |
|---|---|---|
| Server-rendered content site (docs, gov, university) | baseline | few findings; mostly proactive recommendations |
| Client-rendered SPA (app marketing site) | M3-S1 | render-gap finding at `critical`/`high` |
| Large e-commerce / publisher | false positives | **no** link-density or nav-bloat findings on category pages |
| Small local business | false positives | **no** sitemap or corroboration defects — small sites get the `low` proactive variants |

**Guardrails when testing live sites:** keep `--delay` at 0.4s or higher, keep
`--max-pages` modest, run each site once. The marketplace obeys robots.txt for its own
fetching, but courtesy is still yours to enforce.

### The review that matters

For every finding on a real site, ask the three questions each check is required to
answer. If you cannot answer all three, the check is not ready:

1. **Which mechanism does this test?** (`mechanism` field — M1–M6)
2. **What would falsify it?** (`falsified_by` field — could a reasonable site look like
   this and be fine?)
3. **Is the evidence checkable?** Can you re-verify it yourself from what the evidence
   string says — a status code, a matched rule, a word count, a parse error?

When a finding is wrong, the fix is almost always a **guard**, not a threshold. Ask why
the site legitimately looks that way, then encode that reason as a suppression condition
in the relevant `references/checks.md` and in the script.

---

## 5. Adding or changing a check

1. Write the guard **before** the detector. State what would falsify the flag first;
   if you cannot state it, the check is not ready to ship.
2. Give it a structured `evidence_key` (`SIGNAL:cause`) so dedupe works. Never rely on
   title text.
3. Put the reasoning in that skill's `references/checks.md` (mechanism, test, falsifier,
   severity rationale). Keep `SKILL.md` lean.
4. Add a positive case to `testing/make_fixtures.py`'s `bad` site **and** confirm the
   `good` site still produces zero critical/high findings.
5. Re-run `bash testing/smoke_test.sh`.

---

## 6. Pre-submission checklist

```bash
bash testing/smoke_test.sh                                    # all gates PASS
python3 -c "import json;m=json.load(open('marketplace.json')); \
  assert sum(1 for s in m['skills'] if s.get('entrypoint'))==1"
find . -name '__pycache__' -type d -exec rm -rf {} +          # no build artefacts
find . -name '.DS_Store' -delete
du -sh .                                                       # well under 50 MB
```

- [ ] `marketplace.json` at the root, exactly one `entrypoint: true`
- [ ] every listed skill folder has a `SKILL.md` with `name`, `description`,
      `allowed-tools`, and a `name` matching its folder
- [ ] `README.md` at the root
- [ ] no `__pycache__`, no `.pyc`, no fixture output committed
- [ ] scripts have no third-party imports (`grep -rn "^import\|^from" skills/*/scripts/*.py`)
- [ ] no hardcoded URLs or site-specific logic
- [ ] a fresh run against a site never seen before completes in under 5 minutes
