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

The GIF cuts between these four and leaves out the scrolls, because a scroll changes every pixel and a GIF cannot hold that small (about 3 MB at 800 px). The MP4 and WebM keep the scrolls. The numbers differ a little between takes, because the traffic is live; the seeded state does not.

## The frame check

`check-frames.ts` OCRs one frame a second of every video and GIF (and every PNG), enlarged, and flags:

- any IPv4 address (127.0.0.1 too), `localhost`, any hostname or URL other than this repository's own;
- a shell prompt or home path, an e-mail address and a gateway token (`gw`, an optional separator, a run of letters or digits: OCR often drops the underscore);
- any line of `.denylist.local`, a git-ignored list of names of internal machines and tools. It is copied into each worktree from the main checkout, as `.env` is, and its matches are never printed. Without it the check refuses to run (`CHECK_NO_DENYLIST=1` runs it without, and says nothing about internal names).

Files named `dashboard-*`, `social-*` and `overview-*` must also show "Sample data" in every distinct frame. `node check-frames.ts --selftest` proves the check catches a dirty image and passes a clean one, and it found that an early version of the token rule missed an underscore OCR had dropped. Contact sheets of what was checked land in `out/contact/` for a person to look at.

Small, palette-reduced GIF text makes OCR misread words as addresses or prompts. `publish.sh --reviewed` passes the findings to the check as read and accepted: use it only after reading every finding and the frames they came from. The MP4 and WebM of the same scenes, which are larger, are checked too and must be clean.

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
