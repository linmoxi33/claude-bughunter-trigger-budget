#!/usr/bin/env python3
"""claude-bughunter-trigger-budget - activation-surface budget audit for Agent Skills.

Replays, offline and byte-exactly, the frontmatter truncation pass that
Claude-BugHunter's ``scripts/install.sh`` applies to the Codex copy of a skill
bundle (``~/.agents/skills``), then measures what that pass removes from each
skill's *description* -- the only surface a coding agent auto-activates on.

Zero third-party dependencies. No network. Deterministic output.

Exit codes: 0 clean, 1 issues detected, 2 usage/input error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

VERSION = "1.0.0"

DEFAULT_LIMIT = 1024
DEFAULT_MIN_TOKEN_LEN = 3
DEFAULT_SWEEP = (256, 512, 768, 1024, 2048)
AT_RISK_HEADROOM = 32

# install.sh only rewrites a ``description:`` line found in the first 12 lines
# of the file (scripts/install.sh L263-265 in the audited snapshot).
INSTALLER_SCAN_LINES = 12

# install.sh L268-271: a value is treated as quoted when first char == last char
# and that char is a quote; the quotes are stripped before measuring.
QUOTE_CHARS = "\"'"

# install.sh L271 strips these from the right after cutting at a word boundary.
RSTRIP_CHARS = " ,;:—-\u2014"

# Token-level punctuation trimmed for set comparison (not part of the token).
TOKEN_STRIP = ".,;:!?()[]{}\"'`*\u2026\u2014\u2013"

STATUS_OK = "ok"
STATUS_AT_RISK = "at-risk"
STATUS_LOSS = "truncated-token-loss"
STATUS_NO_LOSS = "truncated-no-loss"
STATUS_UNREACHABLE = "over-limit-unreachable"
STATUS_CARVE_OUT = "carve-out"
STATUS_PARSE_ERROR = "parse-error"

# Statuses that make the audit fail (exit code 1). ``carve-out`` is deliberately
# excluded: lint_skills.py documents those three router skills as intentional.
FAILING_STATUSES = (STATUS_LOSS, STATUS_UNREACHABLE, STATUS_PARSE_ERROR)


def parse_frontmatter_description(text: str):
    """Return the installer-visible description, or None if it will not be seen.

    Mirrors install.sh exactly: only the first 12 lines are examined, and only a
    line matching ``^description:\\s*(.*)$`` is considered.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines[:INSTALLER_SCAN_LINES]):
        if not line.startswith("description:"):
            continue
        rest = line[len("description:"):]
        stripped = rest.lstrip(" \t")
        if rest and not stripped and "\t" not in rest:
            # "description:" with no value at all
            return {"line_index": i, "quoted": False, "block_scalar": False, "inner": ""}
        val = stripped.rstrip("\r")
        quoted = len(val) >= 2 and val[0] == val[-1] and val[0] in QUOTE_CHARS
        inner = val[1:-1] if quoted else val
        return {
            "line_index": i,
            "quoted": quoted,
            "block_scalar": val[:1] in (">", "|"),
            "inner": inner,
        }
    return None


def parse_declared_description(text: str):
    """Locate the description wherever it is declared in the frontmatter block.

    Used only to detect over-length descriptions that install.sh *cannot* reach
    (wrong line index, or a block scalar), because those are the ones the strict
    harness sees untruncated.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for j in range(1, min(len(lines), 500)):
        if lines[j].strip() == "---":
            end = j
            break
    if end is None:
        return None
    for i in range(1, end):
        line = lines[i]
        if not line.startswith("description:"):
            continue
        val = line[len("description:"):].lstrip(" \t").rstrip("\r")
        if val[:1] in (">", "|"):
            buf = [val[1:].strip()]
            for k in range(i + 1, end):
                if lines[k].strip() == "":
                    continue
                if lines[k][:1] in (" ", "\t"):
                    buf.append(lines[k].strip())
                else:
                    break
            return {"inner": " ".join(x for x in buf if x), "block": True, "line_index": i}
        quoted = len(val) >= 2 and val[0] == val[-1] and val[0] in QUOTE_CHARS
        return {"inner": val[1:-1] if quoted else val, "block": False, "line_index": i}
    return None


def installer_truncate(inner: str, limit: int):
    """Byte-exact replay of install.sh L270-272.

    Returns ``(result, changed)``.
    """
    if len(inner) <= limit:
        return inner, False
    cut = inner[: limit - 2].rsplit(" ", 1)[0].rstrip(RSTRIP_CHARS)
    return cut + "\u2026", True


def tokens_of(desc: str):
    """Whitespace tokenizer with documented edge trimming. Order preserved."""
    out = []
    for raw in desc.split():
        tok = raw.strip(TOKEN_STRIP)
        if tok:
            out.append(tok)
    return out


def is_high_signal(tok: str) -> bool:
    """Pure character-class predicate: is this token a trigger-bearing term?

    True when the token is an arrow chain, contains an identifier character
    (``_ / \\ + .``), is an all-caps acronym (>=2 chars), or has an internal
    capital (CamelCase). Plain sentence-initial English words ("Use", "The") are
    therefore not counted, with no hand-maintained dictionary.
    """
    if "\u2192" in tok or "->" in tok:
        return True
    if any(c in tok for c in "_/\\+-."):
        return True
    if len(tok) >= 2 and tok.isupper():
        return True
    return any(c.isupper() for c in tok[1:])


def analyze_skill(name: str, rel_path: str, text: str, limit: int,
                  min_token_len: int, carve_out) -> dict:
    rec = {
        "name": name,
        "path": rel_path,
        "status": STATUS_OK,
        "issues": [],
        "lost": [],
        "description_len": None,
        "utf8_len": None,
        "headroom": None,
        "installer_reachable": True,
        "installer_truncates": False,
        "carve_out": name in carve_out,
        "lost_count": 0,
        "lost_high_signal": 0,
        "truncated_description": None,
    }
    fm = parse_frontmatter_description(text)
    if fm is None:
        declared = parse_declared_description(text)
        if declared is not None:
            rec["description_len"] = len(declared["inner"])
            rec["utf8_len"] = len(declared["inner"].encode("utf-8"))
            rec["headroom"] = limit - len(declared["inner"])
            rec["installer_reachable"] = False
        if declared is not None and len(declared["inner"]) > limit:
            rec["status"] = STATUS_UNREACHABLE
            rec["issues"].append(
                "description is %d chars (> limit %d) but install.sh cannot reach it "
                "(line index %d%s), so the strict harness sees the over-length value"
                % (len(declared["inner"]), limit, declared["line_index"],
                   ", block scalar" if declared["block"] else "")
            )
            return rec
        rec["status"] = STATUS_PARSE_ERROR
        rec["issues"].append(
            "no 'description:' line in the first %d lines, so install.sh never "
            "rewrites it" % INSTALLER_SCAN_LINES
        )
        return rec

    inner = fm["inner"]
    rec["description_len"] = len(inner)
    rec["utf8_len"] = len(inner.encode("utf-8"))
    rec["headroom"] = limit - len(inner)
    rec["installer_reachable"] = fm["line_index"] < INSTALLER_SCAN_LINES and not fm["block_scalar"]

    result, changed = installer_truncate(inner, limit)
    rec["installer_truncates"] = changed

    if not fm["block_scalar"] and not changed and fm["inner"].strip() == "":
        rec["status"] = STATUS_PARSE_ERROR
        rec["issues"].append("empty description value")
        return rec

    if fm["block_scalar"]:
        rec["status"] = STATUS_UNREACHABLE if len(inner) > limit else STATUS_PARSE_ERROR
        rec["issues"].append(
            "description uses a block scalar (%r): install.sh measures only the "
            "inline value, so it cannot truncate it" % fm["inner"][:1]
        )
        return rec

    if len(inner) > limit and not rec["installer_reachable"]:
        rec["status"] = STATUS_UNREACHABLE
        rec["issues"].append(
            "description is %d chars (> limit %d) but sits at line index %d / is a "
            "block scalar, so install.sh cannot truncate it and the strict harness "
            "sees the over-length value" % (len(inner), limit, fm["line_index"])
        )
        rec["truncated_description"] = result
        return rec

    if changed:
        rec["truncated_description"] = result
        full = tokens_of(inner)
        kept = set(tokens_of(result))
        seen = set()
        lost = []
        for tok in full:
            if tok in seen or tok in kept:
                continue
            seen.add(tok)
            if len(tok) < min_token_len or tok.isdigit():
                continue
            lost.append({
                "token": tok,
                "offset": inner.find(tok),
                "high_signal": is_high_signal(tok),
            })
        lost.sort(key=lambda d: (d["offset"], d["token"]))
        rec["lost"] = lost
        rec["lost_count"] = len(lost)
        rec["lost_high_signal"] = sum(1 for d in lost if d["high_signal"])
        if rec["carve_out"]:
            rec["status"] = STATUS_CARVE_OUT
            rec["issues"].append(
                "documented carve-out (lint_skills.py DESC_LIMIT_GRANDFATHERED): "
                "truncated, %d high-signal trigger term(s) lost" % rec["lost_high_signal"]
            )
        elif rec["lost_high_signal"]:
            rec["status"] = STATUS_LOSS
            rec["issues"].append(
                "truncated at the word boundary; %d high-signal trigger term(s) "
                "reachable only in the full description" % rec["lost_high_signal"]
            )
        else:
            rec["status"] = STATUS_NO_LOSS
        return rec

    if 0 < rec["headroom"] <= AT_RISK_HEADROOM:
        rec["status"] = STATUS_AT_RISK
        rec["issues"].append(
            "%d chars of headroom before install.sh starts rewriting this copy" % rec["headroom"]
        )
    return rec


def audit(skills_dir: Path, limit: int, min_token_len: int, carve_out, sweep) -> dict:
    files = sorted(skills_dir.glob("**/SKILL.md"))
    skills = []
    inners = {}
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            skills.append({
                "name": p.parent.name,
                "path": str(p.relative_to(skills_dir)),
                "status": STATUS_PARSE_ERROR,
                "issues": ["unreadable file: %s" % exc],
                "lost": [],
                "description_len": None, "utf8_len": None, "headroom": None,
                "installer_reachable": False, "installer_truncates": False,
                "carve_out": False, "truncated_description": None,
            })
            continue
        skills.append(analyze_skill(
            p.parent.name, str(p.relative_to(skills_dir)), text, limit, min_token_len, carve_out
        ))
        fm = parse_frontmatter_description(text)
        inners[skills[-1]["path"]] = fm["inner"] if fm else None

    counts = {}
    for s in skills:
        counts[s["status"]] = counts.get(s["status"], 0) + 1

    losing = [s for s in skills if (s.get("lost_high_signal") or 0) > 0]
    at_risk = [s for s in skills if s["status"] == STATUS_AT_RISK]
    truncated = [s for s in skills if s["installer_truncates"]]
    over = [s for s in skills if (s["description_len"] or 0) > limit]
    min_headroom, min_headroom_skill = None, None
    for s in skills:
        if s["headroom"] is None:
            continue
        if min_headroom is None or s["headroom"] < min_headroom:
            min_headroom, min_headroom_skill = s["headroom"], s["name"]

    sweep_results = []
    for lim in sweep:
        c_losing, c_lost_high = 0, 0
        for s in skills:
            inner = inners.get(s["path"])
            if inner is None:
                continue
            res, ch = installer_truncate(inner, lim)
            if not ch:
                continue
            kept = set(tokens_of(res))
            high = {
                t for t in set(tokens_of(inner))
                if t not in kept and len(t) >= min_token_len and not t.isdigit()
                and is_high_signal(t)
            }
            if high:
                c_losing += 1
                c_lost_high += len(high)
        sweep_results.append({
            "limit": lim,
            "skills_losing_high_signal": c_losing,
            "total_lost_high_signal": c_lost_high,
        })

    max_utf8_divergence = 0
    for s in skills:
        if s["description_len"] is not None and s["utf8_len"] is not None:
            max_utf8_divergence = max(max_utf8_divergence, s["utf8_len"] - s["description_len"])

    verdict = "issues" if any(s["status"] in FAILING_STATUSES for s in skills) else "pass"
    return {
        "tool": "claude-bughunter-trigger-budget",
        "version": VERSION,
        "skills_dir": str(skills_dir),
        "limit": limit,
        "min_token_len": min_token_len,
        "at_risk_headroom": AT_RISK_HEADROOM,
        "carve_out": sorted(carve_out),
        "summary": {
            "skills_scanned": len(skills),
            "status_counts": counts,
            "over_limit": len(over),
            "installer_truncated": len(truncated),
            "at_risk": len(at_risk),
            "skills_losing_high_signal": len(losing),
            "total_lost_high_signal": sum(s.get("lost_high_signal") or 0 for s in losing),
            "carve_out_skills_losing": sum(1 for s in losing if s["carve_out"]),
            "unintentional_skills_losing": sum(1 for s in losing if not s["carve_out"]),
            "min_headroom": min_headroom,
            "min_headroom_skill": min_headroom_skill,
            "max_utf8_char_divergence": max_utf8_divergence,
            "verdict": verdict,
        },
        "skills": skills,
        "sweep": sweep_results,
    }


def render_text(report: dict) -> str:
    s = report["summary"]
    out = []
    out.append("claude-bughunter-trigger-budget v%s" % report["version"])
    out.append("skills dir : %s" % report["skills_dir"])
    out.append("limit      : %d chars (install.sh Codex copy)" % report["limit"])
    out.append("scanned    : %d SKILL.md" % s["skills_scanned"])
    out.append("over limit : %d   installer-truncated: %d   at-risk: %d"
               % (s["over_limit"], s["installer_truncated"], s["at_risk"]))
    out.append("token loss : %d skill(s), %d high-signal trigger term(s)"
               % (s["skills_losing_high_signal"], s["total_lost_high_signal"]))
    if s["min_headroom_skill"]:
        out.append("min headroom: %d chars (%s)" % (s["min_headroom"], s["min_headroom_skill"]))
    out.append("utf8 vs codepoint divergence (max): %d" % s["max_utf8_char_divergence"])
    out.append("")
    for sk in report["skills"]:
        if sk["status"] in (STATUS_OK,):
            continue
        out.append("[%s] %s (%s)" % (sk["status"], sk["name"], sk["path"]))
        for issue in sk["issues"]:
            out.append("    - %s" % issue)
        for d in sk["lost"][:6]:
            flag = "high" if d["high_signal"] else "word"
            out.append("      lost@%d %s %r" % (d["offset"], flag, d["token"]))
        if len(sk["lost"]) > 6:
            out.append("      ... %d more lost token(s)" % (len(sk["lost"]) - 6))
    out.append("")
    out.append("sweep (limit -> skills losing high-signal terms / terms lost):")
    for row in report["sweep"]:
        out.append("  %5d -> %d / %d" % (row["limit"], row["skills_losing_high_signal"],
                                         row["total_lost_high_signal"]))
    out.append("")
    out.append("verdict: %s" % s["verdict"])
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-bughunter-trigger-budget",
        description="Audit what a skill bundle's install-time description truncation "
                    "removes from each skill's activation surface.",
    )
    p.add_argument("--skills-dir", default="skills",
                   help="directory containing <skill>/SKILL.md (default: skills)")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help="frontmatter description char limit (default: 1024)")
    p.add_argument("--min-token-len", type=int, default=DEFAULT_MIN_TOKEN_LEN,
                   help="ignore lost tokens shorter than this (default: 3)")
    p.add_argument("--carve-out", default="",
                   help="comma-separated skill names documented as intentional over-length")
    p.add_argument("--sweep", default=",".join(str(x) for x in DEFAULT_SWEEP),
                   help="comma-separated limits for the sensitivity sweep")
    p.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.limit < 4:
        print("error: --limit must be >= 4 (install.sh slices limit-2)", file=sys.stderr)
        return 2
    if args.min_token_len < 1:
        print("error: --min-token-len must be >= 1", file=sys.stderr)
        return 2

    skills_dir = Path(args.skills_dir)
    if not skills_dir.is_dir():
        print("error: skills dir not found: %s" % skills_dir, file=sys.stderr)
        return 2
    if not any(skills_dir.glob("**/SKILL.md")):
        print("error: no SKILL.md found under %s" % skills_dir, file=sys.stderr)
        return 2

    try:
        sweep = tuple(sorted({int(x) for x in args.sweep.split(",") if x.strip()}))
    except ValueError:
        print("error: --sweep must be a comma-separated list of integers", file=sys.stderr)
        return 2
    if not sweep or any(l < 4 for l in sweep):
        print("error: sweep limits must be integers >= 4", file=sys.stderr)
        return 2

    carve_out = {x.strip() for x in args.carve_out.split(",") if x.strip()}
    report = audit(skills_dir, args.limit, args.min_token_len, carve_out, sweep)

    if args.json:
        import json
        print(json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False))
    else:
        print(render_text(report))

    return 1 if report["summary"]["verdict"] == "issues" else 0


if __name__ == "__main__":
    sys.exit(main())
