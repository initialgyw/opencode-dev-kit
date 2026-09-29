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

Treat remote agent and skill prompts as executable policy: use trusted sources and review the `--dry-run` output. Plugins are JavaScript that execute when OpenCode starts; remote plugins are warned about, cached before linking, and never executed by the installer. Review plugin source before installation.

## config.json

Copy [`sample_config.json`](sample_config.json) to `config.json`; this is the installer's input, not OpenCode's runtime config. Any runtime config the installer writes is `.opencode/opencode.json`.

`--config PATH` loads the specified installer input; `--config` without a path reads `./config.json` from the current working directory. Relative local source paths in `skills`, `agents`, and `plugins` resolve relative to the config file. No top-level key is required, but the file must be a JSON object.

| Optional top-level key | Default | Purpose |
| --- | --- | --- |
| `skills` | `[]` | Optional skill sources. |
| `agents` | `[]` | Optional agent sources. |
| `plugins` | `[]` | Optional plugin sources. |
| `model-aliases` | `{}` | Optional aliases for `provider/model-id` references. |
| `agent-models` | `{}` | Optional per-profile model assignments for supported bundled delegated agents. |
| `opencode_json` | `{}` | Optional runtime overlay; only its `providers` child is accepted and merged into runtime `provider`. |

Use environment-based credential references rather than literal secrets.

## Agents and Skills

### Agents

| Agent | Purpose |
| --- | --- |
| `plan` | Read-only primary planner and investigator for architecture, debugging, and root-cause analysis. |
| `build` | Primary implementation coordinator that delegates specialist work and handles approved non-coding actions. |
| `coder` | Implements scoped coding changes delegated by Build. |
| `researcher` | Gathers read-only, cited repository, documentation, ticket, upstream, and configuration evidence. |
| `observer` | Collects current runtime and operational evidence and verifies outcomes without making changes. |
| `documenter` | Edits ordinary Markdown and plain-text documentation only. |
| `reviewer` | Reviews completed code changes and focused documentation diffs without editing. |

### Skills

| Skill | Purpose |
| --- | --- |
| `code-philosophy` | Small, safe engineering baseline for implementation and review. |
| `summarize-investigation` | Produces evidence-based investigation reports with timelines, findings, root cause, and remediation. |

### How to run tests

Run the automated test suite with:

```shell
python3 -m unittest discover -s tests -v
```

## Sessions

Keep one bounded objective per session. Use a concise handoff and a fresh session when changing to an unrelated objective or moving from investigation into implementation; this prevents stale assumptions and raw evidence from crowding execution context.
