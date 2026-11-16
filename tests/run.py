#!/usr/bin/env python3
"""Test suite for claude-bughunter-trigger-budget.

Run: python3 tests/run.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = ROOT / "cb_trigger_budget.py"
CLI = [sys.executable, str(MOD_PATH)]
EXAMPLES = ROOT / "examples" / "in" / "skills"

spec = importlib.util.spec_from_file_location("ctb", MOD_PATH)
ctb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctb)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    if not cond:
        print("FAIL: %s %s" % (name, detail))


def write_skill(root, name, lines):
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def mk(text):
    return "---\nname: t\ndescription: " + text + "\nlicense: MIT\n---\n\n# t\n"


# --- unit: truncation replay -------------------------------------------------
def test_truncate_under_limit():
    out, ch = ctb.installer_truncate("abc", 1024)
    check("truncate_under_limit", out == "abc" and ch is False, repr(out))


def test_truncate_exactly_limit():
    s = "a" * 1024
    out, ch = ctb.installer_truncate(s, 1024)
    check("truncate_exactly_limit", out == s and ch is False, "len=%d" % len(out))


def test_truncate_word_boundary_and_ellipsis():
    s = "x" * 1015 + " ENDWORD tail"
    out, ch = ctb.installer_truncate(s, 1024)
    check("truncate_word_boundary",
          ch is True and out.endswith("\u2026") and "ENDWORD" not in out
          and len(out) <= 1024,
          "len=%d tail=%r" % (len(out), out[-12:]))


def test_leading_chars_are_kept():
    s = "KEEPME " + "z" * 2000
    out, _ = ctb.installer_truncate(s, 1024)
    check("leading_chars_kept", out.startswith("KEEPME"), out[:10])


# --- unit: frontmatter parsing ----------------------------------------------
def test_installer_only_scans_first_12_lines():
    lines = ["---", "name: late"] + ["  k: v"] * 10 + ['description: "abc"', "---", ""]
    got = ctb.parse_frontmatter_description("\n".join(lines))
    check("installer_skips_late_description", got is None, repr(got))


def test_declared_parser_finds_late_description():
    lines = ["---", "name: late"] + ["  k: v"] * 10 + ['description: "abc"', "---", ""]
    got = ctb.parse_declared_description("\n".join(lines))
    check("declared_parser_finds_late", got and got["inner"] == "abc", repr(got))


def test_quoted_description_is_unquoted():
    got = ctb.parse_frontmatter_description(mk('"hello world"'))
    check("quoted_unquoted", got and got["inner"] == "hello world", repr(got))


def test_block_scalar_detected():
    got = ctb.parse_frontmatter_description(mk(">"))
    check("block_scalar_detected", got and got["block_scalar"] is True, repr(got))


# --- unit: token semantics ---------------------------------------------------
def test_high_signal_predicate():
    hi = ["WPScanProxy", "GCP\u2192AWS", "smtp_open_relay", "kube-hunter", "BBOT", "a.php"]
    lo = ["Use", "The", "recon", "and", "with"]
    check("high_signal_predicate",
          all(ctb.is_high_signal(t) for t in hi) and not any(ctb.is_high_signal(t) for t in lo),
          "hi=%s lo=%s" % ([t for t in hi if not ctb.is_high_signal(t)],
                           [t for t in lo if ctb.is_high_signal(t)]))


def test_token_strip():
    check("token_strip", ctb.tokens_of("a, (b) c.") == ["a", "b", "c"],
          repr(ctb.tokens_of("a, (b) c.")))


# --- behavioural: library-level analysis ------------------------------------
def test_at_risk_borderline():
    desc = "w" * (1024 - 9)
    with tempfile.TemporaryDirectory() as td:
        write_skill(td, "border", ["---", "name: border", "description: " + desc, "---", ""])
        rep = ctb.audit(Path(td), 1024, 3, set(), (1024,))
        check("at_risk_borderline",
              rep["skills"][0]["status"] == ctb.STATUS_AT_RISK
              and rep["skills"][0]["headroom"] == 9,
              rep["skills"][0]["status"])


def test_carve_out_suppresses_verdict():
    desc = "x" * 1000 + ("tail " * 20) + "WPScanProxy"
    with tempfile.TemporaryDirectory() as td:
        write_skill(td, "router", ["---", "name: router", "description: " + desc, "---", ""])
        plain = ctb.audit(Path(td), 1024, 3, set(), (1024,))
        carved = ctb.audit(Path(td), 1024, 3, {"router"}, (1024,))
        check("carve_out_suppresses_verdict",
              plain["summary"]["verdict"] == "issues"
              and carved["summary"]["verdict"] == "pass"
              and carved["skills"][0]["status"] == ctb.STATUS_CARVE_OUT
              and carved["skills"][0]["lost_high_signal"] >= 1,
              "%s/%s" % (plain["summary"]["verdict"], carved["summary"]["verdict"]))


def test_loss_witness_has_offset_and_token():
    desc = "y" * 1020 + " KubeHunter"
    with tempfile.TemporaryDirectory() as td:
        write_skill(td, "wit", ["---", "name: wit", "description: " + desc, "---", ""])
        rep = ctb.audit(Path(td), 1024, 3, set(), (1024,))
        lost = rep["skills"][0]["lost"]
        check("loss_witness_offset",
              any(d["token"] == "KubeHunter" and d["offset"] == 1021 and d["high_signal"]
                  for d in lost),
              repr(lost[:3]))


def test_unreachable_over_limit_is_an_issue():
    lines = ["---", "name: late"] + ["  k: v"] * 10 + [
        "description: " + "z" * 1300, "---", ""]
    with tempfile.TemporaryDirectory() as td:
        write_skill(td, "late", lines)
        rep = ctb.audit(Path(td), 1024, 3, set(), (1024,))
        check("unreachable_over_limit",
              rep["skills"][0]["status"] == ctb.STATUS_UNREACHABLE
              and rep["summary"]["verdict"] == "issues",
              rep["skills"][0]["status"])


def test_sweep_monotonic():
    with tempfile.TemporaryDirectory() as td:
        for i in range(3):
            write_skill(td, "s%d" % i,
                        ["---", "name: s%d" % i, "description: " + "q" * (900 + i * 40),
                         "---", ""])
        rep = ctb.audit(Path(td), 1024, 3, set(), (256, 512, 1024))
        counts = [r["skills_losing_high_signal"] for r in rep["sweep"]]
        check("sweep_monotonic", counts == sorted(counts, reverse=True), repr(counts))


# --- behavioural: CLI exit codes, --json, clean run -------------------------
def run_cli(args, cwd=None):
    return subprocess.run(CLI + args, capture_output=True, text=True, cwd=cwd)


def test_cli_exit_1_on_examples():
    p = run_cli(["--skills-dir", str(EXAMPLES)])
    check("cli_exit_1_on_examples", p.returncode == 1, "rc=%d" % p.returncode)


def test_cli_json_is_parseable():
    p = run_cli(["--skills-dir", str(EXAMPLES), "--json"])
    ok = False
    try:
        d = json.loads(p.stdout)
        ok = d["tool"] == "claude-bughunter-trigger-budget" and d["summary"]["skills_scanned"] == 4
    except Exception as exc:  # noqa: BLE001
        print("json error", exc)
    check("cli_json_parseable", ok, p.stdout[:120])


def test_cli_exit_0_on_clean_bundle():
    with tempfile.TemporaryDirectory() as td:
        write_skill(td, "clean", ["---", "name: clean", "description: short and fine",
                                  "---", ""])
        p = run_cli(["--skills-dir", td, "--json"])
        d = json.loads(p.stdout)
        check("cli_exit_0_clean", p.returncode == 0 and d["summary"]["verdict"] == "pass",
              "rc=%d" % p.returncode)


def test_cli_exit_2_missing_dir():
    p = run_cli(["--skills-dir", "/nonexistent-ctb-dir"])
    check("cli_exit_2_missing_dir", p.returncode == 2 and "error" in p.stderr,
          "rc=%d" % p.returncode)


def test_cli_exit_2_bad_limit():
    p = run_cli(["--skills-dir", str(EXAMPLES), "--limit", "3"])
    check("cli_exit_2_bad_limit", p.returncode == 2, "rc=%d" % p.returncode)


def test_cli_examples_matches_expected_json():
    expected = json.loads((ROOT / "examples" / "out" / "expected.json").read_text(encoding="utf-8"))
    p = run_cli(["--skills-dir", str(EXAMPLES), "--json"])
    got = json.loads(p.stdout)
    check("examples_match_expected",
          got["summary"] == expected["summary"] and got["skills"] == expected["skills"],
          "summary diff")


def test_cli_is_deterministic():
    a = run_cli(["--skills-dir", str(EXAMPLES), "--json"]).stdout
    b = run_cli(["--skills-dir", str(EXAMPLES), "--json"]).stdout
    check("cli_deterministic", a == b, "outputs differ")


def test_bin_wrapper_runs():
    wrapper = ROOT / "bin" / "claude-bughunter-trigger-budget"
    p = subprocess.run([str(wrapper), "--skills-dir", str(EXAMPLES), "--limit", "512",
                        "--json"], capture_output=True, text=True)
    ok = p.returncode == 1
    try:
        json.loads(p.stdout)
    except Exception:  # noqa: BLE001
        ok = False
    check("bin_wrapper_runs", ok, "rc=%d" % p.returncode)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("%d tests, %d passed, %d failed" % (len(RESULTS), passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
