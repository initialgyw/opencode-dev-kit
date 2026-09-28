# opencode-dev-kit

Reusable OpenCode agents and skills for planning, investigation, implementation, review, and operational verification.

## How it works

The kit uses OpenCode's built-in `plan` and `build` agents as primary entry points. Custom `planner` and `builder` orchestrators are invoked as subagents with `@planner` and `@builder`.

```mermaid
flowchart TD
    U[User] --> P[Built-in Plan]
    P --> PL[@planner]
    PL --> R[Researcher]
    PL --> O[Observer]
    R --> PL
    O --> PL

    U --> B[Built-in Build]
    B --> BU[@builder]
    BU --> Q{Coding request?}
    Q -->|Yes| C[Coder]
    C --> CR[Code Reviewer]
    CR --> BU
    Q -->|No| N[Non-code change]
    N --> OV[Observer verification]
    OV --> BU
    BU -. Unknown root cause .-> PL
```

For coding requests, the builder delegates implementation to `coder` and then requests `code-reviewer`. For non-coding requests, the builder asks `observer` to verify whether the resulting state fixed the issue. An unexplained failure returns to the planner's investigator behavior before remediation.

## Operating doctrine

All agents apply the same portable rules:

- Keep planning, research, observation, implementation, review, and coordination as separate responsibilities.
- Use the least-powerful suitable tool and read before changing state.
- Safety, explicit user scope, and verified evidence override speed or autonomy.
- A failed tool, missing credential, inaccessible target, or absent result never counts as success.
- Separate facts, evidence-backed inferences, assumptions, hypotheses, and unknowns.
- Give delegated work an objective, scope, inputs, constraints, evidence requirements, expected output, and stop conditions.
- Run independent work in parallel; serialize dependent work and changes to overlapping files or resources.
- Pass bounded summaries between agents instead of raw documents, transcripts, or logs.
- Require observable acceptance evidence, independent review or verification, and a rollback path where applicable.
- Keep one bounded objective per session. Use a concise handoff when changing objectives or moving from investigation to implementation.

## Included agents

| Agent | Mode | Responsibility |
| --- | --- | --- |
| `planner` | Subagent | Returns junior-friendly plans or performs read-only investigations when invoked with `@planner`. |
| `builder` | Subagent | Coordinates implementation, operational actions, review, and verification when invoked with `@builder`. |
| `researcher` | Subagent | Reads local and public documentation and returns cited findings. |
| `observer` | Subagent | Checks current state and logs without making changes. |
| `coder` | Subagent | Implements repository changes using `code-philosophy`. |
| `code-reviewer` | Subagent | Reviews completed coding changes without editing them. |

## Role consolidation

The six-agent design intentionally combines related specialist strategies:

| Generic source responsibilities | Public agent |
| --- | --- |
| Architecture, work breakdown, investigation, and root-cause analysis | `planner` |
| Interactive execution, autonomous coordination, and incident workflow | `builder` |
| Implementation and acceptance-criterion verification | `coder` |
| Documentation and upstream behavior research | `researcher` |
| Runtime verification, health sweeps, and bounded waiting | `observer` |
| Correctness, security, performance, maintainability, and over-engineering review | `code-reviewer` |

This keeps role boundaries clear without requiring separate agents for diagnosis, verification, polling, or incident response.

## Primary workflow

The built-in `plan` and `build` agents remain the Tab-selectable primary agents. The custom orchestrators are explicit subagents:

```text
Plan mode -> @planner <request>
Review the returned plan
Build mode -> @builder Implement the approved plan at <plan-path>
```

`approved` is not a special OpenCode control token. Pass the approved plan text or an exact saved plan path to `@builder`; a child session should not be expected to inherit the complete planning transcript automatically. The installer sets `subagent_depth` to at least `2` because `planner` and `builder` delegate to specialist subagents. Restart OpenCode after installation.

## Included skills

| Skill | Responsibility |
| --- | --- |
| `summarize-investigation` | Produces an evidence-based investigation report with timelines, commands, exact safe logs, diagrams, root cause, and remediation. |
| `code-philosophy` | Provides a small baseline for correct, secure, testable, and maintainable changes. |

## Capability-neutral vocabulary

Agent prompts describe capabilities instead of assuming products:

| Capability | Meaning |
| --- | --- |
| Monitoring service | Metrics, alerts, traces, health signals, and service-level indicators. |
| Logging service | Searchable application, platform, audit, and infrastructure logs. |
| Incident-management service | Incident records, responders, escalation, and current impact. |
| Runtime platform | The system running workloads, jobs, hosts, or services. |
| Deployment system | Release, rollout, health, and rollback state. |
| Infrastructure automation service | Planned and applied infrastructure changes and their run state. |
| Issue tracker | Requests, work items, ownership, acceptance criteria, and status. |
| Documentation system | Architecture, operating procedures, and decision records. |
| Source-control platform | Repositories, changes, reviews, and delivery metadata. |
| Secrets service | Controlled storage and access for sensitive values. |

Installations can provide any tools that satisfy these capabilities. Core agent behavior does not depend on a particular provider. Because `observer` is strictly read-only, each operational adapter must be explicitly allowlisted only after its operations are audited.

## Source layout

```text
agents/
  builder.md
  code-reviewer.md
  coder.md
  observer.md
  planner.md
  researcher.md
skills/
  code-philosophy/
    SKILL.md
  summarize-investigation/
    SKILL.md
install.py
sample_config.json
tests/
  test_install.py
```

## Install

`install.py` links this repository's bundled agents and skills, plus selected plugins, into the current project's `.opencode/` directory. Bundled agents and skills are always installed by default; plugins are installed only when a plugin source is selected or a root `plugins/` collection is added.

```shell
./install.py
```

The installer creates relative symlinks:

```text
.opencode/agents/<agent>.md -> ../../agents/<agent>.md
.opencode/skills/<skill>    -> ../../skills/<skill>
.opencode/plugins/<file>.js -> <local or cached plugin source>
```

Use `--target PROJECT` to install into another project. Passing a path whose final component is `.opencode` uses that directory directly.

### External sources

Singular and plural flags are repeatable and auto-detect one item versus a collection:

```shell
./install.py --skill /path/to/one-skill
./install.py --skills /path/to/many-skills
./install.py --agent /path/to/one-agent.md
./install.py --agents /path/to/agent-directory
./install.py --plugin /path/to/plugin.js
./install.py --plugins /path/to/plugin-directory
./install.py --skill https://example.test/skills/one-skill/
./install.py --skills https://example.test/skills/
./install.py --plugin https://example.test/plugins/example.js
./install.py --plugins https://example.test/plugins/
```

`--skills`, `--agents`, and `--plugins` may be used without a value to explicitly select the corresponding bundled collection. Bundled agents and skills are selected when no flags are provided; this repository currently has no bundled `plugins/` directory.

GitHub repository tree URLs are supported, including:

```shell
./install.py --skills \
  https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/tree/main/.claude/skills
./install.py --skill \
  https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/tree/main/.claude/skills/banner-design
```

A single skill directory must contain a direct `SKILL.md`. A collection directory contains child directories with direct `SKILL.md` files. All files below a skill directory are retained, including reference files.

HTTP is supported by default and prints a warning. Treat remote agent and skill prompts as executable policy: use trusted URLs, review the dry-run output, and prefer HTTPS.

Plugins are JavaScript code and execute when OpenCode starts. Remote plugin sources print an additional executable-code warning, are cached before linking, and are never executed by the installer. Review remote plugin source before installation.

### `config.json`

Use `--config` to merge external sources. With no path, it loads `./config.json`; relative paths inside the file resolve beside that file.

Copy the template first if you want a starting point:

```shell
cp sample_config.json config.json
```

The config can combine external content sources with custom OpenCode providers:

```json
{
  "skills": [
    "./external-skills",
    "./one-skill",
    "https://example.test/skills/"
  ],
  "agents": [
    "./external-agents",
    "./one-agent.md",
    "https://example.test/agents/"
  ],
  "plugins": [
    "./local-plugin.js",
    "./external-plugins",
    "https://example.test/plugins/"
  ],
  "model-aliases": {
    "opus": {
      "model": "anthropic/claude-opus-5-5",
      "variant": "high"
    },
    "google": {
      "model": "google/gemini-3.8-flash",
      "variant": "high"
    }
  },
  "agent-models": {
    "profile1": {
      "planner": "opus",
      "builder": "opus",
      "researcher": "google",
      "observer": "google",
      "coder": "opus",
      "code-reviewer": {
        "alias": "opus",
        "variant": "high"
      }
    },
    "profile2": {
      "planner": "google",
      "builder": "google",
      "researcher": "google",
      "observer": "google",
      "coder": "google",
      "code-reviewer": "opus"
    }
  },
  "opencode_json": {
    "providers": {
      "custom-openai": {
        "npm": "@ai-sdk/openai",
        "options": {
          "baseURL": "https://api.example.invalid/v1",
          "apiKey": "{env:CUSTOM_OPENAI_API_KEY}"
        },
        "models": {
          "custom-model-id": {
            "name": "Custom Model",
            "reasoning": true,
            "tool_call": true
          }
        }
      }
    }
  }
}
```

The installer maps `opencode_json.providers` to the schema-correct runtime key `provider` in `.opencode/opencode.json`. The top-level `plugins` array uses the same local/remote source rules as `skills` and `agents` and links each JavaScript basename under `.opencode/plugins/`. Provider and model objects are deep-merged by ID: values supplied by `config.json` override existing values, while unspecified existing fields remain. Existing providers and models are preserved. If `enabled_providers` already exists, new provider IDs are appended; a provider listed in `disabled_providers` causes an error instead of being silently enabled.

`model-aliases` is user-defined. Alias names such as `opus`, `sol`, `luna`, `google`, `cheap`, or `expensive` have no built-in meaning. Each alias points to a canonical `provider/model-id` and may define a default `variant`. `agent-models` is a profile map: each profile contains installed agent names mapped to aliases, canonical IDs, or objects with an alias/model and variant override.

Select a profile with an explicit config file:

```shell
./install.py --config config.json --profile profile1
```

If no `--profile` is supplied, content installs normally and existing agent routing is unchanged. `--profile` without `--config` fails intentionally. For Magnite, use `databricks-anthropic/magnite_ai.anthropic.claude-opus-5-5`; public Anthropic uses `anthropic/claude-opus-5-5`. Magnite Gemini 3.8 Flash is `databricks-google/system.ai.gemini-3-8-flash`; public Gemini is `google/gemini-3.8-flash`. Bare display names are rejected unless explicitly registered as aliases.

Config sources are processed before command-line sources. Exact duplicate sources and identical content are deduplicated. If external sources provide different content with the same name, installation fails before changing the target. Bundled content always wins a name conflict. Provider credentials should use environment interpolation or a credential-injecting plugin; do not commit secrets to `config.json`.

### Replacement and safety

The installer leaves unrelated existing agents and skills untouched. A correct existing symlink is retained. A wrong or broken symlink is unlinked and replaced without backup. Only existing non-symlink content is backed up: a real file or directory is moved to `.opencode/.install-backups/<timestamp>/` before replacement. If installation fails, newly created links are removed and non-symlink backups are restored.

Remote content is staged in `.opencode/.install-cache/` and linked from there. `--dry-run` resolves, downloads, validates, and displays the plan without creating `.opencode`, cache entries, links, backups, or state.

Quit and restart OpenCode after installation because configuration-time files are not hot-reloaded.

## Validate the installation

List the resolved agents:

```shell
opencode agent list
```

Inspect the merged OpenCode configuration and confirm the permission and provider rules:

```shell
opencode debug config
opencode models
```

Run the installer tests:

```shell
python3 -m unittest discover -s tests -v
```

Expected results:

- Built-in `plan` and `build` remain primary agents.
- `planner` and `builder` appear as subagents.
- `researcher`, `observer`, `coder`, and `code-reviewer` appear as subagents.
- `planner` cannot edit files, run shell commands, or delegate implementation.
- Content searches require approval because OpenCode cannot exclude sensitive paths from `grep` results.
- `builder` delegates repository edits to `coder` and can invoke only `coder`, `code-reviewer`, and `observer`.
- `observer` cannot edit or delegate. Unknown operational tools and non-allowlisted commands are denied; installations must explicitly allowlist audited read-only adapters.
- `code-reviewer` cannot edit or run shell commands.
- `coder` and `code-reviewer` can load only `code-philosophy`.
- `planner` can load `summarize-investigation` but returns the report in chat because it is read-only.
- Selected plugins appear as symlinks under `.opencode/plugins/` and are not executed by the installer.

## Use

Use the built-in `plan` and `build` agents as primary modes. Invoke the custom orchestrators explicitly:

```text
@planner Create the implementation plan for this request.
@builder Implement the approved plan at <plan-path>.
```

### Plan or investigate

The planner calibrates work as Quick, Thorough, or Deep. It handles small questions directly and may run independent researcher and observer requests in parallel for larger work. Plans start by explaining how the system works. Each work item states why it is needed, its goal, scope, complexity, tasks, dependencies, risks, rollback needs, and observable acceptance criteria.

Investigation ranks competing hypotheses and records supporting and conflicting evidence. A root cause is confirmed only when positive evidence supports it, no material evidence contradicts it, and the timeline is consistent. The planner can load `summarize-investigation`, but it returns the complete report in chat instead of writing a file.

### Execute

For complex work, the builder outlines phases, dependencies, parallel groups, review or verification gates, and rollback points. Coding work follows `coder` → `code-reviewer`. Critical or High review findings receive one bounded correction and re-review cycle before escalation. Non-coding work ends with observer verification.

Incident work follows observation → investigation → approved remediation → review when code changes → recovery verification. If the root cause is unknown, the builder returns a handoff to the planner rather than guessing.

Builder terminal statuses are `done`, `partial`, `blocked`, `failed`, and `cannot verify`.

### Verify or wait

Observer check statuses are `PASS`, `WARN`, `FAIL`, `ERROR`, and `CANNOT VERIFY`. Tool and credential failures are errors, not healthy results. The observer prefers explicitly allowlisted blocking or native wait capabilities; fallback polling must define success states, failure states, interval, timeout, and retained evidence before it starts.

### Session boundaries

Keep one bounded objective per session. Use a concise handoff and a fresh session when changing to an unrelated objective or moving from investigation into implementation. This prevents stale assumptions and raw evidence from crowding execution context.

## SSH safety

The observer automatically allows raw SSH patterns for a narrow set of status commands and bounded service-log queries, including hostname, time, uptime, selected process fields, sockets, filesystem, memory, and selected `systemctl` properties. Other shell and SSH commands are denied. Common shell operators, SSH command-line options, interpreters, and state-changing service commands are explicitly denied.

These raw patterns are a convenience, not a security boundary. OpenCode globs do not enforce shell token boundaries, and local SSH configuration can still change client behavior. Use them only in a trusted environment. Use a dedicated remote account with least-privilege filesystem permissions, no `sudo`, restricted commands where practical, and server-side auditing. The observer must never use an interactive shell or perform remediation.
