# claude-bughunter-trigger-budget

Measure, offline and byte-exactly, **what a skill bundle's install-time description truncation removes from every skill's activation surface** — before your agent silently stops auto-loading a skill on one harness but not another.

Upstream: https://github.com/elementalsouls/Claude-BugHunter

Why now: Claude-BugHunter's installer keeps the full `description` for Claude, Hermes and AntiGravity but rewrites the Codex/OpenCode copy under `~/.agents/skills` at 1024 chars. A skill's `description` *is* its auto-activation surface, so that rewrite is a behaviour change, not cosmetic — and nothing in the repo measures what is lost.

## What the upstream is, and what it does not do

[elementalsouls/Claude-BugHunter](https://github.com/elementalsouls/Claude-BugHunter) is a Claude Code skill bundle: 82 skills, 15 slash commands and 681 disclosed-report patterns. It already knows about the limit and enforces it:

- `docs/multi-harness.md` — "Codex is the strict parser … it hard-rejects descriptions > 1024 chars … so the installer **auto-truncates** any description > 1024 … only in the `~/.agents/skills` (Codex) copy — your `~/.claude`, `~/.hermes`, and `~/.gemini/config/skills` copies keep the full descriptions (incl. non-English trigger words)."
  <https://github.com/elementalsouls/Claude-BugHunter/blob/2cfe533df8efa2ced87c91299e1f62b88cb26108/docs/multi-harness.md#L63>
- `scripts/install.sh` L270-L274 — the truncation itself: `cut = inner[:LIMIT - 2].rsplit(" ", 1)[0].rstrip(" ,;:—-")`, then `cut + "…"`, applied only to lines matched by `^description:\s*(.*)$` in the **first 12 lines**.
  <https://github.com/elementalsouls/Claude-BugHunter/blob/2cfe533df8efa2ced87c91299e1f62b88cb26108/scripts/install.sh#L270-L274>
- `scripts/lint_skills.py` L174-L176 — flags over-length descriptions but treats the three router skills as a `DESC_LIMIT_GRANDFATHERED` warning only.
  <https://github.com/elementalsouls/Claude-BugHunter/blob/2cfe533df8efa2ced87c91299e1f62b88cb26108/scripts/lint_skills.py#L174-L176>
- Open issue [#82](https://github.com/elementalsouls/Claude-BugHunter/issues/82) — "Three SKILL.md descriptions run past the documented 1,024 character limit", still **open**, with a measured comment showing `bb-local-toolkit` has 91 distinct trigger terms needing 1,023 of the 1,024 characters.

Neither `lint_skills.py` nor `install.sh` answers the operational question: *for which skill, and which exact term, is activation lost — and how much headroom is left before it happens?* This tool answers exactly that, by replaying the installer pass instead of approximating it.

## What this tool does / does not do

Does:

- replays `install.sh`'s truncation **byte-exactly** (word-boundary cut, `rstrip`, `…`, first-12-lines reachability) over every `<skill>/SKILL.md`;
- reports per skill: description length, UTF-8 length, headroom, whether the installer truncates, and the **lost high-signal trigger terms with their character offsets** (so a claim like "it loses `GCP→AWS`" is checkable, not vibes);
- flags `over-limit-unreachable`: a description over the limit that the installer *cannot* rewrite (wrong line index or block scalar) — the strict harness then sees the over-length value, which is worse than a silent truncation;
- runs a **limit sensitivity sweep** (default 256/512/768/1024/2048): what breaks if another harness picks a smaller budget;
- rounds it off with exit codes and `--json`.

Does not: call the network, install anything, parse full YAML (only the same `^description:` line the installer sees, plus a declared-description scan for the unreachable case), judge whether a lost token is *important* to you, or modify any upstream file.

## Install

No dependencies beyond Python 3.8+.

```bash
git clone https://github.com/linmoxi33/claude-bughunter-trigger-budget.git
cd claude-bughunter-trigger-budget
python3 cb_trigger_budget.py --skills-dir examples/in/skills      # exit 1: demo bundle has issues
```

## Usage

```bash
# audit a real bundle clone (offline, zero writes)
git clone --depth=1 https://github.com/elementalsouls/Claude-BugHunter /tmp/bh
python3 cb_trigger_budget.py --skills-dir /tmp/bh/skills \
  --carve-out bug-bounty,bb-local-toolkit,osint-methodology

# machine-readable
python3 cb_trigger_budget.py --skills-dir /tmp/bh/skills --json | python3 -m json.tool

# what if a harness used a 512-char budget?
python3 cb_trigger_budget.py --skills-dir /tmp/bh/skills --limit 512 --sweep 256,512,1024
```

Flags: `--skills-dir` (default `skills`), `--limit` (default `1024`), `--min-token-len` (default `3`), `--carve-out` (comma-separated names documented as intentionally over-length), `--sweep`, `--json`, `--version`.

### Measured on the audited snapshot

Run on `elementalsouls/Claude-BugHunter` at commit `2cfe533df8efa2ced87c91299e1f62b88cb26108` (`--carve-out bug-bounty,bb-local-toolkit,osint-methodology`), **exit code 0** because every loss falls inside the documented carve-out:

| metric | value |
| --- | --- |
| SKILL.md scanned | 83 |
| over 1024 chars | 3 (`bb-local-toolkit` 1764, `bug-bounty` 1405, `osint-methodology` 1288) |
| installer-truncated | 3 |
| at-risk (headroom ≤ 32) | 3 — `hunt-ato` **9**, `hunt-llm-ai` 12, `hunt-auth-bypass` 23 |
| skills losing high-signal terms | 3 (all carve-outs), **23 terms** |
| min headroom | −740 chars (`bb-local-toolkit`) |
| max UTF-8 vs code-point divergence | 124 chars |
| sweep | limit 512 → **47 skills / 279 terms**; limit 256 → 75 / 708 |

The most concrete loss is not English: the whole `中文触发词：…` trigger block at the tail of `bb-local-toolkit` and `bug-bounty` is cut off the Codex/OpenCode copy, so those skills lose *all* of their Chinese activation surface on that harness while keeping it on Claude. `docs/multi-harness.md` says the copies keep the full descriptions "incl. non-English trigger words" — for the Codex copy, the installer's own truncation makes that false.

### `--json` output (excerpt, real run)

```json
{
  "tool": "claude-bughunter-trigger-budget",
  "limit": 1024,
  "summary": {
    "skills_scanned": 83,
    "over_limit": 3,
    "installer_truncated": 3,
    "at_risk": 3,
    "skills_losing_high_signal": 3,
    "total_lost_high_signal": 23,
    "min_headroom": -740,
    "min_headroom_skill": "bb-local-toolkit",
    "max_utf8_char_divergence": 124,
    "verdict": "pass"
  },
  "skills": [
    {
      "name": "hunt-ato", "status": "at-risk", "description_len": 1015,
      "headroom": 9, "installer_truncates": false, "lost": [],
      "issues": ["9 chars of headroom before install.sh starts rewriting this copy"]
    }
  ],
  "sweep": [{"limit": 512, "skills_losing_high_signal": 47, "total_lost_high_signal": 279}]
}
```

Full example: `examples/out/expected.json` + `examples/out/report.txt`, reproducible with the command in Install. Schema is fixed for the keys shown; `skills[]` always carries `status`, `description_len`, `utf8_len`, `headroom`, `installer_reachable`, `installer_truncates`, `carve_out`, `lost[]`, `issues[]`.

### Exit codes

| code | meaning |
| --- | --- |
| `0` | no *unintentional* token loss; carve-out losses are reported but do not escalate |
| `1` | at least one skill loses high-signal terms, or has an over-limit description the installer cannot reach, or has no readable description |
| `2` | usage or input error (missing `--skills-dir`, no `SKILL.md` found, bad `--limit`/`--sweep`) |

### Statuses

`ok`, `at-risk` (headroom ≤ 32), `truncated-no-loss`, `truncated-token-loss`, `over-limit-unreachable`, `carve-out`, `parse-error`.

## Tests

```bash
python3 tests/run.py     # 23 tests, 23 passed, 0 failed
```

10 unit tests over the truncation/parsing/token predicates and 13 behavioural ones (5 library-level, 8 driving the real CLI as a subprocess) (exit 0/1/2, `--json` parses, examples match `examples/out/expected.json`, byte-identical re-runs, `bin/` wrapper).

## Limits

- The truncation replay is pinned to the `install.sh` of commit `2cfe533…`; if upstream changes the algorithm this tool is wrong until updated.
- "High-signal" is a pure character-class predicate (arrow chain, `_ / \ + - .`, all-caps acronym, internal capital). It deliberately needs no dictionary, so it can over- or under-count borderline words — every lost token is printed with its offset so you can judge.
- Lengths are Python code points, matching `install.sh`'s own `len()`. A harness counting UTF-8 bytes or UTF-16 units would hit the cliff earlier; `utf8_len` is reported per skill and the divergence is summarised, but this tool does not claim how any harness counts.
- Only `description:` is audited; bodies, token budgets and rubric quality are out of scope.

## 与同类项目的差异

Searched before writing (2026-11-16): `gh search repos "agent skill description linter"` and `"SKILL.md frontmatter audit"` returned no repository; `gh search code "DESC_LIMIT_GRANDFATHERED"` returned only `lint_skills.py` in Claude-BugHunter and copies of it in forks (`Zyrexnn/Cybermes`, `Skobyn/talon`, `ranjithvettayam/method-red`) — all of them are the upstream linter itself, i.e. the thing this tool complements. Commands and hits are in `docs/similar.md`.

- vs. `scripts/lint_skills.py`: it prints one warning per over-length skill and stops. This tool quantifies the post-truncation state per skill, per term, with offsets, plus the headroom cliff and the limit sweep.
- vs. `scripts/install.sh`: it performs the truncation and prints a one-line byte count. This tool reports what the truncation costs on the surface the agent actually routes on.

## 本轮创新

B 档. The gap is one no upstream tool covers and that the upstream's own docs get wrong: the installer's Codex-copy truncation is an **activation-surface** change, and this is the first tool that quantifies it per skill and per term. Two findings come out of the real run that were not in issue #82 or the docs: (1) three skills sit within 23 characters of the cliff, `hunt-ato` within **9**; (2) the Codex copy loses the *entire* Chinese trigger block of the two aggregator skills, which `docs/multi-harness.md` explicitly claims is preserved. Both are reproducible from the committed snapshot with the command above.

## License

MIT (see `LICENSE`). The replayed algorithm is reimplemented from Claude-BugHunter's `scripts/install.sh` (MIT, © 2026 Sachin Sharma); no upstream file is copied into this repository.
