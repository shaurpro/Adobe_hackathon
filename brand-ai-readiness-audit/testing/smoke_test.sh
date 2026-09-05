#!/usr/bin/env bash
# smoke_test.sh — end-to-end check of the marketplace against local fixtures.
#
# Verifies three things, in the order they matter:
#   1. the manifest is well-formed and every skill folder is spec-compliant
#   2. the broken fixture produces the expected findings  (detection)
#   3. the clean fixture produces NO critical/high findings (false positives)
#
# Usage:  bash testing/smoke_test.sh
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }

echo "== 1. Marketplace structure =="
python3 - "$ROOT" <<'PY'
import json, os, re, sys
root = sys.argv[1]
m = json.load(open(os.path.join(root, "marketplace.json")))
eps = [s for s in m["skills"] if s.get("entrypoint")]
print("  manifest parses: ok")
print(f"  exactly one entrypoint: {'ok' if len(eps)==1 else 'FAIL'} ({[e['id'] for e in eps]})")
for s in m["skills"]:
    p = os.path.join(root, s["path"], "SKILL.md")
    if not os.path.exists(p):
        print(f"  {s['id']}: FAIL - no SKILL.md"); continue
    t = open(p, encoding="utf-8").read()
    fm = re.match(r"^---\n(.*?)\n---\n", t, re.S)
    if not fm:
        print(f"  {s['id']}: FAIL - no YAML frontmatter"); continue
    body = fm.group(1)
    name = re.search(r"^name:\s*(\S+)", body, re.M)
    has = all(re.search(rf"^{k}:", body, re.M) for k in ("name", "description", "allowed-tools"))
    match = name and name.group(1) == s["id"]
    print(f"  {s['id']}: frontmatter={'ok' if has else 'FAIL'} "
          f"name-matches-folder={'ok' if match else 'FAIL'} "
          f"lines={len(t.splitlines())}")
PY

echo
echo "== 2. Build and serve fixtures =="
python3 "$ROOT/testing/make_fixtures.py" --dir "$WORK/fixtures" >/dev/null
(cd "$WORK/fixtures/bad"  && python3 -m http.server 8800 >/dev/null 2>&1 &) 
(cd "$WORK/fixtures/good" && python3 -m http.server 8801 >/dev/null 2>&1 &)
sleep 2
curl -sf -o /dev/null http://127.0.0.1:8800/ && ok "broken fixture serving on :8800" || bad "broken fixture not serving"
curl -sf -o /dev/null http://127.0.0.1:8801/ && ok "clean fixture serving on :8801"  || bad "clean fixture not serving"

echo
echo "== 3. Detection run (broken fixture) =="
python3 "$ROOT/skills/audit-orchestrator/scripts/orchestrate.py" \
    --url http://127.0.0.1:8800/ --max-pages 12 --delay 0.05 --render off \
    --out "$WORK/report_bad.json" 2>&1 | sed 's/^/  /'
python3 "$ROOT/skills/audit-orchestrator/scripts/validate_report.py" "$WORK/report_bad.json" \
    >/dev/null 2>&1 && ok "report is schema-valid" || bad "report failed schema validation"

python3 - "$WORK/report_bad.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
sigs = {f.get("signal") for f in r["findings"]}
# Defects deliberately planted in the broken fixture.
expect = {"M1-S1": "blocked AI crawlers",
          "M1-S2": "broken internal link",
          "M3-S1": "client-rendered shell",
          "M3-S2": "PDF-only pricing / missing alt",
          "M3-S3": "invalid + missing JSON-LD"}
missed = [f"{k} ({v})" for k, v in expect.items() if k not in sigs]
print(f"  detected signals: {sorted(sigs)}")
if missed:
    print("  FAIL  missed planted defects: " + "; ".join(missed)); sys.exit(1)
print(f"  PASS  all {len(expect)} planted defect classes detected")
PY

echo
echo "== 4. False-positive control (clean fixture) =="
python3 "$ROOT/skills/audit-orchestrator/scripts/orchestrate.py" \
    --url http://127.0.0.1:8801/ --max-pages 12 --delay 0.05 --render off \
    --out "$WORK/report_good.json" 2>&1 | sed 's/^/  /'
python3 - "$WORK/report_good.json" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
s = r["summary"]
bad = [f for f in r["findings"] if f["severity"] in ("critical", "high")]
print(f"  summary: {s['critical']} critical, {s['high']} high, "
      f"{s['medium']} medium, {s['low']} low")
if bad:
    for f in bad:
        print(f"  FAIL  false positive: [{f['severity']}] {f['title']}")
    sys.exit(1)
print("  PASS  zero critical/high findings on the clean site")
PY

echo
echo "== 5. Determinism =="
python3 "$ROOT/skills/audit-orchestrator/scripts/orchestrate.py" \
    --url http://127.0.0.1:8801/ --max-pages 12 --delay 0.05 --render off \
    --out "$WORK/report_good2.json" >/dev/null 2>&1
python3 - "$WORK/report_good.json" "$WORK/report_good2.json" <<'PY'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
ka = [(f["id"], f["title"], f["severity"]) for f in a["findings"]]
kb = [(f["id"], f["title"], f["severity"]) for f in b["findings"]]
print("  PASS  identical findings across two runs" if ka == kb
      else "  FAIL  runs differ (non-deterministic)")
sys.exit(0 if ka == kb else 1)
PY

pkill -f "http.server 8800" 2>/dev/null
pkill -f "http.server 8801" 2>/dev/null
echo
echo "Artifacts in $WORK"
