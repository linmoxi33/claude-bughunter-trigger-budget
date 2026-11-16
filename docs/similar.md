# Phase D — prior-art search (2026-11-16, before writing any code)

All commands run with `gh` against api.github.com.

## 1

```bash
gh search repos "agent skill description linter" --limit=15 --json fullName,description,stargazersCount
```
Hits: **0**. No repository returns for this query.

## 2

```bash
gh search repos "SKILL.md frontmatter audit" --limit=10 --json fullName,stargazersCount
```
Hits: **0**.

## 3

```bash
gh search code "DESC_LIMIT_GRANDFATHERED" --limit=10 --json repository,path
```
Hits (5):

| repository | path |
| --- | --- |
| elementalsouls/Claude-BugHunter | scripts/lint_skills.py |
| Zyrexnn/Cybermes | knowledge/Claude-BugHunter/scripts/lint_skills.py |
| Skobyn/talon | scripts/lint_skills.py |
| ranjithvettayan/method-red | skill-libraries/Claude-BugHunter/scripts/lint_skills.py |
| frx-Abduljbbar-code/RunItUp23kFRX | scripts/lint_skills.py |

Every hit is upstream `lint_skills.py` itself or a vendored copy of it — i.e. the
component this tool complements, not a competitor. No repo combines the
installer-truncation replay with per-term activation-loss witnesses.

## 4

```bash
gh search code "inner[:LIMIT - 2].rsplit" --limit=10 --json repository,path
```
Hits: only `install.sh` / `install.ps1` in Claude-BugHunter and its forks (the
algorithm replayed here). Conclusion: no mature (>=500 star) equivalent exists;
no `## 与同类项目的差异` requirement triggered by a competitor, but the section is
included in README.md anyway for the neighbouring tools inside the upstream repo.
