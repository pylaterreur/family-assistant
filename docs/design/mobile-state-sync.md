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

### 3.1 Change tracking (the chokepoint)

Every synced row carries a `sync_cursor`: the cursor of the transaction that last wrote it. The
synced tables are the ones behind the client's collections: `message_history`,
`conversation_threads`, `conversation_read_markers`, `confirmation_requests` and `delegation_runs`.
"What changed since N" is then a query of the state itself, `WHERE sync_cursor > N`, and the answer
is already compacted: a row written five times appears once, in its current state. Deletes leave a
row in `sync_tombstones` (kind, entity id, audience, cursor), because a deleted row cannot answer a
query.

**The stamp is applied by the transaction executor, not by each writer.** `DatabaseTransaction`
already sees every symbolic `INSERT`, `UPDATE` and `DELETE` before it runs. For a statement on a
synced table it allocates the transaction's cursor (once per transaction), adds
`sync_cursor = <cursor>` to inserts and updates, and turns a delete into "record a tombstone for
each matching row, then delete". A write path added next year is stamped without anyone remembering
to stamp it; Telegram, the task worker, move-to-thread and in-place mutations are covered the same
way as web turns. Raw SQL text is not stamped, and the project already forbids it for writes.

**The cursor is commit-ordered, not sequence-ordered.** A PostgreSQL sequence hands out values
outside the transaction, so two writers can take 10 and 11, 11 commits first, a client advances to
11, and 10 becomes visible afterwards below the client's cursor and is never delivered. The cursor
is therefore allocated from a single counter row that the writing transaction updates and holds
locked until commit (`UPDATE sync_cursor SET value = value + 1 RETURNING value`). Writers to synced
tables serialise on that row for the rest of their transaction, which is acceptable at household
scale and is what makes "everything with a cursor above yours is visible to you" true. The
convergence property test (§6.3) exercises exactly this interleaving against PostgreSQL.

**Every synced row names its audience, and the audience is either a conversation or a user.**
Conversation-scoped kinds (messages, threads, and the conversation rows derived from messages) are
filtered at read time by the ownership predicate `GET /conversations` already uses, so ownership
rules stay in one place. User-scoped kinds are filtered by their user column: a read marker belongs
to its viewer, not to the conversation (another viewer of a shared conversation must not receive
it), a confirmation to its `target_user_id` (it may have no origin conversation at all), a
delegation to the user who started it. A kind that fits neither is a design error, not a third
filter. Tombstones record the same audience as the row they replace.

The transaction's commit callback wakes waiting long polls in-process, so a client hears about a
commit without the server polling the database.

Retention: tombstones older than a bound (say 30 days) are pruned; a cursor older than the oldest
retained tombstone gets `410` and the client does a bootstrap sync. Same contract as today's 410,
but the bootstrap is one request, not a different code path.

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

- **Store**: materialised state plus the cursor, persisted to disk and observed by the UI. Its
  collections are exactly the server's change kinds: conversation rows, Home and threads, messages
  per conversation, active turn progress, read markers, pending confirmations, pending delegations
  (status and child progress, which the 15 s poll observes today). **Anything the UI renders from a
  poll or a stream today is a sync kind, and a poll is deleted only once its kind is in the
  engine.** Views no longer own network state, so pushing a conversation over Home no longer doubles
  every connection.
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

### 6.1 Network impairment proxy (`tests/netem/`)

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

`poe sync-benchmark` starts the app, puts the impairment proxy in front of it and drives a client
through scenarios, reporting the **time from the moment a change could be shown to the moment the
client shows it** (p50/p95) per profile, plus connections opened and bytes moved. A driver commits
changes over a perfect link, so only the client under test sees the bad network:

- a reply completes in the open conversation while the client is connected and idle;
- a reply completes while the page is still loading the conversation;
- the client sends a message itself;
- a reply completes during a blackhole, measured from the blackhole's end;
- a reply completes just after a hand-over resets every connection;
- a reply completes in a conversation other than the open one (the list must update);
- cold load of a conversation.

Clients under test: the real web client via Playwright from M0, the **Python reference sync client**
from M1 (it doubles as the protocol's executable specification and runs in backend functional
tests), and, on demand, the iOS simulator against a local backend through the proxy. A regression
subset with generous bounds runs in CI; the full table runs on demand and is recorded in §6.6.

### 6.3 Deterministic client scenarios

`SyncEngine` takes a `Transport` protocol and a clock. `SimulatedTransport` replays a scenario file
(JSON: server state timeline + network events: delay, drop, blackhole, 410, 5xx, stale push) under
virtual time, so a two-minute airplane scenario runs in milliseconds and is not retried in CI. The
same scenario files drive the TypeScript engine's tests and the Python reference client, so the
three implementations are proven equivalent on the same inputs. A property test (Hypothesis in
Python, seeded random in Swift) asserts convergence: for any interleaving of changes, drops and
reorderings, the store equals the server state once a sync succeeds.

### 6.4 Production telemetry

Client-side, via the telemetry lane: **staleness** (client apply time minus the server
`committed_at` of each applied change, with clock offset estimated from `server_time`; larger is
worse), **push gap** (the time the announced `sync_cursor` was applied minus push arrival; positive
means the banner beat the app, zero or negative means the app was already current), sync request
duration and outcome by reason, and time in each presentation state. This turns "notifications
arrive before the app updates" into a number with a baseline and a target.

### 6.5 Front door check

`scripts/check_sse_frontdoor.py` holds an event stream open through `assistant.andrewgarrett.dev`
and prints when each frame arrives, so late, bunched or missing heartbeats show the front door
buffering or cutting it. After M1 it gains a long-poll probe. It needs a user access token: the
read-only diagnostics token is rejected at the gateway, so it has not yet been run against
production.

### 6.6 Baseline (M0, current web client)

Recorded 2026-10-25 with `poe sync-benchmark`, five runs per cell, against a local backend through
the impairment proxy. The app is loaded over a good link before the profile applies, as for someone
whose connection degrades with the app open; the two load scenarios then reload under the profile
with assets already cached. "KB down" includes that initial page load and is not comparable between
scenarios. A timeout means the client had not shown the change 60 seconds later.

| Client | Scenario           | Profile        | Runs | p50 (s) | p95 (s) | Timeouts | Connections | KB down |
| ------ | ------------------ | -------------- | ---- | ------- | ------- | -------- | ----------- | ------- |
| web    | after_blackhole    | good           | 5    | 0.20    | 0.22    | 0        | 27          | 3063    |
| web    | after_blackhole    | lossy_cellular | 5    | 0.50    | 0.69    | 0        | 31          | 3062    |
| web    | after_blackhole    | airplane_wifi  | 5    | 1.28    | 1.57    | 0        | 31          | 3062    |
| web    | after_blackhole    | hotel_wifi     | 5    | timeout | timeout | 5        | 37          | 3038    |
| web    | after_handover     | good           | 5    | 6.99    | 7.06    | 0        | 32          | 3062    |
| web    | after_handover     | lossy_cellular | 5    | 7.41    | 23.22   | 0        | 34          | 3060    |
| web    | after_handover     | airplane_wifi  | 5    | 8.92    | timeout | 1        | 34          | 3058    |
| web    | after_handover     | hotel_wifi     | 5    | 7.23    | 7.28    | 0        | 38          | 3061    |
| web    | cold_load          | good           | 5    | 0.66    | 0.80    | 0        | 6           | 3219    |
| web    | cold_load          | lossy_cellular | 5    | 1.28    | 1.46    | 0        | 22          | 3181    |
| web    | cold_load          | airplane_wifi  | 5    | 2.60    | 2.65    | 0        | 17          | 3147    |
| web    | cold_load          | hotel_wifi     | 5    | 0.95    | 1.02    | 0        | 17          | 3218    |
| web    | commit_during_load | good           | 5    | timeout | timeout | 5        | 46          | 3263    |
| web    | commit_during_load | lossy_cellular | 5    | 59.78   | timeout | 2        | 52          | 3254    |
| web    | commit_during_load | airplane_wifi  | 5    | timeout | timeout | 3        | 49          | 3247    |
| web    | commit_during_load | hotel_wifi     | 5    | timeout | timeout | 5        | 53          | 3254    |
| web    | connected_idle     | good           | 5    | 0.15    | 0.18    | 0        | 8           | 3065    |
| web    | connected_idle     | lossy_cellular | 5    | 0.63    | 0.68    | 0        | 10          | 3063    |
| web    | connected_idle     | airplane_wifi  | 5    | 1.54    | 1.80    | 0        | 10          | 3062    |
| web    | connected_idle     | hotel_wifi     | 5    | timeout | timeout | 5        | 32          | 3037    |
| web    | other_conversation | good           | 5    | 0.12    | 0.21    | 0        | 6           | 3042    |
| web    | other_conversation | lossy_cellular | 5    | 0.82    | 0.93    | 0        | 9           | 3039    |
| web    | other_conversation | airplane_wifi  | 5    | 1.95    | 2.18    | 0        | 9           | 3038    |
| web    | other_conversation | hotel_wifi     | 5    | timeout | timeout | 4        | 28          | 3037    |
| web    | own_send           | good           | 5    | 0.13    | 0.24    | 0        | 6           | 3043    |
| web    | own_send           | lossy_cellular | 5    | 0.79    | 1.12    | 0        | 13          | 3047    |
| web    | own_send           | airplane_wifi  | 5    | 1.86    | 2.34    | 0        | 14          | 3047    |
| web    | own_send           | hotel_wifi     | 5    | 5.42    | 5.46    | 0        | 28          | 3043    |

What the table shows:

- **A reply committed while the page loads is lost.** The client loads history, then opens its
  follow stream about a second later from the live head and skips the reload on first connect, so a
  turn that ends in between never appears until something else triggers a reload. This happens on a
  perfect link. The design removes it structurally: a sync request carries the cursor its snapshot
  was taken at.
- **A buffering front door freezes the client.** On the hotel profile, idle updates, other
  conversations and recovery after an outage never arrive, because nothing reaches the client until
  the buffer fills. The client's own send recovers in about five seconds only because its stream
  closes when the turn ends. Plain request and response, as in §3.2, is immune.
- **Every hand-over costs about seven seconds**, the client's fixed reconnect delay plus a reload,
  and on lossy links the tail reaches 23 seconds or never.
- **Steady state on a working stream is fast**: about 0.15 s on a good link and 1.5 to 2 s on the
  airplane profile, roughly one round trip plus a history reload. The new protocol has to match this
  rather than beat it.
- **The web bundle is about 3 MB.** On the airplane profile's 1 Mbit/s a first load takes longer
  than the page-load timeout. That is outside this design, but it is why the benchmark loads the app
  before degrading the link.

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

**M0. Measure.** Impairment proxy with profiles; the time-to-consistency benchmark running the
current web client through Playwright; the push-gap metric on iOS, fed by a `turn_id` in push
payloads; front-door check. The web client's push gap is measured from M5, because M5 replaces the
code it would instrument. *Outcome:* a baseline table in this document (§6.6) and a live push-gap
metric from production. *Verified by:* a regression subset of the benchmark runs in CI on the good
profile; the full table is recorded here.

**M1. Server sync protocol.** Executor-stamped `sync_cursor` columns and tombstones (§3.1),
`GET /sync` with `focus`, `wait` and bootstrap, turn progress materialisation, `GET /sync/stream`,
`internal_id` history paging. The Python reference client. Old endpoints untouched. *Verified by:*
functional tests of the reference client through every proxy profile; convergence property test,
including the commit-order interleaving on PostgreSQL; benchmark table for the reference client.

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
