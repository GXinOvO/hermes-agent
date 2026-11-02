# Completing tasks with existing Hermes tools

Use this workflow for an authorized task. Keep the project's original acceptance,
permissions and model choices. These instructions guide the agent; they do not
install a runtime gate or activate in an already-running session automatically.

## Choose the shortest sufficient path

For a short follow-up, answer from accepted context and refresh only a fact that
could change the answer. Do not restart research or add a reviewer just to restate
an accepted result. For a larger task, read the existing plan and evidence first,
then investigate only the unresolved question.

Batch independent tool calls. Use `execute_code` or an existing reusable entry for
deterministic collection, filtering and calculations instead of serial model
turns. Return the information needed for judgment, retaining full output when it
is needed for later diagnosis. Delegate only independent deliverables or a required
independent review; name one writer per file and keep the parent responsible for
integration and authorized side effects.

Reuse evidence while its source, inputs, dependencies, environment and requirements
remain applicable. Rerun a check when one of those changes, instability matters, or
an original acceptance item is still missing. Preserve the first necessary tests,
real failure diagnosis and required reviews. Finish when those requirements pass;
a new search, review round or rerun needs a concrete remaining question.

## Structured artifact handoffs

Use ordinary text for ordinary text tasks. For an artifact-producing child with a
structured consumer, attach a task-specific `output_schema` to `delegate_task`.
Start from `templates/delegation-output.schema.json` when the six-field material
contract fits; additional constraints may tighten that contract. Give the child
the exact files and checks the parent requires before dispatch.

Check the individual native result's lifecycle, `schema_valid`, substantive answer
and actual artifacts. A `completed` lifecycle can coexist with an invalid schema
or an unfinished summary. Save the returned result without rebuilding a successful
envelope from the child's prose or inventing `schema_valid=true`; retain its native
dispatch identity and validation verdict. Missing native validation evidence stays
unknown even if a later local validation passes.

Call `scripts/check-delegated-result.py` through `terminal` to check this material
contract. From the repository root with the repository environment active:

```python
terminal(command="python -c \"import runpy,sys; sys.argv=['check','--result','TASK/child-result.json','--required-artifact','TASK/review.md','--require-checks']; runpy.run_path('skills/autonomous-ai-agents/hermes-agent/scripts/check-delegated-result.py', run_name='__main__')\"")
```

For an installed skill, use the installed Hermes bootstrap reported by
`hermes --print-runtime-command`: preserve initialization and replace the final CLI
entry with `runpy.run_path` plus the `sys.argv` above. That supplies the native JSON
extraction helper and `jsonschema` from the Hermes environment without installing
dependencies into an ambient Python. `python -m runpy <file>.py` treats the argument
as a module name and fails; use `runpy.run_path`.

- `--required-artifact` is the parent's expectation, not a list inferred only from
  the child's answer. Repeat it for every required file.
- `--require-checks` rejects an empty checks list. Omit it only when the task needs
  no executable check; an empty list is not evidence that tests passed.
- `--schema` can add task-specific constraints to the six-field material contract;
  it cannot remove its fields or weaken its types. Use the original consumer for
  unrelated output shapes instead of forcing every task into this template.
- Relative artifact and receipt paths resolve from `--base-dir`, or the result
  file's directory by default.

The script rejects missing or false native validation, non-pass/remaining work,
missing or empty declared/required files, failing reported commands and missing
receipts. Exit zero means only that structure and material checks passed. Read
those artifacts, check source/input identity and actual command results, and apply
the original product acceptance. File presence does not prove semantics, freshness,
hashes, command truth or external-state changes. Do not turn this helper into an
unconditional hook or a second task authority.

## Complete once

Consume each asynchronous result once by its attempt identity. An already-accepted
notice needs no repeated inspection or acknowledgement. A missing promised file or
unfinished summary remains partial even when the child says completed. Preserve
failures rather than promoting a parent's repaired answer into a successful child.

When a new asynchronous result is the only dependency, save one checkpoint and give
a nonempty pending status. Do not poll child transcripts or send repeated unchanged
recaps. Distinguish saved rules, native loading, actual task use, parent acceptance,
other-session adoption and measured benefit. A helper test or a Git merge alone
does not prove faster accepted delivery.
