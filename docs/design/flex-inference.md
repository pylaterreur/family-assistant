# Flex Inference for Background Work

## Status

Approved for implementation (2026-10-22).

## Goal

Pay the discounted "Flex" price for model calls nobody is waiting on, without slowing down the calls
somebody is, and without growing a client per combination of provider, model, parameters and price
class.

Google (Gemini Developer API), OpenAI and OpenRouter all offer Flex: the same synchronous request
with `service_tier: "flex"`, at roughly half the token price, in exchange for queueing and
occasional capacity rejections (429/503). Anthropic offers no Flex; its discount is Message Batches,
a separate asynchronous API that does not fit a tool-calling turn.

Most of the assistant's turns have no human waiting: scheduled automations, callbacks, event
listener wakes, email intake, memory review, conversation-list summaries and document indexing. They
run on the `standard` tier, whose models (Gemini Flash, and gpt-6-luna on the reminder and event
profiles) all have Flex.

## Two independent axes

A **model tier** says which model to run. A **workload** says whether anybody is waiting for the
answer. They are orthogonal: a background task may still need the deep tier, it just does not need
capacity immediately. So workload is not a tier, and Flex variants are not tier entries.

- `interactive` (the default): somebody is waiting. Never Flex.
- `background`: nobody is waiting. Flex where the model's chain entry opts in.

## Approach

### Workload is set once, where the work starts

The task worker marks the task types that are background work, around the handler, in a `ContextVar`
beside the existing call attribution (`llm/call_context.py`). Every LLM call made underneath — the
turn's own calls, its tool-call review, summaries, one-shot script calls — sees it without any
argument threading.

The marking is an allowlist of task types. A task type nobody listed runs `interactive`, so neglect
costs money, not latency for a person. Turns a person is waiting on (chat, resumption of an
interrupted turn, delegated runs) are not on the list.

### Flex is an opt-in on a chain entry, applied at the request chokepoint

A tier or `retry_config` chain entry sets `flex: true`. Nothing else in the configuration changes.

Each provider client already assembles every request's parameters in one place (Gemini's
`_build_base_generation_config`, OpenAI's `_get_model_specific_params`). That accessor adds
`service_tier: flex` when the in-flight call has been marked as a Flex attempt. The same client
object serves both interactive and background calls; the price class is a property of the call, not
of the client.

### Capacity failures degrade to Standard, on the same model

A flex-enabled entry's client is wrapped once, at construction. For a background call the wrapper:

1. makes the call as a Flex attempt, bounded by a maximum wait (time to the first streamed event, or
   the whole call when not streaming);
2. if Flex is out of capacity (rate limited, unavailable, timed out, or past the wait) and nothing
   has been streamed yet, makes the call again at Standard on the same model;
3. after such a failure, stops attempting Flex on that entry for a cool-down period, so a capacity
   crunch costs one wasted wait per entry rather than one per call.

Only after the Standard attempt fails does the chain's existing fallback (`RetryingLLMClient`) run.
Without step 2, a Flex 429 would go straight to the chain's fallback — usually a different provider
at full price — which is the opposite of the intent.

The maximum wait bounds what a background turn can lose to Flex queueing, so the existing task
handler timeouts stay correct.

### Validation at startup, without building variants

Because the per-call input is a closed choice (Flex or not) rather than free-form parameters, every
combination is known when configuration loads, and is checked then:

- `flex: true` on a provider without Flex (Anthropic), on an Interactions/Antigravity agent model,
  or alongside an explicit `service_tier` in the entry's parameters is a startup error naming the
  tier and entry.
- A Gemini entry's parameters are validated with the Flex field included, so an SDK that does not
  know `service_tier` fails at startup rather than on the first background call.
- Every tier is checked, including tiers no profile can currently reach.

### Shared provider connections

Independently of Flex, the factory built a new SDK client — with its own HTTP connection pool — for
every chain entry of every tier and profile. Clients now borrow their SDK client from a registry
keyed by provider, endpoint and credential, owned by the application and closed at shutdown. A model
client without its connection is just a model name and its parameters, so the number of model
clients stops mattering.

### Measuring it

Each provider call's metrics carry the requested service tier, and the tier the provider says it
served (OpenAI's `service_tier`, Gemini's `traffic_type`) is stored with the call's usage. Savings
and downgrade rates are then queries over existing metrics and message history.

## Deliberate simplifications

- **Workload is per task type, not per profile.** A profile on a flex-enabled tier cannot opt out of
  Flex for its background turns. The maximum wait bounds the cost to a reminder or event handler.
- **No Flex retry loop.** One Flex attempt, then Standard. Waiting longer for the discount is not
  worth the latency for a household's volume.
- **No Batch API.** Single-call background jobs (summaries, indexing) are cheap enough already that
  a submit-and-poll lifecycle does not pay for itself. Flex gives them the same discount through the
  synchronous path. Evals already have their own batch runners.
- **Anthropic stays at Standard.** The deep tier's Opus and the Haiku review judge get no discount.

## Work plan

1. **Shared connections.** No behaviour change. Verified by unit tests that clients built for the
   same provider, endpoint and key share one SDK client and that only the registry closes it.
2. **Workload and Flex.** Workload context, task-type allowlist, `flex` on chain entries, provider
   chokepoints, the degrading wrapper, startup validation, metrics and served-tier recording. Ships
   enabled on the `standard` tier's Gemini entry. Verified by unit tests of the wrapper's
   degradation and cool-down, request-parameter tests per provider, startup-validation tests for
   each rejected configuration, and a task-worker test that background task types (and only those)
   reach the provider as Flex.
3. **Evaluate.** Compare cost per completed background task, completion time, and the downgrade rate
   from production metrics before enabling Flex on further entries.
