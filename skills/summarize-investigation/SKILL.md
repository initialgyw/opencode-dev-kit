---
name: summarize-investigation
description: Produces a junior-friendly investigation report with a timeline, commands, exact safe logs, flow diagrams, root cause, and remediation. Use when an investigation needs a durable summary.
compatibility: opencode
metadata:
  audience: engineers
  output: investigation-summary
---
# Summarize an Investigation

Create a report that lets an engineer with no prior context understand what happened, how it was investigated, why each route was chosen, and what should happen next.

## Decide whether to write

1. Determine whether this is a new investigation or a continuation of an existing one.
2. For a new investigation, use `investigation-summary.md` unless the user supplied another name.
3. Check whether the destination exists before writing.
4. If `investigation-summary.md` already exists for a new investigation, stop and ask the user which filename to use. Do not overwrite it or invent a suffix.
5. For a continuation, update an existing report only when the user identified it or clearly requested an update.
6. If the current agent cannot edit files, do not attempt a write. State `No file was created because this agent is read-only`, name the proposed destination, and return the complete report in chat.

## Evidence rules

- Include every shell, SSH, database, API, and diagnostic command used in the investigation.
- For each command, record where it ran, why it was selected, and the result that affected the investigation.
- Record non-command tools by tool name and the query or resource inspected.
- Use the smallest output excerpt that proves the conclusion; do not dump full logs.
- Include relevant non-sensitive log lines exactly as recorded. Preserve spelling, capitalization, timestamps, identifiers, and spacing.
- Never silently rewrite or partially redact a safe log line.
- If a relevant line contains a credential, token, private key, session identifier, personal data, or other secret, do not reproduce that line. Record its source location, timestamp if safe, the type of sensitive data, and what the line proved.
- Never reproduce a command containing a secret. Show a safe command shape such as `<token from environment>` and state where the value is managed, not its value.
- Do not disclose sensitive hostnames, private URLs, account numbers, or addresses when the user has classified them as sensitive. Identify the system or source location at the least-sensitive useful level.
- Distinguish direct evidence, interpretation, and assumptions.

## Explain the system first

Write for a junior engineer who has no background in the system:

- Define unfamiliar terms on first use.
- Explain the normal request, data, or control flow before explaining the failure.
- Identify each component's responsibility and the boundary where the failure occurred.
- Use short sentences and concrete language.

Include a flow representation when it materially clarifies the investigation; omit it when concise prose is clearer. Mermaid, ASCII, or concise call/data-flow syntax such as `funcA() -> funcB() -> funcC()` are all valid.

- For a code investigation, show the call or data flow from entry point to the failing branch when useful.
- For system troubleshooting, show the participating services or hosts, their normal interactions, and the observed failure point when useful.
- Include both views only when both are needed to understand the incident.

Any flow representation must reflect verified behavior. Mark inferred edges as assumptions.

## Explain every investigation step

For each step, include:

- **Why this route**: why this check was more useful than the alternatives at that point.
- **Goal**: the fact the step was intended to prove or disprove.
- **Action**: command, query, file, log, dashboard, or document inspected.
- **Result**: the bounded evidence collected.
- **Conclusion**: how the result changed the next step or hypothesis.

Keep failed paths. They show which causes were ruled out and prevent repeated work.

## Report structure

Use this structure, omitting only optional sections that truly do not apply:

````markdown
# Investigation Summary: <short title>

## Executive Summary
<What happened, user impact, root cause or leading hypothesis, and current state in plain language.>

## How the System Works
<Normal behavior and definitions needed by a new engineer.>

<Optional verified flow representation, if useful. Use Mermaid, ASCII, or concise call-flow text such as `funcA() -> funcB() -> funcC()`; omit it when prose is clearer.>

## Impact and Scope
- **Started**: <timestamp and timezone, or unknown>
- **Ended**: <timestamp and timezone, ongoing, or unknown>
- **Affected**: <users, requests, hosts, services, or data>
- **Not affected**: <important boundaries supported by evidence>

## Timeline
| Time | Event | Evidence |
| --- | --- | --- |
| <timestamp and timezone> | <what happened> | <source> |

## Investigation
### Step 1: <descriptive name>
- **Why this route**: <reason>
- **Goal**: <question to answer>
- **Action**: <where and what was inspected>
- **Commands**:
  ```shell
  <exact safe command or safe command shape>
  ```
- **Evidence**: <smallest useful excerpt or source reference>
- **Conclusion**: <what this proved and why the next step followed>

## Relevant Log Evidence
**Source**: `<file, log group, service, or host>`

**Time window**: `<start to end with timezone>`

```text
<exact non-sensitive log lines>
```

## Hypotheses Considered
| Hypothesis | Supporting evidence | Conflicting evidence | Result |
| --- | --- | --- | --- |
| <possible cause> | <facts> | <facts> | Confirmed, rejected, or unresolved |

## Root Cause
<The technical and contributing causes, confidence level, and evidence chain. If unconfirmed, label this Leading Hypothesis instead.>

## Remediation
### Immediate Recovery
<What restored or can restore service safely.>

### Permanent Correction
<What prevents recurrence by fixing the root cause.>

### Verification
<Observable checks proving the remediation works.>

### Rollback
<How to return to the previous safe state when applicable.>

## Remaining Unknowns
<Questions that evidence could not answer and why.>
````

## Quality check

Before saving or returning the report, confirm that:

- The timeline uses explicit timezones and evidence-backed ordering.
- Every investigation command and tool query is documented safely.
- Every step explains why it was chosen and what it established.
- Included log lines are exact and contain no sensitive information.
- No secret value appears anywhere in the report.
- Any included flow representation matches the written explanation.
- Root cause is not stated more confidently than the evidence permits.
- Immediate recovery and permanent correction are clearly separated.
- A junior engineer can understand the report without hidden context.
