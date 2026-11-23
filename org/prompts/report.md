The run is ending. Verify the result, write the report, and return the final status.

Why the run is ending: {{reason}}

## Measured by the runner

These numbers are measurements, not your recollection. Use them as they are.

```json
{{actuals}}
```

## Repositories found in the workspace

{{repo_checks}}

Anything listed as uncommitted or unpushed is lost when this machine is deleted. Unless delivery is `none`, commit and push it now, and if delivery is `merge` and the work is verified and not yet landed, land it on the default branch as your instructions describe: one squashed commit, pushed, never forced.

## What to do

1. Check every acceptance criterion in the spec ({{acceptance_ids}}). Run the check; do not reason about whether it would pass. Where the run changed code, have a subagent that did not write the code verify the criteria from the spec alone, on a model you choose for the difficulty, and reconcile any disagreement before you report.
2. Write `{{run_dir}}/report.md` for the human who approved the spec:
   - Outcome: the status and the reason in two or three sentences.
   - Delivered: for each repository, the branch, and the commit on the default branch when the work was landed there.
   - Acceptance criteria: each one, pass, fail or unverified, with the evidence.
   - Deviations and assumptions: where you departed from the spec or the plan, and why.
   - Forecast against actual: time and cost, estimated and measured, per milestone, with the cause of each miss. Be specific about what you misjudged; this section is how the next estimate gets better.
   - What slowed the run: waits, retries, machine limits, tooling, unclear parts of the spec.
   - Remediations: what would make the next run cheaper or faster — a larger machine, a refactor, a cache, a change to how specs are written.
   - Follow-ups: work the spec did not ask for that you would recommend.
3. Finish with the structured output.

`status` is `done` only when every MUST requirement is met and every acceptance criterion passes. `partial` means useful work was delivered and something required is missing. `fail` means the spec was not met. `msg` is the reason in one or two sentences, written for someone who has not read the report.
