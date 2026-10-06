# Demo video script

A voice-over script for a person to record, about 90 seconds, with a marked extension to about 2:30. Harborline Supply Co. and all of its data are fictional, and the video says so on screen.

The table below is the source of truth. One command builds everything from it, so a change to this file regenerates the silent cut, the captions and the teleprompter:

```sh
cd demo && npm run video       # -> demo/out/video/ and docs/video/
```

What it makes, and where:

| File | What it is |
| --- | --- |
| `demo/out/video/silent-core.mp4`, `silent-extended.mp4` | The silent master cuts, every scene in order at the timings below (git-ignored) |
| `demo/out/video/teleprompter-core.mp4`, `teleprompter-extended.mp4` | The same cuts with each narration line burned in on a bar under the picture, shown 1.2 s before it is to be spoken (git-ignored) |
| `docs/video/captions-core.srt` and `.vtt`, `captions-extended.srt` and `.vtt` | One cue per sentence of narration, timed to the scenes (committed) |
| `demo/out/final/*.mp4` | The finished video, from `demo/video/mux.sh` (git-ignored; it goes to YouTube) |

## How to record

1. Run `cd demo && npm run video`, then play `demo/out/video/teleprompter-core.mp4` (or `-extended`) and read the line on the bar when it appears. Each line is shown 1.2 seconds before its cue, so you can breathe in. Record the voice alone, in a quiet room, as a WAV or M4A file, starting when the video starts.
2. The pace is about 140 words a minute. The build prints any scene whose narration is faster than 2.7 words a second, and refuses one faster than 3.2.
3. Mux it: `demo/video/mux.sh voice.wav core` (or `extended`). It normalises the loudness (two passes, -16 LUFS, true peak -1.5 dB), keeps the picture untouched, and writes `demo/out/final/ai-gateway-core.mp4`. It stops with a message if the voice is more than half a second longer than the picture, and pads a shorter one with silence.

## What is on screen

The clips are the real ones in `docs/media`: Claude Code through the gateway (`real-client-*`, a rendered transcript of a real run, labelled as such, then the dashboard), and the dashboard under simulated traffic. The compliant-model run is **labelled on screen** as a stand-in for a model that was talked into the export; never describe it as something Claude did unprompted. Claude Desktop was not tested and is not shown.

## The script

Source syntax: `card:title`, `card:end`, `still:scorecard`, `still:architecture`, `text:Heading / line / line`, and `clip:<name>:<scene>,<scene>` (a clip's scenes, from its timeline: `real-realistic`, `real-compliant` and `dashboard`). A clip shorter than its scene holds its last frame; a longer one is sped up, to at most twice. `{{...}}` is read from `docs/scorecard.json` when the build runs: `attacks`, `columns`, `all_on`, `all_off`.

<!-- script:start -->
| id | cut | seconds | source | narration |
| --- | --- | --- | --- | --- |
| hook | core | 9 | card:title | AI assistants read text that strangers write. This gateway sits between an assistant and its tools, and checks every call. |
| tools | core | 12 | clip:real-realistic:prompt,tools | Here is Claude Code, a real client, connected through the gateway. It is offered nine tools; the two that change a ticket's status or owner are held back. |
| block | core | 17 | clip:real-realistic:calls,answer | I ask it to triage the open tickets. One ticket, planted by me, tells assistants to export every customer. When Claude opens it, the gateway refuses the call, and Claude tells me it could not read it. |
| layer | core | 10 | clip:real-realistic:dashboard-layers,dashboard-decision | The dashboard names the layer that stopped it: the classifier. Every call is recorded with the layer that decided it. |
| obeys | ext | 24 | clip:real-compliant:calls,answer,dashboard-decision | What if a model does obey? In this second run I tell Claude to do the export myself. It stands in for a model that was talked into it, and the video says so. It reads all forty accounts, but the one ticket that would carry them is refused by the egress layer. No ticket is created. |
| scorecard | core | 12 | still:scorecard | To measure this, a scripted attacker runs {{attacks}} attacks across {{columns}} configurations. With every layer off, {{all_off}} succeed. With every layer on, {{all_on}} do. |
| method | ext | 16 | text:How it was checked / An independent oracle reads the databases / Each attack states its prediction first / The scorecard shows where a run differed | Each attack is judged by an independent oracle that reads the databases, not the gateway's own record. Each attack file states its prediction before any run, and the scorecard shows where reality differed. |
| chain | core | 10 | still:architecture | Every call passes a fixed chain of layers. Scope, allowlist, rate limit, schema, pinned descriptions, egress, canary, classifier. Writes wait for a person. |
| live | ext | 12 | clip:dashboard:volume-live,layers,decisions | Under simulated traffic the overview updates live, with every call, and the layer behind every refusal. |
| approvals | ext | 8 | clip:dashboard:approvals | Writes wait here for a person. The dashboard is read-only: it can show an approval, never give one. |
| limits | core | 14 | text:Honest limits / The attacker is a script, not a model / The classifier replays recordings / Slow drips still leak a few values | The gaps are real. The attacker is a script, not a model. The classifier replays recordings, including the one that caught my planted ticket. Slow drips still leak a few values. |
| end | core | 6 | card:end | Try it yourself with Docker Compose and no API key. The link is below. |
<!-- script:end -->

Lengths: the core cut is every `core` row, in order (90 seconds); the extended cut is every row (150 seconds).
