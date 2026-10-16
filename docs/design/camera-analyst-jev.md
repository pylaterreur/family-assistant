# Camera Analyst on Jev

## Problem

The `camera_analyst` profile has never worked well: an investigation is slow and expensive, and
often runs out of patience before it answers. Three things make it so.

1. **Every frame costs a generative call.** `scan_camera_frames` sends each frame to the turn's chat
   model with a structured-output request for a match flag, a description, a confidence and a list
   of objects. That is seconds of latency and an output-token bill per frame, to answer what is
   almost always a yes/no question.
2. **The search loop runs in the agent.** The prompt teaches the model to binary-search: fetch a
   batch of frames with `get_camera_frames_batch`, look at them all, narrow the range, repeat. Every
   batch lands in the agent's context as images and is re-sent on each later iteration, so cost
   grows with the square of the investigation's length, and the model spends iterations on
   bookkeeping a loop in code would do for nothing.
3. **Frames arrive one at a time.** `get_frames_batch` bounds concurrency at three, but every
   `get_frame` takes the camera's lock, so extraction is serial: a recording search plus an ffmpeg
   seek on the playback stream per frame, a few seconds each at best.

Measuring against a real camera (a Reolink Duo 3 watching a backyard) found four more reasons, each
a bug in the camera backend rather than in the agent:

4. **A search across midnight finds nothing.** A Reolink recording search whose window spans
   midnight returns no files at all, so every overnight question saw no footage.
5. **Every recording time was an hour out in summer.** Reolink searches and reports by wall-clock
   fields; `reolink_aio` tags the times it returns with the camera's base offset, without daylight
   saving. Converted to local time they were an hour late, and a frame requested at the reported
   time was the wrong frame.
6. **A blind grid misses motion-triggered footage.** The camera records only when it sees motion.
   Nine of ten frames on a regular grid fell in gaps and came back "no recording".
7. **Seeking to a clip's last seconds stalls.** The playback stream sends nothing until ffmpeg times
   out at 30s, and then the download fallback fails too, because this camera returns an empty reply
   to `Download`.

## What Jev offers

TypeSafe's Jev answers typed questions with calibrated probabilities. Image input is available on
`jev-1.13.0` (the model already pinned in `typesafe.model`) behind the
`TypeSafe-Danger-Zone-Images: true` header: images are supplied in an `assets` map and referenced by
key from `state`, instructions or criteria. Billing is per input token with outputs free.

### Benchmark

108 frames from the backyard camera: 90 sampled across five days of motion clips (most of them
night-time rain, insects and lens glare), and 18 from clips the camera itself tagged as a person or
an animal. Each was asked four yes/no questions by Jev (`jev-1.13.0`) and by the current scan path
(`gemini-3.8-flash` structured output), and scored against hand labels. Chickens too small to tell
apart from the coop's white paint were left unscored.

| Question (positives / negatives) | Jev at 0.5          | Gemini Flash      |
| -------------------------------- | ------------------- | ----------------- |
| Person visible (6 / 102)         | 5 found, 5 false    | 5 found, 9 false  |
| Chicken visible (18 / 61)        | 16 found, 3 false   | 18 found, 0 false |
| Night-time infrared (37 / 70)    | 37 found, 5 false   | 37 found, 2 false |
| Dog or cat (0 / 106)             | 0 false             | 1 false           |
| Latency per frame, p50 / p95     | 0.26s / 0.36s       | 4.5s / 8.2s       |
| Cost per frame                   | ~2,100 input tokens | image plus output |

Both judges called the chickens at the edge of two frames people. Jev is as good on scene-level
questions and on people, and seventeen times faster. It is weaker on small, distant animals, missing
two hens Gemini found. Rephrasing the question, or adding true/false criteria, moved the results by
no more than noise.

The design takes Jev's speed and cost for the scan and accepts the misses, with two mitigations: the
agent looks at the frames a scan attaches before reporting, which catches false matches; and
`typesafe.camera_frames.mode: off` returns a deployment to the model judge.

TypeSafe labels image input experimental and not production-ready; its request format may change
between releases. Pinning the model bounds the drift, and the fallback below bounds the damage.

## Approach

### A frame judge, not a frame describer

Scanning asks one question of many frames, so the per-frame work becomes a **judgement**: the
probability that a frame satisfies a condition. Jev is the judge when the deployment has TypeSafe
configured; otherwise the existing per-frame LLM call is, its answer mapped onto the same
probability. Both camera search tools take the judge, so neither knows which one it has.

Descriptions disappear from the scan. The agent looks at the few frames that matter itself, which is
both cheaper and better than reading a describer's paraphrase of them.

### `scan_camera_frames` returns a timeline

The scan returns every frame's probability as text — a cheap timeline the agent can read — and
attaches images only for the frames that match, highest first, with a cap. A scan that finds no
match still says where the near-misses were, rather than only "nothing found".

### `find_camera_change` moves the binary search into code

Most "when did X happen" questions are about a condition that flips once: the package is on the
step, then it is not; the gate is closed, then it is open. A new tool takes the condition and a time
range, samples it coarsely, finds where the judged state flips, and bisects each flip down to a
requested precision. It returns the moment with a before and an after frame. The agent gets an
answer in one tool call instead of a dozen iterations of image-heavy context.

A frame that cannot be extracted (a gap in recording) leaves its interval unresolved at the
precision reached so far; the tool reports that rather than guessing.

### Camera backend fixes

The search across midnight is split into one search per wall-clock day, and the backend treats every
time as wall-clock time in the household's zone, which is the zone the cameras' clocks follow.
Frames are sampled from the recordings in a range rather than on a blind grid, and a few seconds
clear of a clip's start and end.

### Faster extraction (deferred)

The camera served six playback extractions at once in 3.7s against 15.8s one at a time, so the
per-camera lock is most of the remaining latency. Lifting it is deferred: after a run of concurrent
extractions, some cut off mid-stream by the ffmpeg timeout, the camera refused all playback for more
than five minutes while still serving live snapshots. Until that is understood, extraction stays one
request at a time per camera, as before. An end-to-end `find_camera_change` across six evening hours
took 242s one frame at a time with samples at clip ends, and 84s with samples kept clear of clip
ends and extraction concurrent; Jev's share of either was a few seconds. The serial time with the
clip margins in place is yet to be measured.

### A smaller profile

With search in tools, the profile orchestrates rather than looks. `get_camera_frames_batch` is
retired from it, its iteration ceiling comes down, and its prompt is rewritten around the two search
tools.

## Privacy and the Rule of Two

Camera frames are household data, and with Jev they reach a second processor besides the chat
provider. Jev has no tools and its answers are constrained to the probabilities the caller asked
for, so a frame's contents cannot direct it to act or exfiltrate; the profile's [BC] posture is
unchanged. Operators opt in through the existing TypeSafe configuration, and camera scanning has its
own switch so a deployment can use Jev elsewhere and keep frames off it.

## Deliberate simplifications

- **One request per frame.** Jev can take several images in one request, but every question sees the
  whole state, so batching N frames costs N² image tokens for no latency win over N concurrent
  requests.
- **A failed judgement is an error, not a fallback.** A frame Jev fails on is reported as
  unanalysed, as the LLM path does today. Falling back per frame to the LLM would double the
  failure-handling paths for a case the error count already makes visible.
- **A change too brief for the coarse pass is missed.** `find_camera_change` finds the flips between
  the frames its coarse pass samples, so a state that comes and goes between two samples -- a
  spotlight on for a minute -- goes unseen. The coarse pass is spread over the recordings, so this
  takes a change shorter than the gaps between them; `scan_camera_frames` over the narrower range is
  the way to look for one.
- **One condition per search.** Compound questions ("a person carrying a package") are left to the
  agent to phrase; TypeSafe's own guidance is that simpler questions do better on images.

## Milestones

1. **Measure.** Done: the benchmark above.
2. **Judge and scan.** Image support in `JevClient`, the frame judge with Jev and LLM
   implementations, `scan_camera_frames` on it, and the camera backend fixes. Verified by unit tests
   against a fake Jev and the fake camera backend, and end to end against the real camera.
3. **Find the change.** `find_camera_change`, and the profile and prompt rework around it. Verified
   by unit tests over a fake backend whose frames flip at a known time, including recording gaps.
4. **Extraction.** Lift the per-camera lock once it is known what cutting off concurrent playback
   does to the camera. Verified by a long concurrent run against a real camera that leaves playback
   working afterwards.
