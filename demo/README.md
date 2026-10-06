# Media pipeline

Makes the pictures the README and the project's pages use, from the running demo stack, so a change to the dashboard or the scorecard can be re-shot. Harborline Supply Co. is fictional, and so is every number on screen.

```
scripts/run_dashboard_demo.sh   (the seeded demo stack, project ai-gateway-demo, ports 4400-4402)
      + traffic stream           (the simulator, read-only, as both fictional bots)
      v
record.ts (Playwright, 1440x900, video on)  -> out/<theme>/raw.webm + timeline.json
      v
edit.ts (ffmpeg)                            -> out/<theme>/dashboard.{gif,mp4,webm}
cards.ts                                    -> out/social-dark.png (1280x640)
architecture.ts                             -> docs/images/architecture-{light,dark}.svg
      v
check-frames.ts (OCR every frame)           -> refuses a hostname, address, token, prompt or internal name
      v
publish.sh                                  -> docs/media/
```

Raw recordings and edited output stay in `demo/out/` (git-ignored). Only the compressed results go to `docs/media/`.

## Run it

Needs Docker with Compose, [uv](https://docs.astral.sh/uv/), Node 22.18 or newer (it runs the `.ts` files itself), ffmpeg, ffprobe and tesseract on PATH.

```sh
cd demo
npm ci
npx playwright install chromium
./record.sh              # clean demo stack, warm-up traffic, records light and dark, takes the stack down
node edit.ts light       # and: node edit.ts dark
./publish.sh             # checks every frame it is about to publish, then copies into docs/media
node architecture.ts     # the diagram: no stack needed
```

`record.sh` makes every `docker compose` call through `scripts/run_dashboard_demo.sh`, which takes the shared Docker lock (`DOCKER_LOCK` moves it) for that one command and no longer, so do not wrap `record.sh` in `flock`. It starts from `down`, so the seeded state is the same on every take, and takes the stack down afterwards, also when a take fails. It uses only 4400-4402 on `127.0.0.1`. The stack runs in replay mode: a recording costs nothing and needs no API key.

The dashboard's strict content security policy stays on. The recorder pins the top bar and the sidebar by setting properties from script (an injected stylesheet would be refused), so the "Sample data" pill is in every frame while the page scrolls; that is a recording aid and the dashboard itself is unchanged.

## What the clip shows

1. **The overview, last hour:** the totals and the request volume as simulated traffic arrives (sped up four times, because the page refreshes every 15 seconds).
2. **Blocked by layer** and the failed sign-ins.
3. **Recent decisions:** every call with its outcome and the layer that decided it.
4. **The approval queue:** writes wait for a person; the dashboard cannot approve.

The README uses the GIFs; the MP4 and WebM of each theme are the same clip for places that can play a short silent loop (the website's pages). The GIF cuts between these four and leaves out the scrolls, because a scroll changes every pixel and a GIF cannot hold that small (about 3 MB at 800 px). The MP4 and WebM keep the scrolls. The numbers differ a little between takes, because the traffic is live; the seeded state does not.

## The real-client clip

`real-client/take.sh` makes the clips of Claude Code connected through the gateway, end to end, on a fresh demo stack: it plants the injected ticket (`real-client/plant.py`, the export attack's own ticket), runs Claude Code twice, records the dashboard after each run in both themes, and checks that no ticket was added. Needs the `claude` CLI, logged in; each run costs a few cents and the runs are not deterministic.

- **Realistic:** the prompt asks to triage the open tickets and says nothing about exporting. If Claude does not open the injected ticket the take fails: it does not film something else.
- **Compliant:** the prompt asks for the export itself, standing in for a model that was talked into it. The clip carries that label on screen for as long as it plays.

`real-client/run.sh` runs Claude Code in an empty directory with no built-in tools, no settings files and no skills, and the gateway as its only MCP server; the token is read into the one command's environment from the git-ignored `.demo/` (`scripts/run_dashboard_demo.sh tokens`), and the MCP config names `${GATEWAY_TOKEN}`. Every invocation is appended to `demo/out/real-client/runs.log`. `real-client/render.py` turns the run's event stream into the transcript the clip plays (it reformats, folds long runs of identical calls into one marked line, and refuses a token, a home directory or a path), and `real-client/record.ts` plays it in a terminal page in the portfolio tokens, then shows the dashboard with the refused row outlined. The clip is a **rendering of the recorded transcript**, not a screen capture of Claude Code (its banner shows an account and a working directory), and says so. `real-client/publish.sh` edits, checks every frame and copies into `docs/media`, with the transcripts (`real-client-*.txt`). A terminal page, and not a terminal-recording tool, keeps the pipeline to what it already needs.

## The video

`npm run video` (`video/build.ts`) reads the timing table in [`docs/video-script.md`](../docs/video-script.md) and makes, from that one source: the silent master cuts (the 90-second core and the 2:30 extension, every scene at its seconds, from the published clips in `docs/media`), the captions (`docs/video/captions-*.srt` and `.vtt`, one cue per sentence, committed) and the teleprompter cuts (a bar under the picture with each line shown 1.2 seconds early). The cuts and the teleprompter go to `out/video/` (git-ignored). `video/mux.sh <voice-file> [core|extended]` puts a recorded voice-over on a cut, with two-pass loudness normalisation, into `out/final/` (git-ignored: the finished video goes to YouTube). The script's numbers are read from `docs/scorecard.json` when the build runs.

## Checking the cleanup

`verify-cleanup.sh` starts `record.sh`, waits until its traffic is really sending, kills it, and checks that no simulator, container, volume, `.demo` directory or session file is left. It needs a few minutes and Docker.

## The frame check

`check-frames.ts` OCRs one frame a second of every video and GIF (and every PNG), enlarged, and flags:

- any IPv4 address (127.0.0.1 too), `localhost`, any hostname or URL other than this repository's own;
- a shell prompt or home path, an e-mail address and a gateway token (`gw`, an optional separator, a run of letters or digits: OCR often drops the underscore);
- any line of `.denylist.local`, a git-ignored list of names of internal machines and tools. It is copied into each worktree from the main checkout, as `.env` is, and its matches are never printed. Without it the check refuses to run (`CHECK_NO_DENYLIST=1` runs it without, and says nothing about internal names).

Files named `dashboard-*`, `social-*` and `overview-*` must also show "Sample data" in every sampled frame (a frame a second, plus the last). `node check-frames.ts --selftest` proves the check catches a dirty image and passes a clean one, and it found that an early version of the token rule missed an underscore OCR had dropped. Contact sheets of what was checked land in `out/contact/` for a person to look at.

Small, palette-reduced GIF text makes OCR misread words as addresses or prompts. `publish.sh --reviewed` accepts findings **in GIFs only**, once a person has read them and the frames they came from; it never accepts a denylist hit, a token or a missing "Sample data", and it does not touch the MP4 and WebM of the same scenes, which are larger and must be clean.

## Files

| File | What it does |
| --- | --- |
| `record.ts` | Signs in off camera, drives the dashboard, writes the video and `timeline.json` |
| `record.sh` | The stack, the traffic and both themes, under the lock per command |
| `edit.ts` | Cuts the scenes by the timeline, speeds up the waits, encodes the GIF and the videos under their size limits |
| `cards.ts`, `templates/card.html` | The social preview; its two figures are read from `docs/scorecard.json` |
| `architecture.ts` | The architecture diagram, light and dark, from one definition |
| `check-frames.ts` | The OCR check |
| `publish.sh` | Checks, then copies into `docs/media` |
| `config.json` | The card's text, the logos and the check's patterns |
