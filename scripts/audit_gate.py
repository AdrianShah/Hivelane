"""Fail an audit run only on actionable findings (.github/workflows/audit.yml).

    python scripts/audit_gate.py pip  pip-audit.json   # pip-audit -f json output
    python scripts/audit_gate.py npm  npm-audit.json   # npm audit --json output

Actionable means there is something to upgrade to: a pip-audit finding with
fix versions, or an npm advisory of high/critical severity whose fixAvailable
is not false. Everything else is printed as a GitHub warning and passes. A
missing or unreadable report fails, so a crashed audit never looks clean.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

NPM_LEVELS = {"high", "critical"}


def pip_findings(report: dict) -> tuple[list[str], list[str]]:
    fixable: list[str] = []
    unfixable: list[str] = []
    for dep in report.get("dependencies", []):
        for vuln in dep.get("vulns", []):
            line = f"{dep.get('name')} {dep.get('version')}: {vuln.get('id')}"
            fixes = vuln.get("fix_versions") or []
            if fixes:
                fixable.append(f"{line} (fixed in {', '.join(fixes)})")
            else:
                unfixable.append(f"{line} (no fixed release yet)")
    return fixable, unfixable


def npm_findings(report: dict) -> tuple[list[str], list[str]]:
    fixable: list[str] = []
    unfixable: list[str] = []
    for name, vuln in (report.get("vulnerabilities") or {}).items():
        severity = vuln.get("severity", "")
        if severity not in NPM_LEVELS:
            continue
        line = f"{name} ({severity})"
        if vuln.get("fixAvailable") is False:
            unfixable.append(f"{line}: no fix available yet")
        else:
            fixable.append(f"{line}: run npm audit fix or bump the dependency")
    return fixable, unfixable


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"pip", "npm"}:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(argv[2])
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"::error title=Audit report unreadable::{path}: {exc}")
        return 1
    if not isinstance(report, dict):
        print(f"::error title=Audit report unreadable::{path} is not a JSON object")
        return 1
    fixable, unfixable = (pip_findings if argv[1] == "pip" else npm_findings)(report)
    for line in unfixable:
        print(f"::warning title=Known vulnerability, no fix yet::{line}")
    for line in fixable:
        print(f"::error title=Vulnerability with a fix::{line}")
    print(f"{len(fixable)} actionable, {len(unfixable)} without a fix")
    return 1 if fixable else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
