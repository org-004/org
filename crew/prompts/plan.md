Plan the implementation of the spec. Do not implement it in this turn.

Spec: `{{run_dir}}/spec.md` (sha256 `{{spec_sha}}`)
Project brief, if present: `{{run_dir}}/project.md`
Repositories named by the spec: {{repos}}

## Caps from the spec

{{caps}}

A plan that does not fit the caps is not a plan. If the MUST requirements fit and some SHOULD items do not, plan without them and list them in `dropped_requirements`. If the MUST requirements cannot be met within the caps, or at all, the verdict is `infeasible`.

## Budget right now

{{budget}}

## How past estimates turned out

{{history}}

## What to do

1. Research until the estimate is something you would defend: read the code, run the build and the tests and time them, try the riskiest step small. Spend in proportion; planning is a small fraction of the job.
2. Decide how to do the work cheaply and quickly: what is settled first, what fans out and how wide, which model does which part, what order avoids collisions, what runs on the machine in the background.
3. Break the work into milestones. Milestones run in order, one turn each, and each ends in a state you can verify and have pushed. Parallelism lives inside a milestone. Two to eight is typical; a small job may be one.
4. Estimate each milestone in wall-clock minutes and dollars, from what you measured. `cost_usd` is API-equivalent dollars at the table's prices and includes your own turns as well as subagents. Give the totals in `estimate`, with a 90th-percentile figure for each. Where the budget section gives a dollars-per-percent rate, also state in `plan.md` what share of the weekly limit the plan will use and how much of the limit is left; the person approving work budgets in that unit.
5. Write `{{run_dir}}/plan.md`. A human reads it after the run to judge the planning, so make it complete on its own: the approach and why, alternatives you rejected, the milestones with their fan-out and model choices, the estimate and what it rests on, assumptions, and risks with what you will do if they land.
6. Finish with the structured output. It must agree with `plan.md`.
