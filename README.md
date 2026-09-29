# opencode-dev-kit

Reusable OpenCode agents and skills for planning, investigation, implementation, review, and operational verification.

## Operating principles

- Keep planning, research, observation, implementation, review, and coordination separate.
- Read before changing state and use the least-powerful suitable tool.
- Safety, explicit scope, and verified evidence take priority over speed; failures, missing credentials, inaccessible targets, and absent results are not success.
- Distinguish facts, evidence-backed inferences, assumptions, hypotheses, and unknowns.
- Give delegated work an objective, scope, constraints, evidence requirements, and stop conditions. Run independent work in parallel, serialize dependent or overlapping changes, and preserve a rollback path where applicable.
- Keep one bounded objective per session and pass concise summaries rather than raw logs or transcripts.

## Install

`install.py` links bundled agents and skills, plus selected plugins, into a project's `.opencode/` directory. Bundled agents and skills are selected by default; this repository has no bundled `plugins/` directory, so plugins require an explicit source.

```shell
./install.py
./install.py --target /path/to/project
./install.py --dry-run --target /path/to/project
```

`--target` accepts a project directory or an explicit `.opencode` directory. The installer creates relative links for bundled content and links remote content from `.opencode/.install-cache/`. A correct existing link is retained.

### External sources

The repeatable singular/plural flags select one item or a collection:

```text
--skill SOURCE       --skills [SOURCE]
--agent SOURCE       --agents [SOURCE]
--plugin SOURCE      --plugins [SOURCE]
```

Local paths, HTTP(S) URLs, and GitHub repository-tree URLs are supported. A no-value plural flag explicitly selects its bundled collection; no flags select bundled agents and skills. A single skill directory must contain a direct `SKILL.md`; a skill collection contains child directories with direct `SKILL.md` files. Files below a skill directory, including references, are retained.

For example:

```shell
./install.py --skill /path/to/one-skill
./install.py --skills https://example.test/skills/
./install.py --skills https://github.com/example/project/tree/main/skills
```

HTTP is supported but prints a warning; prefer HTTPS. Treat remote agent and skill prompts as executable policy: use trusted sources and review the `--dry-run` output. Plugins are JavaScript that execute when OpenCode starts; remote plugins are warned about, cached before linking, and never executed by the installer. Review plugin source before installation.

### Config, providers, and profiles

Use `--config [PATH]` to merge `skills`, `agents`, and `plugins` source lists plus OpenCode settings. Without a path it loads `./config.json`; relative paths inside the file resolve beside that file. Use [`sample_config.json`](sample_config.json) as the provider/model template instead of copying configuration into this document:

```shell
cp sample_config.json config.json
./install.py --config config.json --profile profile1
./install.py --list-profiles --config config.json
./install.py --list-profiles --config
```

`model-aliases` are user-defined names that point to canonical `provider/model-id` values and optional variants. `agent-models` maps bundled delegated agents — `coder`, `researcher`, `observer`, `reviewer`, and `documenter` — through those aliases, canonical IDs, or per-agent overrides. Primary `plan` and `build` models are managed manually in the runtime configuration, and `--profile` rejects assignments for those primary names. Provider and model objects are deep-merged by ID: supplied fields override existing fields while unspecified fields and existing providers remain. The installer maps `opencode_json.providers` to the runtime `provider` key. Use environment-based credential interpolation or a credential-injecting plugin; never commit secrets to the config.

`--profile NAME` requires the explicit `--config` option. If its optional path is omitted, `--config` loads `./config.json`; in normal install mode without `--profile`, content installs normally and existing agent routing is unchanged. Config sources are processed before command-line sources; exact duplicates are deduplicated, differing same-name content fails before the target changes, and bundled content wins a name conflict.

`--list-profiles` reads the configured profiles in order, resolves aliases and variants, and labels missing agent assignments as `not assigned`. It requires `--config [PATH]` (an omitted path still uses `./config.json`). Recognized installation options — `--target`, `--profile`, `--skill`, `--skills`, `--agent`, `--agents`, `--plugin`, `--plugins`, and `--dry-run` — are ignored in list mode; `--profile` does not filter or validate a profile name, and source or target values are not resolved. `--config` is the only option that affects listing; unknown options and incomplete required arguments still produce argparse errors. Listing is read-only and does not inspect the target runtime config: its output describes configured profile assignments, not the final model OpenCode will use from the target's runtime configuration. `plan` and `build` are not profile-controlled.

### Repeat runs and restart

Unrelated existing content is left alone. Wrong or broken links are replaced; existing real files or directories are backed up under `.opencode/.install-backups/<timestamp>/` before replacement. If installation fails, new links are removed and non-symlink backups are restored.

`--dry-run` resolves, downloads, validates, and displays the plan without creating the target, cache entries, links, backups, or state. Quit and restart OpenCode after installation because configuration-time files are not hot-reloaded.

## Use

The installed `plan` and `build` modes are the Tab-selectable primary agents. Use them directly:

```text
Plan mode:
Describe the implementation plan or investigation needed, or ask Plan to assess a plan. Assessment alone is not authorization to execute it.

Build mode:
Provide the complete approved plan text, including findings and acceptance criteria, or the exact saved path to it. Explicitly authorize execution.
```

`approved` is not a special OpenCode control token, and Build should not be expected to inherit the complete planning transcript. Whenever Plan uses research or observation for any plan type—implementation, investigation, operational, or documentation—the final handoff to Build includes the complete plan and all bounded summaries Plan relied on, not only README research. Summaries include roles, sources, and citations; findings, status, and classification; decisions and implications; assumptions; unknowns, including `CANNOT VERIFY`; conflicts; acceptance and verification implications; and baseline, time window, and rollback when relevant. Do not copy raw documents or logs; mark non-applicable fields `none` or `not applicable`. Build consumes the complete plan. If the handoff is unavailable or incomplete, Build requests the handoff or returns to Plan; it re-researches only for a named material gap, conflict, or stale fact. A completeness/readiness check is not another research or approval round.

For README plans, Plan delegates `researcher` to read the target README and relevant source of truth; Build reuses that research. When the user authorizes execution of a complete README plan, Build delegates the edit to `documenter`. After every `documenter` edit, Build sends the changed documentation, approved plan, and acceptance criteria to `reviewer` for a focused final-diff check before declaring completion. Reviewer checks only the changed documentation, related links/examples, scope, and acceptance criteria—not unrelated files. Use `observer` only when current runtime or operational evidence is required, not for static repository or documentation facts. Build delegates coding changes to `coder`, coding review to `reviewer`, and post-change operational verification to `observer`; approved non-coding actions may be performed directly. Unknown root causes are handed back to Plan for evidence-backed investigation rather than guessed.

## Agents, skills, and validation

### Inventory

| Component | Role and boundary |
| --- | --- |
| Bundled `plan` / `build` | Primary planning and execution modes that override OpenCode built-ins. Their models are managed manually. |
| `researcher` | Read-only research of source evidence such as codebase, documentation, tickets, upstream material, and configuration, with cited findings; never runs shell commands or delegates. |
| `observer` | Read-only verification of current runtime and operational state, logs, and outcomes; not for static repository or documentation facts; cannot edit or delegate. Unknown operational tools require an explicitly audited read-only allowlist. |
| `documenter` | Edits ordinary Markdown and plain-text documentation only; cannot edit runtime configuration, code, or delegate. |
| `coder` | Implements delegated coding changes using `code-philosophy`. |
| `reviewer` | Is instructed to review documentation and coding changes without editing or running shell commands; this prompt is not an enforced shell-permission boundary, so configured permissions must be considered separately. Documentation checks are limited to changed docs, related links/examples, scope, and acceptance criteria; coding review uses `code-philosophy`. |
| `code-philosophy` | Baseline for correct, secure, testable, maintainable implementation and review. |
| `summarize-investigation` | Evidence-based investigation report skill for timelines, findings, root cause, and remediation. |

### Validate

Run the core checks after installation:

```shell
opencode agent list
opencode debug config
opencode models
python3 -m unittest discover -s tests -v
```

Confirm that:

- Installed `plan` and `build` are primary, with `researcher`, `observer`, `documenter`, `coder`, and `reviewer` available as delegated agents.
- For implementation, investigation, operational, and documentation plans, whenever Plan uses research or observation, its final handoff to Build contains the complete plan and every bounded summary Plan relied on—not only README research—with the contents listed in Use. If unavailable or incomplete, Build requests the handoff or returns to Plan; it re-researches only for a named material gap, conflict, or stale fact.
- For README plans, Plan delegates target-README and source-of-truth research to `researcher`; Build reuses that research and delegates authorized edits to `documenter`; `reviewer` checks every `documenter` edit against the approved plan and acceptance criteria. `observer` is used only for current runtime or operational evidence, not static repository or documentation facts.
- Build delegates coding to `coder`, coding review to `reviewer`, and post-change operational verification to `observer`; `observer` remains read-only. `reviewer` is instructed to perform read-only review, but its prompt does not enforce shell permissions; consider its configured permissions separately.
- Permission rules match the inventory: content searches may require approval because sensitive paths cannot be excluded from `grep` results.
- Observer outcomes use `PASS`, `WARN`, `FAIL`, `ERROR`, or `CANNOT VERIFY`; tool and credential failures are not healthy results.
- Selected plugins appear as symlinks under `.opencode/plugins/` and are not executed by the installer.

## Sessions

Keep one bounded objective per session. Use a concise handoff and a fresh session when changing to an unrelated objective or moving from investigation into implementation; this prevents stale assumptions and raw evidence from crowding execution context.
