#!/usr/bin/env python3
"""
validate_report.py — assert a report satisfies the required schema before it is
handed to anyone.

The brief defines a schema floor. This checks the floor is met, that severity
counts are internally consistent, that ids are unique, and that every finding
carries usable evidence and an action. Extensions beyond the floor are allowed
and are not flagged.

Usage:
  python3 validate_report.py report.json
Exit code 0 = valid, 1 = invalid (errors printed to stderr).
"""

import json
import re
import sys

SEVERITIES = {"critical", "high", "medium", "low"}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})$")


def validate(report):
    errors, warnings = [], []

    for key in ("site", "audited_at", "summary", "findings"):
        if key not in report:
            errors.append(f"missing required top-level key: '{key}'")
    if errors:
        return errors, warnings

    if not isinstance(report["site"], str) or not report["site"].strip():
        errors.append("'site' must be a non-empty string")
    if not ISO.match(str(report["audited_at"])):
        errors.append(f"'audited_at' is not ISO8601: {report['audited_at']!r}")

    summary = report["summary"]
    for key in ("total_findings", "critical", "high", "medium"):
        if key not in summary:
            errors.append(f"summary is missing required key: '{key}'")
        elif not isinstance(summary[key], int):
            errors.append(f"summary.{key} must be an integer")

    findings = report["findings"]
    if not isinstance(findings, list):
        errors.append("'findings' must be a list")
        return errors, warnings

    seen_ids = set()
    for i, f in enumerate(findings):
        where = f"findings[{i}]"
        for key in ("id", "title", "severity", "evidence", "suggested_action"):
            if key not in f or f[key] in (None, "", {}):
                errors.append(f"{where} missing required field: '{key}'")
        if f.get("severity") not in SEVERITIES:
            errors.append(f"{where}.severity invalid: {f.get('severity')!r}")
        fid = f.get("id")
        if fid in seen_ids:
            errors.append(f"{where}.id duplicated: {fid!r}")
        seen_ids.add(fid)
        action = f.get("suggested_action") or {}
        if not isinstance(action, dict):
            errors.append(f"{where}.suggested_action must be an object")
        else:
            if not action.get("summary"):
                errors.append(f"{where}.suggested_action.summary is empty")
            if not action.get("priority"):
                errors.append(f"{where}.suggested_action.priority is empty")
        ev = f.get("evidence") or ""
        if len(ev) < 40:
            warnings.append(f"{where}.evidence is very short; evidence should let a "
                            f"reader verify the finding independently")
        if not re.search(r"\d", ev) and f.get("finding_type") != "proactive":
            warnings.append(f"{where}.evidence contains no counts, codes or measured "
                            f"values — is it an observation or an assertion?")

    for j, r in enumerate(report.get("proactive_recommendations", [])):
        where = f"proactive_recommendations[{j}]"
        for key in ("id", "title", "suggested_action"):
            if not r.get(key):
                errors.append(f"{where} missing required field: '{key}'")
        rid = r.get("id")
        if rid in seen_ids:
            errors.append(f"{where}.id collides with a finding id: {rid!r}")
        seen_ids.add(rid)

    if summary.get("total_findings") != len(findings):
        errors.append(f"summary.total_findings ({summary.get('total_findings')}) != "
                      f"len(findings) ({len(findings)})")
    for sev in ("critical", "high", "medium", "low"):
        if sev in summary:
            actual = sum(1 for f in findings if f.get("severity") == sev)
            if summary[sev] != actual:
                errors.append(f"summary.{sev} ({summary[sev]}) != actual count ({actual})")

    return errors, warnings


def main():
    if len(sys.argv) != 2:
        print("usage: validate_report.py <report.json>", file=sys.stderr)
        return 2
    try:
        with open(sys.argv[1], "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"INVALID: cannot read report: {e}", file=sys.stderr)
        return 1

    errors, warnings = validate(report)
    for w in warnings:
        print(f"  warning: {w}", file=sys.stderr)
    if errors:
        print(f"INVALID: {len(errors)} error(s)", file=sys.stderr)
        for e in errors:
            print(f"  error: {e}", file=sys.stderr)
        return 1
    print(f"VALID: {len(report['findings'])} finding(s), "
          f"{len(report.get('proactive_recommendations', []))} recommendation(s), "
          f"schema floor satisfied"
          + (f", {len(warnings)} warning(s)" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
