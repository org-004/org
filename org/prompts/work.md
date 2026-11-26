Do the spec, all of it, in this turn. Plan as much or as little as the work needs; nobody reads a plan.

Spec: `{{run_dir}}/spec.md` (sha256 `{{spec_sha}}`)
Project brief, if present: `{{run_dir}}/project.md` (read as your instructions say: the opening and the headings, then the sections the work touches)
Repositories named by the spec: {{repos}}

## Caps from the spec

{{caps}}

The runner ends the turn at a cap. Spend and time so far are in `{{run_dir}}/status.json`. If the MUST requirements cannot be met within the caps, or at all, stop early and report it: an honest `partial` or `fail` is a correct result.

## Budget right now

{{budget}}

## When the work is done

1. Unless delivery is `none`, commit and push. If delivery is `merge`, land it as your instructions describe: one squashed commit, pushed, never forced. This machine is deleted when the run ends.
2. Check every acceptance criterion in the spec ({{acceptance_ids}}) yourself. Run the check; do not reason about whether it would pass.
3. Write `{{run_dir}}/report.md` for the person who approved the spec. They read it whole, so it is short: at most about 1,200 words, tables included. Facts and figures, not narrative. These sections, in this order:
   - **Outcome**: the status and the reason in two or three sentences, with the headline figure.
   - **Delivered**: per repository, the branch and the commit on the default branch when landed.
   - **Acceptance criteria**: a table, one row per criterion: id, pass, fail or unverified, and the evidence in one line (the command and the figure it printed).
   - **Figures and tightest margins**: the measures the spec moves, before and after, as a table; then the three margins closest to their limits, each with its value, its limit and what it belongs to.
   - **Deviations**: each departure from the spec, each reading of an ambiguity, and each place the spec's Context no longer fitted what you found. One line each, with the reason.
   - **For the next run**: at most five bullets: what slowed this run, what would make the next one cheaper or faster, and work you would recommend that the spec did not ask for.
4. Finish with the structured output. `status` is `done` only when every MUST requirement is met and every acceptance criterion passes. `partial` means useful work was delivered and something required is missing. `fail` means the spec was not met. `msg` is the reason in one or two sentences, written for someone who has not read the report.
