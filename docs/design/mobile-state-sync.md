# Mobile state synchronization over unreliable networks

**Status:** Draft for review (plan only, nothing implemented). **Scope:** how the iOS and web
clients learn what changed on the server: conversations, Home and threads, messages, running turns,
read markers and pending confirmations. Voice (its own WebSocket) and Telegram are out of scope.

Supersedes the sync parts of [ios-state-sync-improvements.md](ios-state-sync-improvements.md),
[ios-chat-live-resilience.md](ios-chat-live-resilience.md),
[web-chat-stream-resume.md](web-chat-stream-resume.md) and
[ios-conversation-list-freshness.md](ios-conversation-list-freshness.md). Those documents stay as
the record of how the current design was reached; this one replaces their approach.

## 1. Problem

Two symptoms, reported from use:

1. On airplane and hotel Wi-Fi (high latency, loss, long stalls, hostile proxies) the app is slow to
   update, shows stale state, and sometimes needs a pull-to-refresh or a re-open to recover.
2. The push notification for a finished reply arrives **before the app shows the reply**, even on a
   good connection.

Both come from the same shape: the client's correctness depends on a long-lived event stream, and
every recovery path is a chain of sequential round trips gated behind that stream's health.

### 1.1 How the current design behaves (facts, with the code they come from)

**Server.** `ConversationStreamHub` is an in-memory, single-process broker: per-conversation ring
buffer of typed events with a monotonic `seq` (resume via `from_seq`, 410 when evicted), an
account-level "activity" fan-out with **no replay**, 30 s heartbeats, nothing persisted. There is no
`conversations` table and no revision or `updated_at` anywhere on message state; `message_history`
has a monotonic `internal_id` and a `turn_id` but no timestamps beyond `timestamp`, and rows are
mutated in place (attachments, error traceback, interface id) and deleted/re-inserted on move to a
thread. The REST list and messages endpoints page by offset and by timestamp; nothing offers
"changes since".

**Push.** For a web/iOS turn the producer persists the reply, publishes `turn_ended`, then waits a
**2 s ack grace** (`turn_producer.py`, `DEFAULT_ACK_GRACE_SECONDS`) and sends a push only if no
subscriber acked. The APNs payload is **alert-only**: no `content-available`, no `thread-id`, no
collapse id, no `turn_id`. The app declares only the `audio` and `voip` background modes and has no
`didReceiveRemoteNotification` handler, so a push received while backgrounded does nothing until the
user opens the app. Messages created outside a turn (`WebChatInterface.send_message`, scheduled
callbacks) push immediately with no ack check.

**iOS client.** Per `ChatViewModel` (and there are two of them live when a conversation is pushed
over Home): a per-conversation follow SSE stream, an account activity SSE stream, a per-send
`follow=false` stream with its own resume loop, a 15 s pending-approvals poll that **keeps running
in the background**, a 15 s pending-delegations poll, mark-read, ack. The transport is
`URLSession.shared` with system defaults: 60 s idle timeout, no `waitsForConnectivity`, no watchdog
on heartbeats (the follow handler ignores them). Nothing is cached on disk; every cold launch
refetches profiles, the whole paged list and full message history.

A foreground resume into a conversation runs `ResyncOrchestrator`: await old tasks → auth gate →
establish follow stream (8 s timeout) → establish activity stream (8 s timeout, sequential) → full
list (paged) → messages delta → drain buffer → restart both stream loops → recent-list page. Then
each restarted loop runs its own catch-up (delegations → messages delta → recent list). **Five
sequential round trips before the resync completes, about nine on the longest path, plus two stream
connects, all doubled with two view models.** At airplane-Wi-Fi latencies (500–1500 ms RTT, stalls)
that is 5–15 s when everything works, and each stalled stream connect adds its 8 s timeout.

In-memory state is one `[ChatMessage]` array with five id prefixes (`msg_`, `local_user_`,
`local_assistant_`, `local_follow_`, `local_user_input_`) and side maps (`liveFollowBubbleByTurnID`,
`endedTurnIDs`, `knownRunningTurnIDs`, `highestAppliedSeq`). `mergeNewMessages` drops every `local_`
row and re-adds held bubbles; a dozen guards (`isSendActivelyStreaming`, selection re-checks,
generation fences) protect that merge from the three concurrent writers (send stream, follow stream,
snapshots). §9 of the previous design document records six review rounds of fixes to interleavings
of those writers. That is the machinery-edge-case spiral the review guidelines warn about, and it is
a signal about the design, not the implementation.

### 1.2 Why the notification beats the app

The push is sent **only when the app did not ack**, so by construction the push arrives exactly when
the app's stream is not working. On a good connection that means the follow stream is a zombie: open
at the TCP level but delivering nothing (the production front door has a documented history of
buffering or severing SSE; the heartbeats never reached clients). The app discovers that only via
the 60 s `URLSession` idle timeout, then a 2–30 s backoff, then a three-request catch-up. APNs
delivers in one to two seconds. If the app is backgrounded the gap is unbounded: nothing happens
until the next foreground, and then the five-to-nine round trip resync runs.

The foreground push hint path (`willPresent` → `pendingPushHint` → `targetedRefresh`, two round
trips) is the one fast path the app has, which is why the reply appears a moment *after* the banner.

### 1.3 What the previous rounds got right

- Durable turns with a client `turn_id` and idempotent `POST /turns`. Keep.
- Persisted rows tagged with `turn_id`. Keep; it is the join key for progress-to-row reconciliation.
- "A drop is a resumable condition, not an error." Keep as principle; drop the mechanism.
- Operation-aware error surfacing (never a modal for background reads). Keep.

## 2. Design principles

1. **Correctness never depends on a long-lived connection.** One idempotent, short request converges
   the client from any state. Streams are a latency optimisation layered on top and carry nothing
   the sync request does not also carry.
2. **State, not events.** The server hands out compacted *changes* against a durable per-account
   cursor, and the client has exactly one reducer. Replaying a token log is not how a client catches
   up; it fetches the current state of what changed.
3. **The client persists what it knows.** Materialised state plus the cursor live on disk. Cold
   launch renders immediately and then syncs. Offline reading works.
4. **Every signal is just "sync now".** Foreground, push, reachability change, stream frame,
   watchdog, periodic fallback: all collapse into a single-flight, coalesced sync. There is no
   separate "resync" procedure, no establish/buffer/drain ordering, no catch-up per loop.
5. **Push is a sync trigger first and a banner second.** Whether to show the banner is decided on
   the device, which knows what is on screen. The server does not infer the client's connectivity.
6. **Budget for bad networks.** Short request timeouts, one connection class for sync, small
   responses, nothing sequential that can be parallel, nothing that runs while backgrounded.

## 3. Server design

### 3.1 Change log (the chokepoint)

A `sync_changes` table: `id` (autoincrement; **the cursor**), `conversation_id`, `kind`
(`conversation`, `message`, `thread`, `read_marker`, `confirmation`, `delegation`), `entity_id`,
`op` (`upsert` | `delete`), `committed_at`. Rows are written **inside the same transaction** as the
state change, at the repository layer, so every writer feeds sync without enumeration: web turns,
Telegram, the task worker, scheduled callbacks, move-to-thread (which becomes tombstones plus
upserts), in-place row mutations (attachments, tracebacks). Writing a message-history row without a
change row is impossible by construction, because the repository method is the only writer.

Scoping follows the list endpoint's ownership predicate at read time: a sync request returns changes
for conversations the caller owns, resolved with the same SQL filter `GET /conversations` already
uses. The change log records `conversation_id`, not a user, so ownership rules stay in one place.

Retention: rows older than a bound (say 30 days) are pruned; a cursor older than the oldest retained
row gets `410` and the client does a full bootstrap sync. Same contract as today's 410, but the
bootstrap is one request, not a different code path.

The repository already has a precedent for this shape in `memory_change_log`.

### 3.2 The sync endpoint

`GET /api/v1/sync?cursor=N&focus=<conversation_id>[,…]&wait=<seconds>&limit=<n>`

Response: `{cursor, server_time, has_more, changes: [...], active_turns: [...]}`.

- `changes` is **compacted**: at most one entry per `(kind, entity_id)` since the cursor, carrying
  the entity's current state (or a tombstone). A list row for a conversation (preview, last
  timestamp, unread count, kind), a thread row, a read marker, a pending confirmation. **Message
  rows are included only for `focus` conversations**; for the rest the conversation row is enough
  and the client fetches history when the user opens it. Bounded by `limit` with `has_more`.
- `active_turns` is a **progress snapshot** per running turn in a focus conversation: `turn_id`,
  `progress_seq`, and the accumulated parts so far (text, tool calls, tool results, confirmation
  requests). The producer maintains this materialised view as it streams; today it only appends to
  the ring buffer. A client that syncs mid-turn renders the whole partial reply, not the tail from
  the connect point (closing the "mid-sentence" limitation recorded in the live-resilience doc).
- `cursor=0` is the bootstrap: the full list, focus history (last page), active turns. `limit` and
  `has_more` keep each response small enough to survive a lossy link.
- `wait` turns the same request into a **long poll**: the server holds the response up to `wait`
  seconds until a change for this account commits (woken in-process by the hub, which becomes a
  wake-up broker rather than a replay buffer), then returns. An empty response after `wait` is the
  heartbeat. One request shape serves "poll now" and "wait for change", and a completed HTTP
  response traverses every proxy that has ever broken SSE here.

History paging for a focus conversation uses `internal_id` as the cursor (`before=<internal_id>`),
not timestamps.

### 3.3 Live channel

`GET /api/v1/sync/stream?cursor=N&focus=…` is **one SSE stream per account** (not per conversation)
that emits the **same frames the sync response contains**: a `changes` frame with the new cursor,
and `turn_progress` frames for focus turns. A progress frame carries `turn_id`, `progress_seq` and
the delta (appended text, a new tool call); the client applies it only if `progress_seq` is
contiguous and otherwise asks for a snapshot via sync. Heartbeat every 10 s; the client treats 25 s
of silence as death and falls back to the long poll. There is no replay from this stream: resumption
is always a sync request.

Whether to keep this stream at all is a decision for the benchmark (§7, decision D1). The long poll
with `wait` is the baseline and is sufficient on its own: when a turn is active the server returns
as soon as progress changes (coalesced to ~250 ms), so a long-poll client renders in chunks at one
RTT per chunk. Token-by-token smoothness on a good network is the only thing SSE adds.

### 3.4 Push

- Always send the push for a completed turn, immediately after commit. Remove the 2 s ack grace, the
  ack endpoint and `wait_for_delivery`. The device decides presentation (§4.4).
- APNs payload gains `content-available: 1` (so the app wakes to sync while backgrounded),
  `thread-id` = conversation (grouping), `apns-collapse-id` = conversation (a newer reply replaces
  an unread older banner), and `turn_id` + `sync_cursor` in the custom data, so the client can tell
  whether it already has the state the push announces.
- Web Push payload gains the same data and a `topic` (collapse) header.
- Pushes for non-turn changes are not added. A read marker from another device, a thread closing:
  these arrive on the next sync. Behaviour-altitude: reasonable, not ideal, for a rare case.

### 3.5 What goes away on the server (M6)

Per-conversation `GET …/stream`, the activity stream, `POST /ack`, the ring-buffer replay and its
410 body, `active_turns` on `GET …/messages`, the `message` hub event, and the
`follow`/`event_types` query surface. The hub shrinks to: the one-turn-per-conversation reservation,
the progress materialisation, and in-process wake-ups for long polls and the live stream.

## 4. Client design (iOS; web mirrors it)

### 4.1 One engine per account

`SyncEngine` is a transport-agnostic module owned by the app, not by a view model. It has four
parts:

- **Store**: materialised state (conversation rows, Home and threads, messages per conversation,
  active turn progress, read markers, pending confirmations) plus the cursor. Persisted to disk; the
  UI observes it. Views no longer own network state, so pushing a conversation over Home no longer
  doubles every connection.
- **Reducer**: `apply(SyncResponse)` and `apply(StreamFrame)`. Upsert by `(kind, entity_id)`, delete
  on tombstone, replace progress on snapshot, append progress on contiguous delta. No id prefixes,
  no `local_` filtering, no held-bubble maps: a running turn is rendered from `active_turns` and
  disappears from there when its persisted rows (same `turn_id`) arrive in the same response. The
  reducer is a pure function; its tests are tables.
- **Scheduler**: a single-flight `syncNow(reason)` with coalescing. Triggers: foreground, push
  received (any state), reachability satisfied, stream frame announcing a cursor ahead of ours,
  watchdog (no frame in 25 s), periodic fallback while the stream is down (15 s, backing off to 60
  s), outbox flush. Backgrounding cancels the stream and the fallback timer and nothing else; there
  is no teardown/reattach sequence to get wrong.
- **Outbox**: persisted commands with client ids: `send_turn(turn_id, …)`, `confirm_tool`,
  `mark_read`, `steer`, `stop`. Flushed on connectivity and after each sync; retried with the same
  id (the server is already idempotent on `turn_id`). The UI renders pending sends from the outbox,
  which is where "optimistic" rows actually live, so the merge problem that spawned the guard zoo
  does not exist: persisted state and pending commands are different tables, reconciled by `turn_id`
  when the server echoes the turn.

### 4.2 Transport budget

A dedicated `URLSession` for sync: `timeoutIntervalForRequest` 15 s for sync (plus `wait` for a long
poll), `waitsForConnectivity` off (the scheduler handles it), `multipathServiceType = .handover`
(needs the multipath entitlement; if Apple declines it, the reachability trigger covers the
hand-over case at one extra round trip), HTTP/2 so the long poll and an outbox POST share one
connection. Responses are small by construction (§3.2) and gzip-compressed (sync responses are plain
JSON; add `GZipMiddleware` scoped to non-stream routes).

### 4.3 Presentation state

Derived from the store, not from channel health: `lastSuccessfulSyncAt`, `outbox.count`,
reachability. Rendered as "Updated just now" (hidden), "Reconnecting…" (no successful sync for

> 30 s while foregrounded), "Offline, 2 queued" (reachability unsatisfied or outbox non-empty with
> failures). The five-state indicator, its grace period and manual-reconnect tap go.

### 4.4 Push handling

- `willPresent` (foreground): trigger `syncNow(.push)`; suppress the banner if the push's
  conversation is on screen; otherwise show it. The reply is on screen within one round trip of the
  banner, and usually before it, because the stream or long poll delivered it first.
- `didReceiveRemoteNotification` (background, `content-available`): `syncNow(.push)`, persist, call
  the completion handler. The app is current when the user opens it. Requires the
  `remote-notification` background mode.
- Tap: route to the conversation; the store already has it.
- `BGAppRefreshTask` as an opportunistic periodic sync is cheap to add but is not relied upon.

### 4.5 Web client

A TypeScript port of the reducer and scheduler, driven by the same scenario fixtures (§6.3). The
service worker's `push` handler posts a message to open clients to trigger a sync and suppresses the
notification when a focused client shows that conversation (`clients.matchAll`). Replaces
`useStreamingResponse`'s resume loop, `useLiveMessageUpdates`, `useActivityStream` and the
unconfirmed-reply marker machinery.

## 5. What this fixes, mechanically

| Symptom                           | Today                                                   | After                                                                               |
| --------------------------------- | ------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Push before app, app foregrounded | zombie stream → 60 s idle timeout → backoff → 3 GETs    | push triggers one sync; stream watchdog at 25 s regardless; long poll never zombies |
| Push before app, app backgrounded | nothing until foreground, then 5–9 sequential RTTs      | `content-available` wakes the app; one sync; state is current on open               |
| Resume on bad Wi-Fi               | 5–9 sequential RTTs, two 8 s establish timeouts, ×2 VMs | one request (≈1 RTT), rendered from disk meanwhile                                  |
| Stall / half-open connection      | detected at 60 s                                        | 15 s request timeout; long poll bounded by `wait`                                   |
| Hostile proxy buffering SSE       | SSE unusable, recovery via side-effects of failed opens | correctness on plain request/response; SSE optional and detected dead in 25 s       |
| Send on flaky link                | POST may be lost or double-shown; bounded resume loop   | outbox, idempotent `turn_id`, rendered from outbox until echoed                     |
| Opening a conversation mid-turn   | tail only from connect point                            | full progress snapshot                                                              |
| Cold launch                       | full refetch of everything                              | render from disk, one delta sync                                                    |
| Background battery                | 15 s approvals poll runs forever                        | nothing runs while backgrounded                                                     |

## 6. Measurement and test infrastructure

Measure before and after; the plan ships its own ruler.

### 6.1 Network impairment proxy (`tests/support/netem/`)

An in-repo asyncio TCP proxy (no external binary, runs in CI) with composable, seeded impairments:
fixed latency + jitter, bandwidth cap, random connection reset, **blackhole windows** (drop traffic
for T seconds without RST, the half-open case), **idle cut** (close connections idle longer than T,
the proxy-timeout case), **buffer-until-close** (deliver the response body only on close, the
SSE-buffering case), and scheduled outages. Named profiles:

| Profile          | Shape                                                               |
| ---------------- | ------------------------------------------------------------------- |
| `good`           | 20 ms, no loss                                                      |
| `airplane_wifi`  | 600 ms ± 300 ms, 1 Mbps, 3 % resets, 20 s blackhole every 2 min     |
| `hotel_wifi`     | 150 ms, idle cut at 60 s, buffer-until-close on `text/event-stream` |
| `lossy_cellular` | 120 ms ± 200 ms, 5 % resets, 5 s blackholes every 30 s              |
| `handover`       | 10 s blackhole once (Wi-Fi → cellular)                              |

### 6.2 Benchmark: time-to-consistency

`scripts/sync_benchmark.py` starts the app, the proxy and a client, drives scenarios and reports the
**time from server commit to client render** (p50/p95) per profile, plus bytes and request counts:

- turn completes while the client is connected and idle;
- turn completes during a blackhole; measure from blackhole end;
- app resume after N minutes away with M changed conversations;
- cold launch;
- push received while backgrounded (simulated via the trigger API).

Clients under test: the **Python reference sync client** (`tests/support/sync_client.py`, which
doubles as the protocol's executable specification and runs in backend functional tests), the real
web client via Playwright (CDP `Network.emulateNetworkConditions` for latency/throughput, the proxy
for drops), and, on demand, the iOS simulator against a local backend through the proxy driven by
XCUITest. A small subset with generous bounds runs as functional tests in CI; the full table runs on
demand and is recorded in this document.

### 6.3 Deterministic client scenarios

`SyncEngine` takes a `Transport` protocol and a clock. `SimulatedTransport` replays a scenario file
(JSON: server state timeline + network events: delay, drop, blackhole, 410, 5xx, stale push) under
virtual time, so a two-minute airplane scenario runs in milliseconds and is not retried in CI. The
same scenario files drive the TypeScript engine's tests and the Python reference client, so the
three implementations are proven equivalent on the same inputs. A property test (Hypothesis in
Python, seeded random in Swift) asserts convergence: for any interleaving of changes, drops and
reorderings, the store equals the server state once a sync succeeds.

### 6.4 Production telemetry

Client-side, via the telemetry lane: **staleness** (server `committed_at` of each applied change
minus client apply time, with clock offset estimated from `server_time`), **push gap** (push arrival
minus the time the announced `sync_cursor` was applied; negative means the app was already current),
sync request duration and outcome by reason, and time in each presentation state. This turns
"notifications arrive before the app updates" into a number with a baseline and a target.

### 6.5 Front door check

`scripts/check_sse_frontdoor.py`: timestamped `curl -N` of the live stream and the long poll through
`assistant.andrewgarrett.dev`, reporting whether heartbeats arrive at cadence and whether a 25 s
long poll completes. Run in M0 so the proxy profiles match reality, and again after M1.

## 7. Decisions for review

- **D1. Live channel: keep an SSE stream, or long poll only?** Recommendation: build the long poll
  first (it is the same endpoint), benchmark both on `good` and `airplane_wifi`, keep SSE only if
  the measured token latency on a good network is a visible difference. Fewer transports is the
  goal.
- **D2. Push presentation on the device, ack-free (recommended), or keep server-side ack
  suppression?** Device-side is simpler, removes the grace window and the ack endpoint, and is what
  makes "push triggers sync" sound. Cost: a push is sent even when the user is watching; the device
  suppresses the banner.
- **D3. On-disk store.** Recommendation: SQLite via GRDB (indexed by conversation and `internal_id`,
  incremental writes, no whole-file rewrites per token). Alternative: Codable snapshot files per
  conversation, no dependency, simpler, fine at family scale but rewrites a file on every applied
  change.
- **D4. Outbox scope.** Recommendation: sends, tool confirmations, read markers, steer/stop. Not:
  offline note or calendar edits (different design).
- **D5. Cut-over.** No compatibility layer: the old endpoints stay until the clients using them are
  gone (M6), then they are deleted. TestFlight and the single web deployment make this short.

## 8. Deliberate simplifications

- Last-writer-wins by cursor. No multi-writer conflict resolution; the server is the only writer of
  state and the client's only writes are commands.
- Compacted changes lose intermediate states. Nothing in the UI needs them.
- Focus history is the last page plus on-demand paging; the client does not mirror whole
  conversations.
- Single-process deployment stands. The change log is in the database, so a second worker would only
  need `LISTEN/NOTIFY` for long-poll wake-ups; nothing in the protocol assumes one process.
- Silent pushes are best effort (iOS budgets them); the plan never depends on one arriving.
- Token streaming may become chunked (≈250 ms) if D1 lands on long poll only.

## 9. Milestones

Each milestone ships independently and is verified before the next starts.

**M0. Measure.** Impairment proxy with profiles; `sync_benchmark.py` running the current web client
through Playwright; client telemetry for staleness and push gap on iOS and web; front-door check.
*Outcome:* a baseline table in this document and a live push-gap metric from production. *Verified
by:* the benchmark runs in CI on `good` and `airplane_wifi` against the current protocol and records
numbers.

**M1. Server sync protocol.** `sync_changes` written at the repository chokepoint (with a
conformance rule that no other module writes `message_history`), `GET /sync` with compaction,
`focus`, `wait` and bootstrap, turn progress materialisation, `GET /sync/stream`, `internal_id`
history paging. The Python reference client. Old endpoints untouched. *Verified by:* functional
tests of the reference client through every proxy profile; convergence property test; benchmark
table for the reference client.

**M2. iOS engine for reads.** `SyncEngine` (store, reducer, scheduler) with on-disk persistence, one
per account, replacing the follow stream, activity stream, resync orchestrator, catch-ups and both
polls. Sends still use the current turn POST but are rendered from `active_turns`. Delete
`SyncCoordinator`, `ResyncOrchestrator`, `ActiveTurnSession`, the live-follow bubble maps and the
id-prefix filtering. *Verified by:* deterministic scenario tests; the existing UI tests; a
TestFlight dogfood week with the M0 telemetry compared to baseline.

**M3. Push as a trigger.** `content-available`, `thread-id`, collapse id, `turn_id`/`sync_cursor` in
payloads; `remote-notification` background mode and handler; device-side banner suppression; remove
the ack grace and endpoint; web service worker equivalent. *Verified by:* push gap p95 ≤ 0 s on
production telemetry for a foregrounded app; background-push sync observed on device.

**M4. Outbox.** Persisted commands, idempotent flush, pending-send rendering, offline affordances.
*Verified by:* scenario tests (send during blackhole, app killed with a queued send, duplicate
flush) and the benchmark's send scenarios.

**M5. Web client on the protocol.** TypeScript engine on shared scenarios; delete the three hooks
and the marker machinery; Playwright netem tests. *Verified by:* benchmark table for the web client
against baseline.

**M6. Remove the old protocol.** Server endpoints and hub replay (§3.5), remaining iOS and web dead
code, this document's D1 decided from the M1/M5 numbers. *Verified by:* `poe test`, the benchmark
unchanged, the import-cycle and conformance checks.

## 10. Open questions

1. Does the production front door complete a 25 s long poll, or does it also time out idle requests
   below that? M0's check decides the default `wait`.
2. iOS silent-push budget in practice for this usage (a handful of turns per hour). If throttled, M3
   still works via the alert push's `content-available`, which is what carries the common case.
3. Whether the thread "echo" and "moved turn" features need anything beyond tombstone-plus-upsert.
   Expected answer: no, since both already express as row deletes and inserts.
