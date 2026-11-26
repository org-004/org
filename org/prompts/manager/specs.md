# Writing specs

Read by the manager before it writes a spec. Not a prompt: nothing here is filled in.

## From an objective to a spec

A spec starts from an objective of the roadmap or from something the owner said. Either way the words are one way of getting what is wanted, and often not the best one.

1. **Find the intent.** Write, in a sentence or two, what the work is for and which measure it should move. If the request and the intent pull apart, the intent wins, and the owner is told when they are there.
2. **Look before you write.** Read the repositories, clone them into scratch and build them, run the tests, time things, search the web for how others solved it, check which runs are in flight and what past runs reported. Check the claims in a request too: people misremember their own code, and so does a journal.
3. **Name what is missing.** Edge cases, failure modes, migration and rollback, data and security, what breaks for existing users, collisions with engineers already in the same code, cost.
4. **Weigh alternatives.** A narrower, cheaper version and a version that serves the intent better than the literal request, with what each costs. Pick one and record why the others lost.
5. **Size it.** `org review <run>` on past runs of the project shows how estimates held. A large objective is usually several specs: decide which run side by side and which are sequenced because they touch the same files.
6. **Do not dispatch what you can finish.** When you may change repositories yourself (the last section of your prompt says), a small fix or a document is quicker done than specified.

Every ambiguity left in a spec becomes a guess made alone at three in the morning.

## The sections

- **Header.** `repos`: every repository involved, as owner/name or a URL. An engineer cannot create a repository; one that is needed must exist first. `delivery`: `merge`, `branch` (the engineer pushes its branch and stops) or `none`. `after`: see Plans. `max_cost_usd`, `max_weekly_pct`, `max_hours`: hard caps the runner enforces. `model`, `effort`, `machine`: leave out unless there is a reason.
- **Intent** is what an engineer falls back on when something is ambiguous. It must be true.
- **Context** holds what you verified: where things are, what you ran and what it showed, at which commit. Facts with paths and commands, not impressions.
- **Requirements** are one testable statement each, `- R1 (MUST): ...`. MUST defines success. SHOULD is what gets dropped first, which is how an engineer knows what a partial result may leave out. When a change spans repositories, say what changes in each and the order they land in.
- **Out of scope** names what a reasonable engineer might assume is wanted and is not.
- **Acceptance criteria** are checks someone else could run: `- AC1: <command> <expected observation>`. A requirement that cannot be checked is rewritten. Landed work is checked on the default branch, for example that `git log origin/main -1` shows the commit. Do not ask an engineer to wait for CI: you read the checks when the run ends, and a default branch they leave broken gets a spec of its own.
- **Constraints** hold branches, compatibility, performance and security limits, memory, the order in which to drop requirements when the budget runs short, and the reasoning behind the caps.
- **Alternatives considered** records what was weighed and why it lost, so the engineer does not reopen it.
- **Open questions** must read `None`. Anything that someone still has to supply or do before an engineer could start is an open question, never a placeholder.

Ask for what the owner reads: the tightest margins printed in the report, and where progress is visible, recordings and versions that show it.

## Plans

Work that builds on other work, or touches the same files, is several specs joined by a header line such as `after: 008, 9` (spec ids or numbers). The run of that spec stays queued until each named spec has finished `done` or `partial` and the review between them has said `continue`. Split work this way when a later piece needs what an earlier one leaves. Do not split it when the pieces are independent (they run side by side without `after`) or when a piece means nothing until the next one lands: that is one spec.

- Write each later spec's Context as what it assumes the earlier ones will leave: names, files, behaviour, commands that will work. The review between them reads the report against those assumptions, and it can only check what is written down.
- Without anyone: a spec starts when what it waits for has finished and been reviewed; one retry of the same text after a failure a fresh attempt could cure; up to two resumes of a runner or machine that died.
- What stops: a review that says `pause` or gives no valid answer, a second failure, a stopped run, and a weekly limit at 80 percent. Everything that depends on the cause is held, and `notify.url` (if `org doctor` shows it set) is told.
- `org release <run or spec>` starts a held run whatever it waited for. Read the reason for the pause first. If the reason still holds, the fix is a revised spec, not a release.
- `org graph` shows every plan: its specs in order, each one's run and state, what it waits for or why it is held, and each review's verdict and reason.
