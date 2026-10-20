# Workhorse Eval Set: Private Evaluation of Everyday Assistant Tasks

## Status

Proposed. This is a plan for approval; nothing here is implemented.

## Goal

Build a **private, household-derived evaluation set** of the tasks the assistant actually does — the
"workhorse" workload on `default_assistant` at the `standard` tier, and the harder tail that reached
`complex_tasks` or the `deep` tier — and a harness that runs the **real agent loop** against those
tasks in a sandbox with representative state and tools, so that a model, tier, prompt or tool change
can be scored on the work it will really be asked to do.

This is an **integration-test-style eval**. The system under test is the whole application as
deployed — prompts, context providers, tool descriptions and policy, the tool-call reviewer, model
routing, the tool implementations — with the model as one parameter. A failure is as likely to be
the harness's as the model's, and the harness is built so the two are told apart (see
"Integration-test framing" below).

Questions the set should answer:

- Does a cheaper or newer model handle the standard-tier workload as well as the current one?
- Which task categories does `standard` fail on, and does `deep` fix them (the evidence Auto
  routing's shadow mode was designed to need — see
  [model-tiers.md](model-tiers.md#observability-and-evaluation))?
- Did a prompt, tool-description, tool-policy or tool-implementation change regress ordinary tasks?
- Where does the application itself get in the model's way: a tool whose description misleads, a
  reviewer that blocks ordinary work, an iteration ceiling hit on a routine task?

The guiding metric, inherited from the tiers design, is *successful useful work per dollar at
acceptable latency*.

### Configurations under test

A candidate is a **model at an effort level**, not just a model: effort is the cheaper dial to turn
before switching models, and the eval should say when it is enough. Each configuration is an
eval-only tier (see "Boot") with a **single-entry chain and no fallback**, so a trial's result is
that configuration's or a visible failure, never a stand-in's answer under its name. The production
recipes, with their routing and fallbacks, are assessed separately afterwards with the winners in
place.

First round, six models in eight configurations:

| Configuration     | Identifier          | Effort | What it answers                                                |
| ----------------- | ------------------- | ------ | -------------------------------------------------------------- |
| Flash, as shipped | `gemini-3.8-flash`  | medium | the Standard baseline                                          |
| Flash, low        | `gemini-3.8-flash`  | low    | whether routine requests finish faster with less thinking      |
| Opus, as shipped  | `claude-opus-5-5`   | high   | the Deep baseline                                              |
| Opus, medium      | `claude-opus-5-5`   | medium | whether lowering effort beats switching models on cost/latency |
| Gemini Argon      | `gemini-4-argon`    | medium | the strongest challenger for long tool sequences               |
| GPT-6.1 Sol       | `gpt-6.1-sol`       | medium | whether the browser tier's model earns a broader role          |
| Claude Sonnet     | `claude-sonnet-5-5` | medium | whether a cheaper Claude keeps enough of Opus's judgement      |
| GPT Luna          | `gpt-6-luna`        | medium | the cheap OpenAI model already on the reminder path            |

Second round, on the cases the first round failed or disagreed on: Argon at high and the Max tier's
Fable at xhigh, to test whether more spend actually rescues failures.

Effort is per chain entry (`llm_parameters`) for the Anthropic and OpenAI adapters today; the Google
adapter sends no thinking level yet, so **Flash low depends on adding that** (in progress
separately). Whatever was sent, the harness records the **effective request parameters** beside
every trial — a configuration that silently ran at the default effort would otherwise look like a
result.

The managed Antigravity coding agent (`coder`) is a different system — server-side execution, its
own effort controls — and is not in this set.

## What exists and is reused

The tool-call review eval ([tool-call-review-eval.md](tool-call-review-eval.md)) already solved the
privacy and process problems; this design borrows its conventions wholesale rather than inventing
parallel ones.

| Piece                                                | Reused for                                                             |
| ---------------------------------------------------- | ---------------------------------------------------------------------- |
| `.review-eval-local/` + `eval/private_paths.py`      | The one gitignored tree all household-derived material is confined to  |
| `scripts/extract_review_history.py`                  | The read-only history access pattern (`mode=ro`, no `init_db`, no WAL) |
| `scripts/dump_tool_registry.py`                      | Snapshot of the deployment's tool surface including MCP tools          |
| `eval/tool_call_review/scrub.py`                     | Deterministic pseudonymisation when a case has to be quoted            |
| Held-out discipline, per-slice reporting, run stamps | Same rules, same report shape                                          |
| `docs/development/eval-results/`                     | Sanitised aggregate results are published there, nothing else          |
| `tests/functional/tools/taint_enforce_harness.py`    | Booting the whole app on the shipped `defaults.yaml` with overrides    |
| `tests/integration/llm/test_shipped_coder_profile…`  | Booting with **real** LLM clients built from the shipped tiers         |
| `POST /api/v1/chat/turns` with `model_tier`          | Driving a turn exactly as a client would, on a chosen tier             |
| `message_history.reasoning_info` tier/routing stamps | Reading back resolved tier, `would_choose`, usage and cost per reply   |
| Radicale fixture, `FakeCameraBackend`, `MockScraper` | Sandbox-side real-ish services                                         |

What does **not** exist: any harness that runs `ProcessingService` with a real model against seeded
state and scores the outcome; a household fixture (notes, contacts, calendar, tasks); fakes for Home
Assistant, weather and MCP servers that the `Assistant` can be configured to use; a clock seam on
`Assistant`.

## Core concepts

- **Task** — one historical turn (or short run of turns) that a household member asked
  `default_assistant` to do: the trigger message, the profile and tier it ran on, the tools it
  called and what they returned, the reply, and the outcome signals around it.
- **Case** — a *journey*, not a recording (the review eval's rule): a user prompt, the household
  state it was asked against, the moment it was asked, and what a good outcome looks like. A case is
  evaluated against the system as it stands today.
- **Household fixture** — a snapshot of the state the assistant reads, materialised into a fresh
  sandbox per case.
- **Sandbox** — a full `Assistant` on the shipped config with real inference for the profile under
  test, in-app state that is real and inspectable, and external integrations replaced by recorded or
  fake backends that **capture** writes instead of performing them.
- **Grade** — deterministic checks on the sandbox's end state plus an LLM judge with the historical
  production reply as a reference, with seeds to measure variance.

## Part 1 — Exporting chat history and extracting tasks

### Access

Production runs PostgreSQL. Take a **one-off, table-restricted dump** into a local database kept
under `.review-eval-local/workhorse/snapshot/` rather than running extraction against production
repeatedly: the household fixture (Part 3) needs several tables beyond `message_history`, and a
local copy makes every later step reproducible and safe to iterate on. The dump is read with the
same read-only rules the extraction script already enforces. Tables: `message_history`, notes,
memory notes, tasks, users and contacts, automations, and the documents index metadata (not blobs).

### Export: turn records

A new script, a sibling of `extract_review_history.py`, walks `message_history` by `turn_id` for
`default_assistant` and `complex_tasks` — including turns `default_assistant` delegated to
`complex_tasks`, and turns a user escalated with `/deep` — and emits one **turn record** per turn
into the private tree. The hard tail is in scope on purpose: those turns are where a standard-tier
candidate is most likely to differ, and where a harness bug (an iteration ceiling, a tool that
returns too much) is most likely to show. Each record holds:

- the trigger: user message text, attachments metadata, interface, user id, timestamp;
- the run envelope from `reasoning_info`: resolved tier, requested tier and source, routing outcome
  and `would_choose`, serving model, token usage, iteration count, wall-clock;
- the trace: each tool call (name, arguments) and its result row, confirmation requests, error rows;
- the reply text;
- **outcome signals** computed from the surrounding rows: whether the user replied within a short
  window and whether that reply reads as a correction or retry ("no, I meant…", the same request
  reissued, a `/deep` escalation), whether the turn ended in an error row, whether a tool call was
  denied or needed confirmation.

Unlike the review-eval templates, these records hold raw household text. That is deliberate and
matches the review eval's decision for its run records: evaluation needs byte fidelity, the private
tree exists to hold exactly this, and nothing in it is ever committed. The script refuses any output
path outside the tree.

### Stratify and sample

A turn record is tagged along a few axes, then the set is sampled across strata so that it
represents the workload rather than its most common shape:

- **tool signature** — the set of tool families called (none; notes; calendar read; calendar write;
  tasks; search/retrieval; smart home; messaging; web; MCP-backed);
- **intent category** — a closed vocabulary (lookup, create, update, schedule, summarise, remind,
  multi-step) filled by the same cheap-classifier pattern the review-eval history pipeline uses; the
  classifier sees the record but its output is only ever a label;
- **difficulty proxies** — iteration count, tool-call count, `would_choose`, and the outcome
  signals; failures and escalations are over-sampled on purpose, because a set made only of turns
  that went fine cannot discriminate between models.

Curation then checks the sample against a short list of **coverage areas**, each needing both
ordinary successes and known failures, and each asking one thing of the system:

| Area                          | Cases to include                                                                           | Question                                                              |
| ----------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- |
| Retrieval and grounding       | facts spread across notes, calendar, email, documents; stale records; near-duplicate names | finds the complete, current answer and cites the real source          |
| Cross-tool completion         | dependent actions, pagination, related-record updates, partial failures                    | reaches the intended end state with no omissions or unrelated changes |
| Analysis and judgement        | conflicting evidence, competing constraints, comparisons needing a recommendation          | resolves what matters and separates fact from assumption              |
| Memory and continuity         | follow-ups, corrected facts, changed preferences, resumed work, compacted history          | applies corrections without resurrecting stale state                  |
| Delegation and tier selection | simple requests that should stay local; hard ones that need another profile                | picks a useful profile or tier and carries the task through           |
| Easy with room to overwork    | one-record lookups, a simple reminder, a question with one authoritative answer            | stops when the task is done instead of buying quality with checking   |
| Multimodal                    | a handful of photo, screenshot and PDF turns, if the traffic warrants it                   | the whole path extracts the right information                         |

Two areas are deliberately thin here. Authority and trust — instructions in retrieved content,
ambiguous authorisation — is the tool-call review eval's job; this set keeps only a few **benign
requests that resemble risky ones**, to measure the friction the reviewer adds to legitimate work.
Engineering investigation (`engineer` profile) needs a different sandbox — a source tree,
diagnostics endpoints, logs — and is a later set built on the same harness.

The output is a ranked candidate list with frequency per stratum, for the maintainer to skim. Target
for the first version: about 60 cases, with every stratum that has non-trivial frequency
represented, roughly a third drawn from turns with a negative outcome signal, and several short
multi-turn sequences.

### Curate into cases

A case file (YAML, private tree) holds:

- the prompt and any prelude turns needed for context (a follow-up like "move it to Thursday" is
  meaningless without the turn before it);
- the asked-at timestamp and the user;
- a reference to the household fixture revision and the **recorded tool results** for the turn;
- expectations: deterministic assertions on end state (a calendar event exists whose summary
  matches; a note was updated; **no** message was sent) and a short rubric for the judge, plus the
  historical reply as a reference answer;
- lineage: source turn id, strata labels, dev/gate split.

Curation is manual on top of the sampled list. Most of the work is writing expectations; the
extraction gives the maintainer the trace and reply so they can be derived in a minute each.

## Part 2 — The sandbox

### Boot

Boot a real `Assistant` on `defaults.yaml` with the `taint_enforce_harness` override pattern:
sandbox database, storage paths, no Telegram, deterministic embedder, and **no
`llm_client_overrides` for the profile under test**, so it runs the real client built from the tier
being evaluated. The model router, summariser and tool-call reviewer run their configured models
too: they are part of the system under test, and a reviewer that asks for confirmation is a cost the
eval should see. Provider keys come from the environment as for the LLM integration tests.

The candidate model is expressed as an **eval-only tier**: the harness's config overlay adds one
tier per candidate (`eval_claude_sonnet`, say) to `model_tiers` and widens the profile's
`allowed_model_tiers` to include it, then names that tier on the request. `defaults.yaml` is not
edited, the profile's own default tier is untouched, and a candidate without a configured adapter
fails at boot rather than silently running something else — the same rule the tiers design applies
to presets.

### Integration-test framing

The harness is a **pytest suite**, not a standalone script: one parametrised test per case and
candidate tier, living beside the LLM integration tests and carrying a marker that `pytest.ini`
excludes by default, so it runs only when asked and never in ordinary CI. Three things follow.

- **It reuses the test fixtures.** The database engine fixtures, the Radicale server, the server
  socket and wait helpers, the Gemini and VCR replay plumbing, and xdist parallelism are all already
  there, and every eval run exercises them the way the functional tests do.
- **A failure says whose it is.** Every trial ends in one of a closed set of outcomes before any
  grading happens: `completed`, `tool_error` (a tool raised), `unbacked_tool` (the sandbox had no
  backing for a tool the model called), `reviewer_blocked`, `confirmation_declined`,
  `iteration_limit`, `timeout`, `provider_error`. Only `completed` trials are graded for quality.
  The rest are reported as harness or application findings, broken out per tool and per candidate,
  so a tool that errors under every model is filed as a bug in the tool, and one that errors under
  one model as a finding about how that model calls it. A run whose non-`completed` rate exceeds a
  small threshold is a harness failure first and a model result second.
- **The harness has its own regression tests.** A **self-test slice** runs the whole pipeline —
  boot, fixture materialisation, re-dating, tool backing, turn driving, deterministic grading — with
  the rule-based mock LLM scripted to call tools, on a small committed synthetic household rather
  than the private fixture. It runs in ordinary CI, so the harness cannot rot between eval runs, and
  a change to a tool, the chat API or the reviewer that breaks the harness fails a test rather than
  a paid run.

Each case gets a **fresh sandbox**: the database is cloned from a prepared fixture template (the
SQLite schema-template trick, applied to a seeded database), so cases are independent and can run in
parallel.

### Household fixture

The fixture is produced once from the snapshot and revisioned: notes, memory notes, contacts and
known users, tasks, automations, and a calendar. It is the household's real data, kept private,
because representative results need representative context — the assistant's behaviour on "add it to
the shopping list" depends on there being a shopping list note in the house style.

- **In-app state** (notes, tasks, memory, documents metadata) is loaded into the sandbox database.
- **Calendar** is served by the Radicale fixture already used in tests, seeded from the exported
  events.
- **Time.** Rather than running turns "as of" a past date — `Assistant` has no clock seam and
  roughly two hundred direct wall-clock reads sit beneath it — the fixture is **re-dated**: every
  timestamp is shifted by a whole number of weeks so that the case's asked-at moment lands on the
  current date and weekdays are preserved. The case stores the original timestamp; the materialiser
  computes the shift at run time. This gives correct relative-time behaviour ("what's on tomorrow",
  "remind me Friday") without time travel. Adding a clock seam to `Assistant` is a worthwhile
  follow-up for precision, not a prerequisite.

### Tool surface

The registry snapshot from `dump_tool_registry.py` is the contract: the sandbox must present the
same tool names and schemas the deployment did, or the case is not measuring the same task. Three
classes of tool, by what backs them:

1. **In-app tools** (notes, tasks, memory, calendar via Radicale, scheduling, search over the seeded
   index) run for real against the sandbox. Their effects are the end state the grader inspects.
2. **External reads** (weather, Home Assistant state, web fetch, MCP-backed lookups such as the task
   tracker) are served by a **replay backend**: the case's recorded tool results, matched on tool
   name and a loose argument match, with a declared "plausible empty" fallback per tool when the
   model asks something the recording cannot answer. The fallback is logged on the trial so a grader
   can discount it.
3. **External writes** (send message, email, Home Assistant service calls, anything that leaves the
   sandbox) go to **capture backends** that record the call and return a success-shaped result.
   Captured calls are part of the end state and are what "no message was sent" assertions check.

This is the one place the sandbox needs new seams. Concretely: a generic fake MCP server process
that serves a registry snapshot with replay/capture semantics covers every MCP-backed tool in one
mechanism; the `Assistant` needs config-selectable fake backends for the weather service and Home
Assistant (there is no constructor seam today) and the outbound messaging paths. Prefer one
replay/capture mechanism keyed by tool name over per-integration fakes, so a tool added later either
has a recording or fails loudly as "unbacked", never silently succeeds.

### Driving a turn

The runner POSTs to `/api/v1/chat/turns` with the case's prompt, `profile_id`, the configured
`model_tier`, and `interface_type: "api"` as the acting user, follows the stream to completion,
answers confirmation prompts with a per-case policy (default: approve, and count it), and then
collects: reply text, the tool-call sequence, confirmations, errors, iteration count, the
`reasoning_info` envelope (tier, serving model, usage), latency, and the sandbox state diff.

## Part 3 — Grading and reporting

Three layers, cheapest first:

1. **Deterministic checks** from the case's expectations against the end state: required effects
   happened, forbidden effects did not, the turn completed without error. These decide pass/fail for
   the side-effect half of the task.
2. **LLM judge** for the reply: a rubric score (did it do the thing, is the information correct
   against the fixture, is it concise) and a **pairwise preference** against the historical
   production reply, with position swapped. The judge runs on a strong model from a different
   provider than the one under test. Judge prompts are versioned and their hash is stamped on the
   run.
3. **Human spot-check** of a fixed sample per run, recorded as agreement with the judge, so judge
   drift is visible.

Every case runs with several seeds (three to start); seed disagreement is reported per case the way
the review eval reports verdict flips. The judge is blinded to which configuration produced a reply,
and presentation quality is scored as its own line so polished writing cannot hide a wrong result.

### Scorecard

Per configuration and per stratum, in this order of importance:

1. **Correct completion** — the primary metric. Required outcomes achieved with the right people,
   records, dates and quantities; no unintended changes; the reply's claims agree with what actually
   happened. Authority violations and wrong side effects are explicit failures, never averaged in.
2. **Spend per successful task** — cost per attempt and cost per success (all spend across every
   attempt, divided by successes), counting the parent turn, every delegation subconversation,
   retries and failed attempts. The application records token usage, not money, so the harness keeps
   the **raw usage per call** and prices it at report time from a versioned price table; results can
   be repriced later without rerunning (Argon's introductory pricing is the near example).
3. **Experienced latency** — time to first useful token or action, time to completion, median and
   tail with sample counts, and model wait versus tool wait, so a fast acknowledgement cannot make a
   slow task look responsive. Also the fraction of tasks completed within a chosen time budget.
4. **Supervision required** — clarifying questions that were unnecessary, confirmations asked,
   corrections the user would have had to make, manual recovery.
5. **Efficiency and stopping** — model rounds, tool calls, repeated searches, retries, delegations;
   whether extra verification changed correctness and whether the model stopped once the task was
   adequately done. This is the layer that explains *why* a configuration costs more.
6. **Answer usefulness** — the judge's rubric: correctness, completeness, evidence, clarity,
   appropriate uncertainty, with presentation separate.

A run report, written to the private tree, carries all of this plus the harness findings (outcome
taxonomy, unbacked-tool fallbacks hit). A **sanitised summary** (counts, rates, strata labels, no
text) is what gets published under `docs/development/eval-results/`, in the existing shape. The
report the maintainer actually wants is the success–cost–latency table per profile and the routing
decisions it supports: which requests Flash handles adequately, where lower effort pays off, where
Argon earns its cost, where Opus still materially improves the outcome.

Held-out discipline follows the review eval: the case set is split into dev and gate slices by
stratum at curation time; prompt and routing tuning uses dev; a model-switch decision reads gate,
and reading it spends it.

## Work plan

Each milestone is independently useful and verifiable.

1. **Snapshot and export.** The table-restricted dump procedure and the turn-record export script,
   with outcome-signal computation. Verified by running it on the snapshot and on a dev SQLite
   database, checking the private-path refusal, and eyeballing record counts per stratum. Outcome: a
   ranked candidate list to curate from.
2. **Case schema and first curated set.** The case model (pydantic, `extra="forbid"`, slug ids,
   private-tree loader) and about 60 hand-curated cases with expectations, roughly a quarter from
   `complex_tasks` and `/deep` turns. Verified by loading all cases and round-tripping them; a case
   with text in an id or an unknown field fails to load.
3. **Sandbox boot and fixture materialisation.** Shipped-config boot with real clients and the
   eval-only tier overlay, fixture template with re-dating, Radicale seeding, per-case clone, as a
   pytest suite behind an opt-in marker. Verified by the first self-test: one synthetic case with
   the mock LLM scripted to call a calendar tool and the end state showing the event, running in CI.
4. **Tool backing.** Replay/capture mechanism for external reads and writes, the fake MCP server,
   and the config seams for weather, Home Assistant and messaging. Verified by a conformance check
   that every tool in the registry snapshot is either in-app, replay-backed, or capture-backed for
   every case, failing the run otherwise, and by self-test cases for each backing class.
5. **Runner, outcome classification and deterministic grading.** Drive turns, collect traces,
   classify each trial's outcome, apply the checks, write run records. Verified by running the dev
   slice on the current `standard` tier, inspecting a handful of traces by hand, and fixing every
   harness-attributed outcome before any model is judged on the set.
6. **Judge, seeds and report.** Rubric and pairwise judging, seed flips, per-stratum report,
   sanitised publish. Verified by a judge-vs-human agreement check on the spot-check sample.
7. **First real comparison.** Run the first-round configurations on the gate slice; publish a
   sanitised result. Outcome: the first evidence for the tiers design's open question of whether
   `deep` improves the categories Auto would route there, the first ranking of candidates on the
   real workload, and the list of cases the second round (Argon high, Fable xhigh) should target.

## Deliberate simplifications

- **No time travel.** Re-dating the fixture by whole weeks gives correct relative dates for the
  overwhelming majority of tasks; a task that depends on an absolute historical date is mis-measured
  and should be excluded at curation, not special-cased.
- **Replay is loose, and fallbacks are declared.** A model that phrases a lookup differently from
  the recording gets the plausible-empty result and the trial is flagged. This measures the agent
  against slightly less helpful external services than production, which biases *against* the system
  under test uniformly across models and is acceptable for comparisons.
- **Single-turn by default.** Multi-turn journeys are supported through preludes, but the first set
  is dominated by single turns because that is what the workload is and what can be graded cleanly.
- **Raw text lives in the private tree.** No pseudonymisation on the hot path; the existing scrubber
  is used only when quoting a case outside it.
- **Judge preference against history is a signal, not ground truth.** The historical reply came from
  a model that may itself have been wrong; the deterministic checks and the spot-check are what
  anchor it.

## Decisions taken

- **`complex_tasks` is in scope** from the first set, as the hard tail.
- **The harness is as much under test as the model.** Hence the pytest form, the closed outcome
  taxonomy that separates harness and application failures from model failures, and the self-test
  slice in CI.
- **The tool-call reviewer runs its configured model** during eval runs; confirmations are counted
  as friction rather than pinned away.
- **Candidates are model-at-effort configurations with no fallback**, assessed on the real prompts,
  tools and adapters; the production recipes with routing and fallback are assessed after, with the
  winners in place.
- **First round:** Flash medium and low, Opus high and medium, Argon, GPT-6.1 Sol, Sonnet and Luna;
  second round adds Argon high and Fable xhigh on the failures.
- **Engineer and coder are out of this set.** Engineer gets a later set on the same harness;
  Antigravity coding is a different system.

## Open questions

- Whether a `complex_tasks` case runs on that profile (its prompt and iteration ceiling) with the
  candidate as its tier, or on `default_assistant` to ask whether the delegation was needed at all.
  Proposed: both, as two cases sharing one journey, since the second is the question Auto routing
  exists to answer.
