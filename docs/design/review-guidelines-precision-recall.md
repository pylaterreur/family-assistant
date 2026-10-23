# Review guidelines: precision and recall of automated PR review

**Status:** Analysis and decision record, October 2026. The guideline edits it decides on land in
the same change.

## What was examined

Every pull request in `werdnum/family-assistant` updated between 20 July and 23 October 2026 (465
PRs, 440 merged), with every inline review thread on them, was pulled from the GitHub API. The
automated reviewer is OpenAI Codex's native GitHub review (`chatgpt-codex-connector[bot]`), which
posted 2,031 top-level inline comments on 307 of those PRs. The in-repo reviewer script posted 40. A
human (`werdnum`) opened one inline thread in three months; the maintainer's review effort goes into
*replies* (951 of them), almost all written through a Claude Code agent on the author's side.

Each thread was classified independently by an agent that read the thread, the author's replies, the
PR's commits and, where needed, the code, into:

- **outcome**: implemented; implemented then reversed on the same PR; implemented then reversed on a
  later PR; partially implemented; rejected; unknown (no reply and no visible change);
- **validity** (the classifier's own judgement, not the author's reply): true positive; true but
  disproportionate under the behaviour-altitude rule; false positive; restates a trade-off the PR
  had already recorded.

Separately, every change in the window that fixes a defect introduced by an earlier in-window PR was
traced to the introducing PR and to what review did about it at the time.

Two caveats. Classification was done by language models reading the threads, not by the maintainer,
so the validity labels are a second opinion and the disproportionality boundary is a judgement call.
And a handful of PR branches were rebased, so some "addressed in `sha`" replies were matched by
commit message rather than by hash.

## How Codex reviews here

Codex's GitHub review reads `AGENTS.md` and cites it: 1,149 of the 2,031 comments carry an
`AGENTS.md reference: L…-L…` line. It never cites `REVIEW_GUIDELINES.md` (the file that held the
review rules until 3 October) or the `review-guidelines` skill that replaced it. OpenAI's
documentation says the reviewer applies a `## Code Review Rules` section in each applicable
`AGENTS.md`; in practice it draws on the whole file, and what it draws on most is the *development*
guidance, not the review guidance:

| AGENTS.md rule cited                            | Comments | True positive | Disproportionate |
| ----------------------------------------------- | -------: | ------------: | ---------------: |
| Prefer enforcement chokepoints over enumeration |      280 |           76% |              22% |
| No silent failures (error-handling bullets)     |      187 |           79% |              21% |
| Design docs are approach-level documents        |      151 |           75% |              24% |
| Rule of Two and trust boundaries                |      193 |           84% |              13% |
| Withdraw unrequested promises                   |       52 |           85% |              15% |
| User documentation rules                        |       46 |           93% |               2% |
| Behaviour-altitude principle                    |       37 |           89% |               8% |
| No citation                                     |      881 |           84% |              12% |

So the rules that push the reviewer toward *more* (chokepoints, no silent failures, fail fast) are
the ones it reads, and they are the ones with the highest disproportionate rate. The counterweights
that were written to stop review spirals (the proportionality gate, the cost/benefit gate, "respect
documented deliberate simplifications", "do not comment on", the threat-model gate, the forbidden
hedged-comment list) live in the skill's reference file, which the reviewer does not read. The
citations themselves are loose: the classifiers repeatedly found the same line range cited for
unrelated findings, and the "approach-level" rule cited in support of demands for more detail.

## Precision

| Validity of the 2,071 classified threads | Count | Share |
| ---------------------------------------- | ----: | ----: |
| True positive                            | 1,691 |   82% |
| True but disproportionate                |   322 |   16% |
| Restates a recorded trade-off            |    22 |    1% |
| False positive                           |    34 |    2% |

| Outcome                                   | Count | Share |
| ----------------------------------------- | ----: | ----: |
| Implemented                               | 1,680 |   81% |
| Implemented, then reversed on the same PR |   119 |    6% |
| Implemented, then reversed on a later PR  |    15 |    1% |
| Partially implemented                     |    50 |    2% |
| Rejected                                  |   142 |    7% |
| Unknown (no reply, no visible change)     |    65 |    3% |

Factual precision is high. Thirty-four false positives in two thousand comments is not the problem,
and the ones that exist have a recognisable shape: an unverified claim about a library, SDK, runtime
or API (`splice(0)` semantics, asyncio `wait()` on 3.12+, the Brave MCP tool separator, a hook
timeout unit, a stale in-repo mirror of a model catalog), or a "missing file" claim made against the
PR branch tree when the file had already merged to `main` from a companion PR.

The real precision cost is the 16% of findings that are correct and disproportionate, and it is
concentrated:

- **By round.** Findings in the first ten threads of a PR are 12% disproportionate and 3% get
  reversed on the same PR; from the 31st thread on, 28% are disproportionate and 14% are reversed.
  Every push triggers a fresh review, and 334 comments open with "fresh evidence beyond the earlier
  thread". The reviewer attacks the mechanism the previous round made the author add.
- **By target.** Design-document threads are 15% reversed-on-the-same-PR against 3% for code, and
  44% of them carry a P1 badge against 26% for code. Two design-doc PRs (#1142, #1215) account for
  66 of the 119 same-PR reversals: each added a ledger, a fence, a lifecycle, a projection; each was
  deleted when the author stepped back and simplified.
- **By rule.** The disproportionate findings cite chokepoints, no-silent-failures and the design-doc
  rule. Twelve findings cited the "stop review-fix loops at the scope boundary" rule; half of those
  were themselves disproportionate.

Three further patterns recur across almost every batch:

- **Severity badges track the rule cited, not the consequence.** 67 P1 badges sit on style,
  test-hygiene or documentation findings (a bare `noqa`, a `type: ignore` in a test, a fixed sleep,
  a doc placement). 98 of 629 P1s were disproportionate, restated or wrong.
- **Recorded decisions get re-litigated.** 22 findings restated something the PR description or
  design doc had already recorded under "Deliberate simplifications", "Not in this PR" or a filed
  issue. The skill tells reviewers not to do this; the reviewer does not read the skill.
- **Pre-existing defects are attributed to the PR.** A common rejected-but-valid shape is a real gap
  that predates the change (ownership on tasks, the non-streaming send bypass, sensitive reads on
  fold-back), where the comment neither says it is pre-existing nor sizes the scenario, and the fix
  it asks for is a migration or a new persisted state.

The 142 rejections split 71 disproportionate, 26 false positive, 18 restated trade-offs and 27 real
bugs the author declined. Most of the 27 were deferred with a rationale and several were fixed on
`main` within days (#1085, #1303, #1152), so the author side of the loop works. The 65 unanswered
threads are the weak point on the author side: about 28 of them are real and still unaddressed (iCal
bearer URLs reaching the model in #1221, an error row stalling the memory watermark in #1242, the
fallback logout not expiring the cookie in #1333).

## Recall

Fifty-eight fixes merged in the window repair a defect introduced by an earlier in-window PR.

| What review did on the introducing PR                | Fixes |
| ---------------------------------------------------- | ----: |
| Reviewer posted nothing about that code              |    29 |
| Introducing PR had no Codex review at all            |    13 |
| Defect was in code added to satisfy a review comment |     6 |
| Reviewer flagged it; the fix was inadequate          |     4 |
| Reviewer flagged it; the author declined or deferred |     4 |
| Not a defect, or unclear                             |     2 |

Of the 29 misses, 17 were only observable at runtime against a real provider, device or database:
Envoy withholding response headers, watchOS denying `NWConnection`, Siri and CallKit entitlements,
the Antigravity and Gemini API contracts, a Live-API setup flag no deployable model accepts, a
PostgreSQL plan flip, an Alembic migration silently rolled back by a session-level `SET`. Seven
needed cross-file context and five were visible in the diff. The diff-visible misses share a shape
the guidelines do not name: the *absence* of a bound (an await with no timeout, `LIMIT` after an
aggregate, a full-list refetch) or the absence of argument validation at a dispatch point.

Two recall findings are about process, not the reviewer. Thirteen fixes trace to PRs that got no
review at all; several were large plumbing PRs (#1140, #1244, #1454) whose defects surfaced weeks
later, and Codex's usage quota was exhausted around 6–7 August. And the review-induced defects (six
here, plus the 119 same-PR reversals) are the recall-side cost of the precision problem: a fix made
to satisfy a comment is pushed and re-reviewed for the comment's scenario, not for the neighbouring
failure mode the fix just created (`finish_abandoned()` in a `finally` leaving `telemetry` unbound,
source totals carried as a sum through two merges, unfinished turns held open to the 720-hour cap).

## Decisions

The reviewer is accurate and the project's own rules for proportionality are right; the problem is
that the reviewer does not see them, and that the rules it does see have no counterweight next to
them. The changes are therefore mostly about placement, with a few additions.

01. **Put the reviewer-facing rules where the reviewer reads them.** `AGENTS.md` gets a
    `## Code Review Rules` section, the heading Codex documents, carrying the compact rules that
    decide whether a finding should be posted and how it should be badged. The skill's reference
    file stays the full standard for humans, Claude and `scripts/review-changes.py`, and the two
    must not drift: the AGENTS.md section is the short form of rules the reference file states in
    full.
02. **Make the pushing rules carry their own limit.** The development bullets the reviewer cites
    most (chokepoints, no silent failures) each gain a sentence saying what they do *not* license,
    so a reviewer that reads only that bullet still sees the boundary.
03. **A severity rubric tied to consequence.** P1 (and the reference file's blocking levels) require
    a reachable failure in the shipped configuration that loses or duplicates data, crosses a trust
    boundary or breaks startup. Lint suppressions, test hygiene and documentation placement are
    never higher than P3.
04. **A rare-scenario gate a reviewer can apply mechanically.** A scenario that needs two
    independent failures, or two concurrent actions by the same user, is a documented residual, not
    a finding, unless its outcome is data loss or a trust-boundary crossing.
05. **Pre-existing versus introduced.** A finding must say whether the defect predates the change. A
    pre-existing, uncommon defect whose fix needs a migration or new persisted state is follow-up
    work, not a blocking finding on this PR.
06. **Verify before asserting external behaviour.** A claim about a library, SDK, runtime, API or
    tool-configuration semantics is posted only with the pinned source or documentation checked, and
    a "missing file" claim is checked against the base branch, not the PR head tree alone.
07. **Design documents are reviewed per revision, not per commit.** Internal consistency of a design
    doc is checked once when the author marks a revision complete; a contradiction a previous fix
    introduced is answered by restating the rule at higher altitude, not by another case; wire-level
    detail and verification-criteria tuning are not requested.
08. **Scope fail-fast.** Strict rejection applies where a malformed input would advance durable
    state; tolerant parsing is acceptable for read-only discovery of a pinned in-house service's
    response. Eval harnesses, dev CLIs and one-time scripts are reviewed for wrong results and
    committed data, not hardened against their own maintainer.
09. **Recall additions.** The review focus list names the absence of bounds (timeouts, limits,
    pagination) on new external calls, idempotency of anything a task worker may retry, and tests
    whose only evidence for an external contract is the author's own mock. The author-side rules
    require a disposition for every review thread before merge and a self-check of a review fix
    against the neighbouring failure mode.
10. **One finding per family, once.** The Nth variant of a pattern is reported as the class with a
    proposed chokepoint, not as instance N+1; a finding is not re-posted on a later commit unless
    the new head changes it.

### Deliberate simplifications

- The AGENTS.md section is a short form, not a second standard. If it and the reference file
  disagree, the reference file is wrong or the short form is stale; either is a bug to fix, not a
  precedence question.
- No attempt is made to stop Codex re-reviewing every push. That is a product setting, and the
  per-round drift is addressed by telling the reviewer what a later round should and should not do.
- Runtime-only misses are not addressed by review rules. They need a real call, a device or a
  database; the guideline asks reviewers to flag the *missing evidence* (a test that only mirrors
  the author's assumption) rather than to guess the provider's behaviour.
