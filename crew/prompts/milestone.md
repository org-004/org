Execute milestone `{{milestone_id}}` ({{position}}): {{title}}

Goal: {{goal}}
Verification you planned: {{verification}}
Estimate: {{est_minutes}} minutes, ${{est_cost_usd}}

## Where the run stands

{{standing}}

## What to do

Do the milestone as planned, verify it, commit, and push. Then end the turn with the structured output.

- `outcome` is `done` when the milestone's verification passes, `partial` when some of it is deferred and the run can still continue, `blocked` when it cannot be completed.
- If what you learned changes the rest of the plan, return the new remaining milestones in `revised_remaining`, in order, with estimates. They replace every milestone not yet started. Record the change and the reason under "Amendments" in `{{run_dir}}/plan.md`; the original plan is kept separately for review.
- Set `stop` to true only when continuing to the remaining milestones is pointless or would break a cap. The run then goes straight to the report.
