"""Dependency-audit gate for CI (AST-008).

Exceptions live in .github/audit-exceptions.toml. Every entry needs an id, the
package, a justification and a review_by date; an entry past its review date
fails the build so exceptions cannot silently become permanent.

Usage:
  python .github/scripts/audit_gate.py pip-ignore-args
      Print pip-audit "--ignore-vuln ID" arguments for the pip exceptions.
  python .github/scripts/audit_gate.py npm-gate npm-audit.json
      Fail on any high/critical npm advisory that is not an active exception.

Policy: pip-audit's advisory data carries no reliable severity, so every
unexcepted Python advisory fails. npm fails on high and critical only.
"""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sys
import tomllib

EXCEPTIONS_FILE = Path(__file__).resolve().parents[1] / "audit-exceptions.toml"
NPM_FAILING_SEVERITIES = {"high", "critical"}
REQUIRED_FIELDS = ("id", "package", "reason", "review_by")


class GateError(Exception):
    pass


def load_exceptions(path: Path, ecosystem: str, today: date) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    active: dict[str, dict] = {}
    for entry in data.get(ecosystem, []):
        missing = [field for field in REQUIRED_FIELDS if not str(entry.get(field, "")).strip()]
        if missing:
            raise GateError(f"{ecosystem} exception {entry.get('id', '?')} is missing {missing}")
        review_by = entry["review_by"]
        if not isinstance(review_by, date):
            review_by = date.fromisoformat(str(review_by))
        if review_by < today:
            raise GateError(
                f"{ecosystem} exception {entry['id']} ({entry['package']}) expired on "
                f"{review_by.isoformat()}; re-review it or remove it"
            )
        active[entry["id"]] = entry
    return active


def pip_ignore_args(exceptions: dict[str, dict]) -> list[str]:
    args: list[str] = []
    for advisory_id in sorted(exceptions):
        args += ["--ignore-vuln", advisory_id]
    return args


def _advisory_ids(advisory: dict) -> set[str]:
    ids = set()
    url = str(advisory.get("url", ""))
    if "/advisories/" in url:
        ids.add(url.rstrip("/").rsplit("/", 1)[-1])
    if advisory.get("source") is not None:
        ids.add(str(advisory["source"]))
    return ids


def npm_failures(report: dict, exceptions: dict[str, dict]) -> list[str]:
    if "vulnerabilities" not in report:
        # npm audit returns {"error": ...} when the registry cannot be reached.
        raise GateError(f"npm audit produced no vulnerability report: {report.get('error', report)!r}")
    failures: list[str] = []
    for name, vuln in sorted(report["vulnerabilities"].items()):
        for advisory in vuln.get("via", []):
            if not isinstance(advisory, dict):
                continue  # A string names the dependency that carries the advisory.
            severity = str(advisory.get("severity", "")).lower()
            if severity not in NPM_FAILING_SEVERITIES:
                continue
            if _advisory_ids(advisory) & set(exceptions):
                continue
            failures.append(f"{name}: {severity} {advisory.get('title', '')} {advisory.get('url', '')}".strip())
    return failures


def main(argv: list[str], today: date | None = None) -> int:
    today = today or date.today()
    try:
        if argv[:1] == ["pip-ignore-args"]:
            print(" ".join(pip_ignore_args(load_exceptions(EXCEPTIONS_FILE, "pip", today))))
            return 0
        if argv[:1] == ["npm-gate"] and len(argv) == 2:
            report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
            failures = npm_failures(report, load_exceptions(EXCEPTIONS_FILE, "npm", today))
            for failure in failures:
                print(f"::error::npm advisory {failure}")
            counts = report.get("metadata", {}).get("vulnerabilities", {})
            print(f"npm audit totals: {counts}; unexcepted high/critical: {len(failures)}")
            return 1 if failures else 0
    except (GateError, ValueError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"::error::{exc}")
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
