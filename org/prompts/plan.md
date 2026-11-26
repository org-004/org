Plan the implementation of the spec. Do not implement it in this turn.

Spec: `{{run_dir}}/spec.md` (sha256 `{{spec_sha}}`)
Project brief, if present: `{{run_dir}}/project.md` (read as your instructions say: the opening and the headings, then the sections the work touches)
Repositories named by the spec: {{repos}}

## Caps from the spec

{{caps}}

A plan that does not fit the caps is not a plan. If the MUST requirements fit and some SHOULD items do not, plan without them and list them in `dropped_requirements`. If the MUST requirements cannot be met within the caps, or at all, the verdict is `infeasible`.

## Budget right now

{{budget}}

## How past estimates turned out

{{history}}

## What to do

1. Research until the estimate is something you would defend: read the code, run the build and the tests and time them, try the riskiest step small. Planning is a small fraction of the job; spend in proportion.
2. Decide how to do the work cheaply and quickly, yourself in this one session: what is settled first, what order avoids rework, what runs on the machine in the background.
3. Break the work into milestones. They run in order, one turn each, and each ends in a state you can verify and have pushed. Use as few as the work allows: each turn costs a fresh look at the context. Most jobs need one to three.
4. Estimate each milestone in wall-clock minutes and dollars, from what you measured. `cost_usd` is API-equivalent dollars at the table's prices, your own turns included. Give the totals in `estimate`, with a 90th-percentile figure for each. Where the budget section gives a dollars-per-percent rate, also state the share of the weekly limit the plan will use.
5. Write `{{run_dir}}/plan.md`, at most about 1,000 words; a person reads it after the run to judge the planning. It holds: the approach and why, in a paragraph; alternatives rejected, one line each; the milestones as a table (goal, minutes, dollars, verification); what the estimate rests on, with the timings you measured; assumptions; risks, each with what you will do if it lands. No code listings and no restating of the spec.
6. Finish with the structured output. It must agree with `plan.md`.
