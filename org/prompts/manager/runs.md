# Reading runs

Read by the manager when it needs more than `org status`. Not a prompt: nothing here is filled in.

These read from the engineers' machines without touching their sessions:

| command | shows |
|---|---|
| `org status` (`--json` for all of it) | every run: state, milestones done, elapsed, ETA, spend, share of the weekly limit; held runs and the cause of a pause |
| `org peek <run>` | one run in detail, with its recent activity and running subagents |
| `org logs <run>` | its activity feed |
| `org plan <run>` | the plan as frozen before execution (`--current` includes amendments) |
| `org report <run>` | the engineer's report |
| `org review <run>` | plan against outcome: time, cost, per milestone, and what the machine measured |
| `org diff <run>` | every repository in the engineer's workspace: branch, uncommitted and unpushed work, recent commits |
| `org ls <run> [path]`, `org cat <run> <path>` | the files on the engineer's machine, read-only |
| `org pool` | the machines engineers run on, their slots, and what is queued |
| `org graph` | every plan: specs in order, runs, what each waits for, review verdicts |

- `org diff`, `org ls`, `org cat` and `org exec` reach a run only until it ends: the machine of a finished run is deleted. After that, work from the report, `org review` (peak memory beside the limit, CPU, kills for memory, restarts are kept) and the git remote.
- While a run is going, leave it alone. Look when there is a reason: it is far past its plan, it is close to a cap, or something else waits on it.

## When a run ends

1. Read `org report <run>` and `org review <run>`.
2. Check what the report claims: the commit on the default branch, the files it names, and the checks on that commit. A claim you did not check is reported as the engineer's claim.
3. Go through the acceptance criteria one by one: passed, failed, or not shown.
4. Compare forecast and outcome: time, cost, milestones, memory. Name the tightest margins the work left (the least clearance, the closest limit, the slowest check); they are where it is weakest.
5. A `partial` or a `failed` says as much about the spec as about the engineer. Decide what the next spec changes: a smaller scope, a missing fact in Context, a bigger machine, a different cap.
6. Write it down: the outcome in the journal, the lesson in `projects/<name>.md`, the plan in `roadmap.md` if it changed. Tell the owner in a few lines if they are there.
7. Start what comes next.
