# One thread, with threads where they earn it

## Status

Proposed, awaiting approval. Revised after design review. Mockups:
[one-thread-ux/mockups.html](one-thread-ux/mockups.html) (rendered: [iOS](one-thread-ux/ios.png),
[web](one-thread-ux/web.png), [notes](one-thread-ux/notes.png)).

## Problem

The web and iOS clients are organised as a list of separate conversations. That suits a chatbot. It
does not suit a household assistant, which is mostly one ongoing relationship with occasional bigger
jobs:

- A quick question costs a choice: find the right conversation, or start a new one that stays in the
  list forever.
- Long-running work (delegations, browser runs, research) reports back into whichever conversation
  started it. Progress, questions and results from several jobs end up interleaved with unrelated
  chat.
- Switching profile silently starts a new conversation, because history is partitioned by profile.
- Delegated work is only visible as a transient chip (`PendingDelegationsChip`), and once it
  delivers there is nothing left to open.

Telegram already works the other way: one chat, with reply chains as its only structure. People use
it happily for quick questions, but it breaks down when several jobs run in parallel.

## Precedents

- **Meta Muse:** the product is one persistent one-to-one conversation. "Side chats" branch off for
  a single project, and approval cards for consequential actions sit outside the normal chat flow.
- **Grok Bot:** a single "chief of staff" chat delegates work behind the scenes. Threads hang off
  messages behind a chip. The loudest user complaint is that threads are hard to follow from the
  main chat, and users ask for a Slack-style side panel.
- **Claude Code projects:** a parent session spawns child sessions, each shown inline as a card with
  a status dot and an artifact chip. Tapping a card opens the child.
- **Slack:** "Also send to channel" posts a thread's reply into the main stream. The thread keeps
  the detail and the channel keeps the headline.

## The experience

**Home is a single stream and the landing screen.** Quick questions, small edits, reminders,
proactive messages and the morning brief all live there, inline, like a Telegram DM.

**A thread is for work that outlives the turn.** That means work that runs in the background, waits
on someone, takes several steps the user will want to follow, or is a project the user will come
back to. The assistant starts a thread and says so in its reply. The user can start one too:
"Continue in a thread" on any message, "Move to thread…" for an existing thread, send-as-thread from
the composer, or "New thread" from the switcher.

**In Home, a thread is one live card.** It shows the title, a state (working, waiting on you, done,
failed), the latest step or question, and an unread count. The card updates in place and never turns
into a stream of progress messages.

**Answering a thread always reaches the thread.** A simple question the thread is waiting on can be
answered from its card, and the answer goes into the thread, where the thread's profile, tools and
browser tab are. Anything richer opens the thread.

**When a thread finishes, its outcome is echoed into Home.** The thread's own final message appears
in Home, attributed to the thread, so Home reads as the whole story at headline level. A thread that
ends on a question has not finished: it is waiting on you, and its card carries the question.

**Navigation follows the platform:**

- **iOS:** Home replaces the conversation list as the landing screen. The list becomes the thread
  switcher, grouped as Waiting on you / Working / Done today / Earlier. Cards and switcher rows push
  into the thread, which is today's chat view.
- **Web:** three panes. The sidebar holds Home and threads grouped by state. Home stays in the
  centre, and an open thread sits in a right-hand panel that can expand to the centre. Narrow
  windows behave like iOS.
- **Telegram:** the private chat is Home. A thread card is a message edited in place, and replying
  to a card routes into that thread.
- **Voice:** a voice session started from the app belongs to that app's Home. A job that becomes a
  thread is announced aloud, and its card appears in Home.

## Model: a thread is a conversation with a parent

The alternative was to surface the existing subconversations, which `delegate_to_service` already
creates and which `message_history` already partitions on. It was rejected for two reasons.

**Subconversations mean a delegate's private working history.** Their request text is written by a
model, they allow one active run each, and they are deliberately excluded from `/messages`,
summaries and the conversation list. Making some of them visible would put an exception into each of
those filters, and every reader would have to tell the two kinds apart.

**Almost everything a thread needs is already keyed per conversation.** That covers:

- stream hub buffers and resume, active turns, cancel and steer, and activity pings
- the ownership check and share links
- the sticky model tier and profile adoption
- summaries and memory review

Both clients already render a conversation, too. Under the subconversation model each of these would
need a new dimension. Under this model they work unchanged.

### The three kinds of conversation

- **Home** is one designated conversation per user per surface. Web and iOS already share an
  interface type and a conversation namespace, so they share one Home. A user's Telegram private
  chat is their Telegram Home. Unifying the two is out of scope (see Deliberate simplifications).
- **A thread** is an ordinary conversation plus a link record. Every thread has one, whether or not
  it grew from an anchor, and the record is what tells a thread from a legacy conversation. It holds
  the parent Home, the anchor turn (if any) and the thread's state.
- **Legacy conversations** have neither a Home role nor a parent. They appear under "Earlier" in the
  switcher, open and continue as they do today, and are never migrated.

**Subconversations are unchanged.** A thread that delegates gets hidden subconversations inside the
thread's conversation, exactly as today. The browser scope is keyed on conversation and
subconversation, so separate threads get separate tabs by construction.

**Each thread keeps its own profile and tier.** Choosing another profile from Home starts a thread
on that profile rather than an unrelated conversation. Home stays on the default profile. In Home, a
tier choice applies only to that turn and does not stick. A request that deserves a stronger model
for the long run is a thread, and the tier sticks there.

### Starting a thread

**Starting a thread is a delegation whose result is delivered differently.** The assistant starts
one with a Home-only tool. The tool creates the child conversation and enqueues its first turn on
the existing delegation machinery, `delegation_runs` plus the task worker, which gives it durable
queueing and retries. This extends the run model rather than parameterising it. Today a run's rows
live in a subconversation of its source conversation, and completion wakes that source. A thread run
has a target conversation distinct from its source (Home), no subconversation, and a completion that
is recorded without a wake, because the user talks to the thread directly from then on. With no
subconversation, the run is a browser scope of its own rather than inheriting its source's tab.

**A message sent before the first turn starts steers it.** The common case is the user opening a
fresh card and typing straight away. Between enqueue and start there is no active turn to steer, so
a thread with a queued run treats the user's message as steering for that run, the same way it would
once the run is going. It must not start a competing turn.

**It is admitted exactly like a delegation.** Home's model picks the profile and writes the request,
so the target must accept it under the same `allowed_delegation_sources` and
`delegation_security_level` checks, and the request carries the same taint sources. A user starting
a thread from the client is a user choice, like picking a profile today.

**User actions use the same path.** "Continue in a thread" and "New thread" go through the same
creation path, with a user-written opening message. "Move to thread…" moves a whole turn (the user
message with its tool rows and reply), never a single row.

**When to thread is guidance in the prompt, not code.** The default profile's prompt describes when
a thread earns its place. The user's override is always one action away, so a wrong call is cheap.

Threads do not nest. A thread can delegate but cannot start threads.

### Moving content across the boundary

Three paths move rows between conversations: the seed copied into a new thread, a turn moved with
"Move to thread…", and the echo copied into Home. All three follow one rule. **Copied rows keep
their taint and are re-stamped to the destination's profile**, because the history window is scoped
by profile and an unstamped row would be invisible there.

- **The seed follows the delegation boundary.** An assistant-started thread carries what a
  delegation carries today: the request its starting turn wrote, plus the anchor turn. It never
  carries the Home transcript around them. That matters most for profiles that read untrusted
  content and can act, such as `browser_profile`, where a page could try to exfiltrate whatever the
  seed contains. Only the user's own "Continue in a thread" carries the Home turns leading up to the
  anchor, as a deliberate choice to bring that context along, and only into a profile that already
  receives household context. A profile that excludes it (`coder`, `media_analyst`, via
  `excluded_context_providers`) gets the request only, whoever starts the thread. A seed is copied
  rather than read across conversations at every turn, so the thread's history doesn't depend on
  Home's later compaction or edits.
- **Home sees its threads as facts, not transcripts.** Home's assistant sees each live thread's
  title, state and, once it finishes, the echoed final message. That is enough to answer "how are
  the quotes going?" without merging histories. **Generated conversation summaries are never used
  for this.** They derive from possibly untrusted content and carry no taint, as
  [conversation-list-summaries.md](conversation-list-summaries.md) requires. The title Home sees is
  the one written when the thread was created, and it carries that turn's taint.
- **A thread does not see its sibling threads.**

### State

- **Working:** the thread has a queued or active turn, a pending delegation or a pending worker.
- **Waiting on you:** the thread is neither working nor done, so the next move is the user's. This
  is derived by code, not marked by the model, so every thread is always in exactly one state.
- **Done or failed:** the thread's assistant ends the job with a tool call, or the user marks it
  done.
- **A done thread reopens when something new arrives in it.** That could be a reminder or callback
  it scheduled, a late delegation result, or a user message. The day the plumber comes is the same
  thread, not a new one. The thread goes back to working or waiting, its card and switcher row
  update, and finishing again writes a new echo.
- **Unread is a separate per-viewer count.** In a household, one person reading a thread does not
  clear another person's badge. Legacy conversations have no state.

**Finishing writes the echo.** The thread's final message is copied into Home as a row attributed to
the thread (see the rule above). It pings Home's stream and updates the card. The echo is a copy,
not a model turn, so finishing a thread never wakes Home's assistant. Delegations *inside* a thread
wake the thread's profile in the thread's conversation, as today.

### Telegram

Telegram assumes one chat is one conversation today, so three things change:

- **The turn slot becomes per conversation.** The slot is currently held per chat id, so a Home
  message sent while a thread turn runs would steer that thread's turn instead of starting a Home
  turn.
- **Delivery resolves a thread to its chat.** A thread's messages go to the chat with a reply to the
  thread's card, and the card is edited in place.
- **Replies to a card or a thread message route into the thread.** Other reply chains keep their
  current meaning, which is pulling the replied-to turn into context and adopting its profile. They
  do not become threads.

Native private-chat topics (Bot API 9.4) are a possible later step and not part of this design.

## Security

Nothing here changes what a profile can reach. A thread runs on a profile under that profile's tool
policy, exactly as a conversation does today. The new surface is what moves between conversations,
and each path is governed:

- **Starting a thread** is admitted by the target profile's delegation policy, and the request
  carries delegation taint.
- **Copied rows** keep their taint. An assistant-started thread is seeded with no more than a
  delegation carries: the request and the anchor turn. Wider Home context goes only where the user
  chose to bring it, and never to a profile that excludes household context.
- **Home's view of a thread** is a title (carrying its creating turn's taint), a code-derived or
  explicitly set state, and echoes (carrying their source's taint). Generated summaries never reach
  a prompt.
- **The link record** joins two conversations owned by the same user. Creating a thread or moving a
  turn into one checks ownership of both ends.

## Deliberate simplifications

- **One Home per surface.** The Telegram Home and the web/iOS Home are separate conversations,
  because history is scoped by interface type. A household that uses both sees two Homes. Unifying
  them means lifting interface scoping from history, which is a separate project.
- **No nested threads.** One level covers the household use cases.
- **The seed is a snapshot.** A Home message edited after the thread started is not reflected in the
  thread.
- **Cards do not re-post.** A card that has scrolled out of view in Home stays where it is. "Waiting
  on you" is carried by the switcher badge, the sidebar and push notifications.
- **Tier choices don't stick in Home.** Home is everlasting, so a sticky tier would quietly apply to
  every later quick question.

## Decisions

- **The assistant starts threads on its own from day one.** It doesn't propose them first. User
  overrides are one action away in both directions.
- **Done threads reopen** on later events, as described under State.
- **Card quick replies come only from explicit questions the thread asks.** A general
  suggested-replies mechanism may come later.

## Work plan

1. **Threads exist.** Add Home designation, the link record, the start-thread tool on delegation
   machinery, and seeding under the copy rule, behind a single global setting. Threads hang off
   Home, which is per user, not per profile.
   - Verified by functional tests: a started thread runs on its own profile.
   - The target's delegation policy is enforced.
   - Seeded rows are visible to the thread's history window and keep their taint.
   - Coder receives no seed.
   - A message sent before the first turn starts steers it.
2. **Home on web.** Add Home as the landing view, thread cards, the grouped sidebar and the
   right-hand panel. Verified by Playwright tests of card state transitions, answering from a card,
   and panel navigation.
3. **State and echo.** Add derived working and waiting states, explicit done and failed, per-viewer
   unread counts, the echo, and Home's view of thread facts. Verified by tests that Home answers a
   status question from thread facts, that an echo keeps its taint, and that a thread ending on a
   question is waiting rather than done.
4. **Home on iOS.** Add the landing view, the switcher, cards and push navigation. Verified by unit
   tests on card state and UI tests on navigation.
5. **User overrides.** Add "Continue in a thread", "Move to thread…" (moves a turn), send-as-thread
   and "New thread" on both clients.
6. **Telegram.** Add a per-conversation turn slot, delivery from thread to chat, edited-in-place
   cards, reply-to-card routing and echoes. Verified by tests that a Home message during a running
   thread turn starts its own turn rather than steering.
