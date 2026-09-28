---
name: code-philosophy
description: Applies a small, safe engineering baseline for implementation and review. Use before writing or reviewing source code, tests, scripts, or repository configuration.
compatibility: opencode
metadata:
  audience: software-engineers
  maturity: baseline
---
# Code Philosophy

Build the smallest correct solution to the verified problem. Simple means fewer moving parts, not weaker validation, error handling, security, or tests.

## Before writing code

1. Understand the requested behavior, current behavior, and acceptance criteria.
2. Find the root cause and the correct change boundary.
3. Ask whether new code needs to exist at all.
4. Reuse an existing project pattern before introducing a new one.
5. Prefer the standard library or native platform capability.
6. Use an existing dependency before adding another dependency.
7. Only then add the minimum new implementation.

Do not optimize a solution to a problem that has not been demonstrated.

## Correctness

- Validate data at trust boundaries.
- Make invalid states difficult to represent.
- Handle errors where useful context can be added or recovery is possible.
- Preserve the original cause when wrapping or translating an error.
- Do not ignore failures that can cause data loss, corruption, or false success.
- Consider empty input, limits, retries, partial failure, cancellation, and concurrency when relevant.
- Make state-changing operations safe to retry when the workflow requires retries.

## Design

- Fix the root cause once instead of patching every caller.
- Prefer guard clauses over deep nesting.
- Keep functions and interfaces focused on one responsibility.
- Separate pure decision logic from I/O when doing so makes behavior easier to test.
- Choose names that describe intent, not implementation history.
- Avoid speculative abstractions, generic helper layers, and configuration that has no current use.
- Keep public APIs smaller than internal implementation details.

## Dependencies and platform features

- Prefer maintained native features over custom glue.
- Do not add a dependency for behavior that is clear and reliable with existing tools.
- When a dependency is necessary, check maintenance, license, security posture, and operational cost.
- Pin or constrain versions according to the project's existing policy.

## Security and privacy

- Apply least privilege.
- Never place credentials, tokens, private keys, or production data in source, tests, fixtures, logs, or error messages.
- Treat filesystem paths, network input, templates, queries, and shell arguments as untrusted at their boundaries.
- Preserve authentication, authorization, encryption, auditing, and data-retention controls.
- Do not weaken a safety control merely to simplify a test or deployment.

## Tests and verification

- Test externally visible behavior rather than private implementation details.
- Add a regression test that fails for the original bug when practical.
- Cover the normal path, meaningful boundary conditions, and important failure behavior.
- Keep tests deterministic and independent.
- Run the narrowest relevant checks first, then the broader project checks justified by the change.
- Report commands that ran, commands that could not run, and the reason.
- Never claim verification from a test that did not execute successfully.

## Comments and documentation

- Prefer code that explains itself through structure and naming.
- Write comments for constraints, tradeoffs, or non-obvious reasons, not a narration of the code.
- Update documentation when the user-facing workflow, public API, configuration, or operational behavior changes.
- Remove stale comments instead of preserving misleading history.

## Review checklist

Before considering a change complete, ask:

- Does it satisfy every acceptance criterion?
- Is this the correct layer for the fix?
- Is there a shorter solution that remains correct on edge cases?
- Are trust boundaries, errors, and sensitive data handled safely?
- Could this change break callers, stored data, concurrency, or rollback?
- Do tests prove the changed behavior?
- Is every changed line necessary for the requested outcome?
