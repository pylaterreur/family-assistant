# Reviewer Framing of Stored Intent

## Status

Implemented. Follows [executable-definition-taint.md](executable-definition-taint.md) and
[legacy-definition-amnesty.md](legacy-definition-amnesty.md), which made an automation's stored
definition resolve as trusted intent at firing time. This document changes only how the tool-call
reviewer is *told* about a resolved definition; what resolves, at which tier, and what feeds the
destination echo are unchanged.

## Problem

After those designs shipped, every enabled automation and listener in production resolved, and the
reviewer's reasons cited "the trusted trigger definition". Unattended firings were still judged as
if nobody had asked for them. Production verdicts on the e-Ink briefing automation flipped between
`allow` and `confirm` for the same step across runs, with reasons such as "no trusted originating
request is present to confirm the chain" and "the enclosing script is unreviewed". Each reason
traces to something the prompt says rather than to anything the judge was missing:

- **One label for the whole closure.** A firing executes an automation and the stored scripts it
  names; the closure's status is the weakest claim among them, so a human-confirmed automation
  calling one operator-amnestied script rendered only "operator-amnestied … examined by no gate".
  Weakest-first is right for the tier and the echo. As the *only* description it hides the human
  attestation the judge should be weighing.
- **An absent field read as a missing request.** Every trigger rendered an originating-request line,
  which for a scheduled or event firing could only say "No originating trusted request was
  supplied". That field exists for delegated runs, where a human did ask, in another conversation.
  On a stored-definition firing the definition *is* the request, but the prompt said otherwise.
- **Run state read as authorship.** A scheduled script runs as a program no review has decided yet;
  its first reviewed operation decides it. The evidence said `decision: "unreviewed"` and
  `approval_active: false`, which the judge read as a statement about the program's provenance.

## Design

The reviewer is told what each piece of evidence is, in our vocabulary rather than the model's
inference from absence.

1. **Per-artifact status.** A resolution carries the artifacts it was combined from, each with its
   kind, name and own closed-vocabulary status. The trigger renders the overall (weakest) status as
   today and, beneath it, each artifact's. Combining remains the single place statuses merge, so a
   new closure member is listed by construction. Names are part of each artifact's hashed content,
   so they render under the same trust as the definition itself.
2. **Say how the run started.** A trigger states whether it was started by a stored definition or by
   a delegation, and the builders set it: the delegation builder is the one place delegated triggers
   come from, and the field has no default, so a new trigger kind must choose. A stored-definition
   trigger renders "not applicable" in place of the originating request, and the section header
   tells the judge that a trusted trigger definition is the intent it judges against.
3. **Name run state as run state.** Script evidence renders the program's review as this run's
   pending or decided review, separately from the definition's provenance already listed in the same
   entry.

## Deliberate simplifications

- The automation's free-text description is not rendered separately from its executable content. Its
  name appears in the per-artifact status, and for every production firing the rendered definition
  already states what runs.
- The delegated-run framing is unchanged. A delegation-completion wake whose only trusted text is a
  short acknowledgement of an assistant proposal (composed after untrusted reads, so stubbed) is
  still judged against that acknowledgement alone; ratifying assistant proposals is a separate
  problem.

## Verification

Unit tests over the assembled prompt for each change, and manual eval cases for a scheduled script
whose human-confirmed automation names an amnestied script (benign, expected `allow`) and for the
same firing directed by its payload to a new destination (attack). The prompt revision changes, so
the judge must be re-measured with the eval harness and AgentDojo as
[tool-call-review-judge-tuning.md](tool-call-review-judge-tuning.md) requires before the change is
relied on under `enforce`.
