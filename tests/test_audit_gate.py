"""CI dependency-audit gate: exception expiry, severity threshold, allowlisting."""

from contextlib import redirect_stdout
from datetime import date
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("audit_gate", ROOT / ".github/scripts/audit_gate.py")
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

TODAY = date(2026, 10, 31)


def npm_report(*advisories, name="pkg"):
    return {
        "vulnerabilities": {name: {"via": ["parent-dep", *advisories]}},
        "metadata": {"vulnerabilities": {"total": len(advisories)}},
    }


def advisory(severity, ghsa="GHSA-aaaa-bbbb-cccc"):
    return {"source": 1, "title": "t", "severity": severity,
            "url": f"https://github.com/advisories/{ghsa}"}


class AuditGateTests(unittest.TestCase):
    def write_exceptions(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False, encoding="utf-8")
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def entry(self, ecosystem="pip", review_by="2026-12-01", **overrides):
        fields = {"id": "PYSEC-2026-1", "package": "example", "reason": "unreachable", **overrides}
        body = "".join(f'{key} = "{value}"\n' for key, value in fields.items())
        return f"[[{ecosystem}]]\n{body}review_by = {review_by}\n"

    def test_repository_exceptions_file_is_valid_and_unexpired(self):
        gate.load_exceptions(gate.EXCEPTIONS_FILE, "pip", date.today())
        gate.load_exceptions(gate.EXCEPTIONS_FILE, "npm", date.today())

    def test_missing_file_means_no_exceptions(self):
        self.assertEqual(gate.load_exceptions(ROOT / "does-not-exist.toml", "pip", TODAY), {})

    def test_exception_valid_through_review_date_and_expired_after(self):
        path = self.write_exceptions(self.entry(review_by="2026-10-31"))
        self.assertIn("PYSEC-2026-1", gate.load_exceptions(path, "pip", TODAY))
        with self.assertRaisesRegex(gate.GateError, "expired"):
            gate.load_exceptions(path, "pip", date(2026, 11, 1))

    def test_exception_without_justification_is_rejected(self):
        path = self.write_exceptions(self.entry(reason=" "))
        with self.assertRaisesRegex(gate.GateError, "missing"):
            gate.load_exceptions(path, "pip", TODAY)

    def test_pip_ignore_args(self):
        path = self.write_exceptions(self.entry() + self.entry(id="GHSA-zzzz-zzzz-zzzz"))
        args = gate.pip_ignore_args(gate.load_exceptions(path, "pip", TODAY))
        self.assertEqual(args, ["--ignore-vuln", "GHSA-zzzz-zzzz-zzzz", "--ignore-vuln", "PYSEC-2026-1"])
        self.assertEqual(gate.pip_ignore_args({}), [])

    def test_npm_fails_on_high_and_critical_only(self):
        for severity, should_fail in (("low", False), ("moderate", False),
                                      ("high", True), ("critical", True), ("HIGH", True)):
            with self.subTest(severity=severity):
                failures = gate.npm_failures(npm_report(advisory(severity)), {})
                self.assertEqual(bool(failures), should_fail)

    def test_npm_exception_suppresses_only_its_own_advisory(self):
        report = npm_report(advisory("high", "GHSA-aaaa-bbbb-cccc"), advisory("critical", "GHSA-dddd-eeee-ffff"))
        failures = gate.npm_failures(report, {"GHSA-aaaa-bbbb-cccc": {}})
        self.assertEqual(len(failures), 1)
        self.assertIn("GHSA-dddd-eeee-ffff", failures[0])

    def test_npm_clean_report_passes_and_error_report_fails_closed(self):
        self.assertEqual(gate.npm_failures({"vulnerabilities": {}}, {}), [])
        with self.assertRaises(gate.GateError):
            gate.npm_failures({"error": {"code": "ENOTFOUND"}}, {})

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            clean, dirty = Path(directory, "clean.json"), Path(directory, "dirty.json")
            clean.write_text(json.dumps({"vulnerabilities": {}, "metadata": {}}), encoding="utf-8")
            dirty.write_text(json.dumps(npm_report(advisory("critical"))), encoding="utf-8")
            self.assertEqual(gate.main(["npm-gate", str(clean)], TODAY), 0)
            self.assertEqual(gate.main(["npm-gate", str(dirty)], TODAY), 1)
            self.assertEqual(gate.main(["npm-gate", str(Path(directory, "missing.json"))], TODAY), 1)
            self.assertEqual(gate.main(["unknown"], TODAY), 2)


if __name__ == "__main__":
    unittest.main()
