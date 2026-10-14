# Tool call activity display (web and iOS)

## Problem

A turn's tool calls render as one collapsible box whose header names categories ("2 notes and 1
document", "Query trino, Search notes +2"). It says which _areas_ were touched, not what the
assistant is doing or did, so a running turn reads as a spinner over a list of nouns.

Placement is also wrong while a reply streams. Both clients accumulate a turn's text into one string
and append every tool call after it, so text the assistant wrote _after_ looking something up is
drawn _above_ the lookup. The web client only recovers the true order when history reloads; iOS
loses it there too, because merging a turn's tool-call messages concatenates their text.

## What other harnesses do

Surveyed: Claude.ai, ChatGPT, Claude Code, Cursor, Copilot agent mode, Perplexity, Gemini, Vercel AI
Elements, assistant-ui. The patterns they converge on:

- **A call is a verb phrase with its key argument**, present tense while running and past tense when
  done: "Searching notes for 'dentist'" → "Searched notes for 'dentist'". Claude Code's
  `Read(file)`, Cursor's "Grepped streamlit", Codex's "Ran `cmd`".
- **The collapsed header is a live ticker** while work is in flight: one line, replaced in place,
  naming the current action with a shimmer or spinner (ChatGPT "Thinking", Claude.ai, Perplexity
  steps). It does not grow a stack of rows.
- **When finished it collapses to a summary of what happened**, not where: "Searched 12 sites",
  "Worked for 12s", "Explored 3 files".
- **Groups sit inline where they happened.** Only consecutive calls group; text between calls ends
  one group and starts the next (assistant-ui, Claude Code, Codex). Nobody hoists all activity above
  or below the answer.
- **Three detail levels**: header → per-call rows → raw arguments/result.
- **Auto-open for exceptions only.** Failures, denials and approval requests stay visible; a group
  the user toggled stays as they left it.

## Design

### 1. One phrase per call, produced by the backend

Every tool call carries a `display` block —
`{"active": "Searching notes for “dentist”", "done": "Searched notes for “dentist”"}` — computed
once on the server and attached wherever a tool call is serialized for a client: the `tool_call` SSE
event and the history API's `tool_calls`. Both clients (and the shared-conversation page) render it
verbatim and own no phrasing logic.

The phrasing lives beside the tool definitions as a registry of templates keyed by tool name, filled
from the call's arguments. A tool without a template falls back to its humanised name plus the
existing primary-argument summary, so an unregistered tool degrades to today's per-call text rather
than failing. A conformance test lists tools without templates so coverage is visible, not enforced;
the fallback is acceptable for rarely used tools.

Argument values are user/LLM-controlled text, so the phrase is plain text, truncated, and never
rendered as markdown or HTML.

Why the server: there are three renderers (web thread, web shared page, iOS) and the web one already
carries a curated label map that iOS lacks. One table keeps them identical and lets a new tool get a
good phrase in the same change that adds it.

### 2. Collapsed header

- **Running**: spinner + the `active` phrase of the most recent unfinished call, replaced in place
  as calls start and finish, plus how many have finished ("2 done").
- **Finished**: the `done` phrase of the most significant call, plus "and N more". Significance is
  coarse: a call that changed state (created, sent, deleted, scheduled) outranks a read, and among
  equals the latest wins. "Created task “Call plumber” and 3 more" says more than "Searched notes".
- **Awaiting approval**: "Needs your approval: …" ahead of anything else.
- **Any call not succeeded**: the existing "N didn't finish" count stays, with a warning icon.

A group opens by itself only for an approval. It no longer opens while calls run — the header is the
live view — and a call that did not finish is counted rather than opened, since a historical call
with no recorded result would otherwise open every old group.

Expanded, each call is one row: status icon, the phrase, a result snippet. Tapping a row reveals the
raw arguments and result, as the per-call details do today. Category icons are removed from the
header; the phrase carries the meaning.

### 3. Chronological placement

A turn reads as text, tool group, text, … in the order it happened, on both clients, in both
streaming and history. Both clients use the same mechanism: each tool call records how much of its
message's text had arrived when it started, and the renderer splits the text at those offsets.
Whitespace between two calls is dropped so it does not split their group. The message keeps one text
and one list of calls, so copy, search and live-to-persisted reconciliation are untouched.

- **Web**: the streaming hook stamps each call with the offset; the message's parts are rebuilt as
  text/call/text, and assistant-ui's consecutive-call grouping places each group where it happened.
- **iOS**: the stream stamps each call with the offset (counting text still buffered for the flush
  timer); a history row's calls sit after its own text, and merging a turn's tool-call rows shifts
  the later row's offsets past the earlier text. The bubble renders the derived segments in order,
  each tool group with its own disclosure state.

## Work plan

1. **Backend phrase** — `display` on SSE `tool_call` events and history `tool_calls`, with templates
   for the household tools (notes, calendar, documents, tasks/callbacks, Home Assistant, messaging,
   delegation, web). Verified by unit tests on the templates and fallback, and an API test that both
   paths carry it.
2. **Web** — ordered streaming parts and the new header/rows, used by both the live thread and the
   shared page. Verified by updated component tests, a streaming test asserting text-after-tools
   order, and the Playwright tool-grouping test.
3. **iOS** — offsets through streaming and merge, segment rendering, new header/rows. Verified by
   updated `ToolRenderingTests` (ordering through streaming and history merge) and the layout budget
   tests.
4. **Docs** — `docs/user` chat guide describes what the activity line shows.

## Deliberate simplifications

- Phrases are not stored per message; they are recomputed from the stored call at serialization, so
  a template change rewrites how old turns read. That is desirable for a display string.
- No durations ("Worked for 12s"): stream events carry no timestamps and the value is cosmetic.
- No folding of read-only calls into "Explored N" sub-summaries (Cursor); household turns rarely
  make more than a handful of calls.
