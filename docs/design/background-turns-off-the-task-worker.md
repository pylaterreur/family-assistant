# Background LLM turns off the task worker

**Status:** Approved 2026-10-21 (M0 through M3). Implemented.

## Summary

Delegated runs keep breaking because a 30-minute LLM turn runs *inside* a task-queue handler. That
handler sits under several independent clocks that were each tuned separately, and some of them
contradict each other. When any one of them fires, the run is cancelled with nothing recorded. The
retry then fails the run outright, even though everything needed to continue it is already in
history.

Recommendation: **tasks start turns; they don't host them.** A task handler validates, claims and
launches the turn, then returns. The turn runs as a tracked in-process asyncio task that holds a
**turn lease**. This is the same lease, heartbeat, suspend and resume machinery that web turns and
delegation wake turns already use. Liveness comes from the lease rather than from the task claim.
Interruption leads to *resume*, not *fail*. Each turn has exactly one budget.

## What is going wrong

### Too many clocks, with unenforced ordering

A delegated run today is governed by all of these:

| Clock                      | Where                                      | Value                                                              | What it assumes                          |
| -------------------------- | ------------------------------------------ | ------------------------------------------------------------------ | ---------------------------------------- |
| Worker inactivity watchdog | `assistant.py` `WORKER_INACTIVITY_TIMEOUT` | 600 s since last claim/finish                                      | no handler runs longer than this         |
| Handler timeout            | `task_worker.py` budgets table             | 300 s default, 1,920 s delegated, 600 s delivery and memory review |                                          |
| Delegated turn budget      | `DELEGATED_RUN_TURN_BUDGET_SECONDS`        | 1,800 s                                                            | expires before the handler timeout       |
| Stale-claim takeover       | `tasks.py` `STALE_TASK_TIMEOUT_MINUTES`    | 900 s from claim, `locked_at` never renewed                        | "well above the handler timeout (300 s)" |
| Delegation reaper          | `DELEGATION_RUN_STALE_SECONDS`             | 3,600 s                                                            | above handler timeout plus retries       |
| Delivery wake budget       | `DELEGATION_DELIVERY_TIMEOUT_SECONDS`      | 600 s                                                              | under the stale-claim cutoff             |
| Turn lease                 | `turn_resumption.py`                       | 120 s lease, 30 s heartbeat                                        |                                          |
| MCP tool call              | `DEFAULT_TOOL_CALL_TIMEOUT_SECONDS`        | 1,800 s                                                            |                                          |

Each clock is only safe if it is ordered correctly against the others, and nothing checks that
order. Today it is broken in three places:

- **The watchdog (600 s) is shorter than the delegated handler (1,920 s).** It also matches the
  delivery and memory-review budgets exactly, so those two race it. This is the Tuit bug, and it has
  killed 6 runs since 17 Oct.
- **The stale-claim cutoff (900 s) is shorter than the delegated handler.** A second worker takes
  the run over while the first is still running it. This was hidden behind the watchdog bug.
- **A hung tool call (1,800 s) can use up the whole turn budget (1,800 s).** The model never gets to
  react to the tool failing. Run 906 hung inside `execute_shell`.

Every recent fix either added a clock or moved one: the 30-minute budget (#1466), the delivery task
with its 600 s budget (#1485), and claim release on lease expiry. Each fix was correct where it was
made, but each one added another invariant that someone has to remember.

### Interruption fails the run instead of continuing it

When a delegated run is cancelled from outside, it stays `running`. The retry's entry guard then
fails it with "interrupted before completion and cannot be safely retried". That rule was written
before commit-as-you-go and turn resumption existed. Today every tool call and result is committed
as it happens, and `handle_chat_interaction(resume=True)` continues a turn from its rows. Web turns
and delegation wake turns already resume this way. Delegated runs are the main long-turn path, and
they never got it: they arm no lease and register no resumer.

### Long turns occupy scarce worker slots

The pool has 2 general workers and 1 reserved worker. A delegated run holds a general worker for its
entire turn, which can be 32 minutes, including time spent waiting for a human confirmation. Two
concurrent delegations stall every background-lane task until one of them ends.

The `parks_on_queued_work` and reserved-worker exclusion machinery exists only to stop this from
deadlocking. `async_delegation_robustness.md` named the real fix in its known constraints: "run
delegated turns off the task-handler slot".

### Two lifecycles for one thing

The same turn is tracked twice:

- as a task claim, through the watchdog, the handler timeout and the stale takeover;
- as a turn, through the lease, `RunningDelegationTurns` and the reaper.

The delivery wake path shows the cost of this split. `DelegationWakeResumer` has to release the
right worker's claim (`claimed_by`), count the lost attempt, and deliver past a failed final claim.
Two commits on 21 Oct were spent on exactly that.

## Proposal: tasks start turns, a turn runner hosts them

One process-wide **background turn runner** is wired in `assistant.py`. It sits next to the
`TurnLeaseRegistry` and `RunningDelegationTurns`, which it absorbs or uses. It is in-process,
because FA runs as a single process.

1. **Launch, don't await.** `delegated_profile_run` does what it does now (validation, permission
   re-check, remote vs. local routing, `mark_running`). Then it launches the turn on the runner and
   returns. The task row is done within seconds, so the watchdog, the handler timeout and the stale
   takeover no longer touch the turn.
2. **The lease is the liveness signal.** The runner arms a turn lease in the same transaction that
   marks the run `running`. The existing per-process heartbeat keeps the lease alive. On graceful
   shutdown, `suspend_all` and `hand_off` already stop the turn at a loop boundary and hand it on.
3. **Interruption resumes the turn.** A `delegated_run` resumer is registered with the lease
   registry. When a lease comes due, the existing `decide()` logic determines what happens: the turn
   is finished, superseded, awaiting confirmation, or should resume. If it should resume, the turn
   is relaunched with `resume=True`, bounded by `MAX_RESUME_ATTEMPTS`. When attempts run out, the
   run is failed and given a clear reason. The guard that fails a run because it is "running on
   entry" goes away for local runs.
4. **One budget per turn.** The runner enforces the turn's budget and, when it expires, fails the
   run as timed out (`_fail_timed_out_delegation_run` already does this). Cancellation still uses
   `RunningDelegationTurns.stop`, and Stop and timeout share one completion path.
5. **Completion is a callback, not a return value.** When the turn ends, the runner settles the run
   (`_settle_delegated_turn`) and queues delivery. A crash between those two steps is already
   covered by the reaper and the `find_terminal_unnotified` sweep.
6. **A concurrency cap of its own.** The runner limits how many background turns run at once (a
   config value, defaulting to something like 4). If it is at the cap, the handler leaves the run
   `queued` and reschedules its task. The thread-run path already does this
   (`THREAD_TURN_DEFER_SECONDS`). Task-worker slots are never used up by turns.

After this, task workers only run short handlers, so the clocks can be simplified:

- **Watchdog:** a worker counts as stuck only when it is past its *current task's* deadline plus a
  margin, not when it has gone 600 s without a claim. Alternatively, remove the activity heuristic
  and keep the dead-task restart.
- **Stale claim:** renew `locked_at` while a handler runs. At startup, assert that the stale cutoff
  exceeds the longest handler timeout in the budgets table. This turns a remembered invariant into a
  checked one.
- **Parking:** the `parks_on_queued_work` flag and the reserved-worker exclusion can go, because
  nothing parks a worker any more.
- **Tool calls:** a hung tool should fail *that call* well inside the turn budget, so the model can
  react. Lower the MCP default (to about 10 minutes, for example) or cap it relative to the turn's
  remaining budget. This is a small separate decision.

### What this deliberately doesn't change

- Remote (pollable) delegations already submit and poll without blocking, so they are untouched.
- The delegation-run state machine, delivery semantics and the taint and review hand-off stay as
  they are. Only *where the turn executes* changes.
- Confirmation waits still count against the turn budget. That is today's behaviour, and changing it
  would be a separate product decision.

### Deliberate simplifications

- **Resume can repeat a tool call that was mid-flight at the crash.** This is the same residual risk
  web and wake turns already accept: a call whose result was never committed. It is bounded by
  `MAX_RESUME_ATTEMPTS`, and it is better than today, where *every* interruption fails the run.
- **No cross-process runner.** FA is a single process, and the lease and resume handle restarts.
- **A wake delivery that fails after it is launched is left to the hourly cleanup.** Inside the
  task, a transient failure was retried by the task queue within minutes. Once the delivery runs as
  a background turn there is no task left to retry, and a wake that fails transiently already falls
  back to the standard notice, so only a notice that also fails waits for the cleanup's next pass.
- **Callback and memory-review turns keep their task as their lease.** Their handlers already
  checkpoint per task (the turn id comes from the task id), so M3 runs the whole task -- handler
  timeout, completion, retries -- on the runner and keeps renewing its claim, rather than giving
  them turn leases and resumers of their own. A crash is recovered by the stale-claim reclaim, which
  retries the task and resumes its turn, as before. The watchdog's idle check stays: with no turn
  inside a handler it only fires on a worker loop that has really stopped polling.
- **Deliveries are not held back by the concurrency cap.** A delivery belongs to a run that was
  admitted already, and making it wait would hold back the result rather than bound new work.

## Work plan

- **M0: stop the bleeding (small, now).** Make the watchdog track each task's own deadline, renew
  `locked_at` while a handler runs, and add the startup assertion that ties the stale cutoff to the
  budgets table. A watchdog cancellation should fail the run with a clear reason. *Verified by:* a
  worker test showing a delegated handler running past 600 s and 900 s of fake-clock time is neither
  cancelled nor taken over. This meets the Tuit task's done-means on its own.
- **M1: delegated runs on the runner.** Add the runner, the lease, the `delegated_run` resumer and
  the concurrency cap, and remove the "running on entry" failure for local runs. *Verified by:*
  functional tests showing that (a) the handler returns while the turn is still running, (b) a run
  interrupted mid-turn resumes without rerunning a completed tool, (c) a timeout and a
  `cancel_delegation` each settle the run once, and (d) runs over the cap stay queued and start
  later.
- **M2: the other delegation turns.** Move delivery wake turns, `awaiting_children` continuations
  and thread first turns onto the runner. Delete the delivery task's 600 s budget and the
  `claimed_by` claim-release dance in `DelegationWakeResumer`. *Verified by:* the existing #1485
  wake-resume tests passing with the claim plumbing removed.
- **M3 (optional): every background LLM turn.** Move `llm_callback`, script wakes, reminders and
  memory review onto the runner. Then remove the parking machinery and the watchdog's activity
  heuristic. This also supersedes milestone 3 of `turn-resumption-across-restarts.md` and the
  follow-up-turn timeouts from Tuit #99.

## Alternative considered: keep turns in the worker and fix the clocks

This means heartbeating worker activity and `locked_at` from a side task, and deriving each
threshold from the budgets table. M0 is exactly this. It fixes the current incidents, but it keeps
the turn inside a task claim. That leaves interruption as fail-not-resume, leaves long turns
occupying worker slots, and keeps two lifecycles per turn. The next new long path would need its own
budget entry and its own ordering argument.
