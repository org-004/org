The run is ending. Verify the result, write the report, and return the final status.

Why the run is ending: {{reason}}

## Measured by the runner

These numbers are measurements, not your recollection. Use them as they are.

```json
{{actuals}}
```

## Repositories found in the workspace

{{repo_checks}}

Anything listed as uncommitted or unpushed is lost: this machine is deleted as soon as the run ends, and only the report and what was pushed survive. Evidence the report relies on must be in the report itself or pushed. Unless delivery is `none`, commit and push it now, and if delivery is `merge` and the work is verified and not yet landed, land it on the default branch as your instructions describe: one squashed commit, pushed, never forced.

## What to do

1. Check every acceptance criterion in the spec ({{acceptance_ids}}). Run the check; do not reason about whether it would pass. Where the run changed code, have a subagent that did not write the code verify the criteria from the spec alone, on a model you choose for the difficulty, and reconcile any disagreement before you report.
2. Write `{{run_dir}}/report.md` for the person who approved the spec. They read it whole, so it is short: at most about 1,200 words, tables included. Facts and figures, not narrative. What changed in the code is in the commit message; do not repeat it. These sections, in this order:
   - **Outcome**: the status and the reason in two or three sentences, with the headline figure.
   - **Delivered**: per repository, the branch and the commit on the default branch when landed.
   - **Acceptance criteria**: a table, one row per criterion: id, pass, fail or unverified, and the evidence in one line (the command and the figure it printed).
   - **Figures and tightest margins**: the measures the spec moves, before and after, as a table; then the three margins closest to their limits, each with its value, its limit and what it belongs to.
   - **Deviations**: each departure from the spec or the plan, each reading of an ambiguity, and each place the spec's Context no longer fitted what you found. One line each, with the reason.
   - **Forecast against actual**: a table per milestone (minutes and dollars, estimated and measured), then the cause of the largest miss in a sentence or two.
   - **For the next run**: at most five bullets, together: what slowed this run, what would make the next one cheaper or faster, and work you would recommend that the spec did not ask for. One line each.
3. Finish with the structured output.

`status` is `done` only when every MUST requirement is met and every acceptance criterion passes. `partial` means useful work was delivered and something required is missing. `fail` means the spec was not met. `msg` is the reason in one or two sentences, written for someone who has not read the report.
