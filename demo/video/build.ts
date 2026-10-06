// Builds the video's silent cuts, captions and teleprompter from docs/video-script.md, in one command:
//   node video/build.ts        (npm run video)
// Output: out/video/silent-{core,extended}.mp4 and teleprompter-{core,extended}.mp4 (git-ignored), and
// ../docs/video/captions-{core,extended}.{srt,vtt} (committed). Every scene is cut to exactly its seconds in
// the script; the clips are the published ones in docs/media (and their timelines), so the cut can be
// rebuilt from the repository alone.
import { mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
import { FPS, HEIGHT, MEDIA_DIR, REPO_DIR, WIDTH, ffmpeg, kilobytes, probeDurationSeconds, readJson, run } from "../lib.ts";
import { VIDEO_OUT, renderFrames } from "./frames.ts";
import {
  WORDS_PER_SECOND_MAX, WORDS_PER_SECOND_WARN, cuesFor, loadFacts, loadScript, scenesOf, totalSeconds,
  wordsPerSecond,
} from "./script.ts";
import type { Cue, Cut, ScriptScene } from "./script.ts";

const CUTS: Cut[] = ["core", "extended"];
const CAPTIONS_DIR = join(REPO_DIR, "docs", "video");
const CLIP_FILES: Record<string, string> = {
  "real-realistic": "real-client-realistic-dark",
  "real-compliant": "real-client-compliant-dark",
  dashboard: "dashboard-dark",
};
const MAX_SPEED_UP = 2;
const ENCODE = ["-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", "-r", String(FPS)];
const FIT = `scale=${WIDTH}:${HEIGHT}:flags=lanczos,fps=${FPS}`;

/** Teleprompter: the cut on top, a bar under it, each line shown LEAD_S before it is spoken. */
const BAR_HEIGHT = 300;
const LEAD_S = 1.2;
/** The best installed bold sans, wherever the distribution keeps it (fontconfig knows; a path would not travel). */
const PROMPT_FONT = run("fc-match", ["-f", "%{file}", "DejaVu Sans:bold"]).trim();
const PROMPT_CHARS_PER_LINE = 44;
const PROMPT_SIZE = 46;
const NEXT_SIZE = 26;
const BAR_COLOUR = "0x15181B";

interface ClipTimeline {
  scenes: { id: string; seconds: number }[];
}

function clipPieces(source: string): { file: string; start: number; length: number }[] {
  const [, name, ids] = source.split(":");
  const base = CLIP_FILES[name];
  if (!base) throw new Error(`no clip named ${name}: use ${Object.keys(CLIP_FILES).join(", ")}`);
  const file = join(MEDIA_DIR, `${base}.mp4`);
  const timeline = readJson<ClipTimeline>(join(MEDIA_DIR, `${base}.timeline.json`));
  return ids.split(",").map((id) => {
    let start = 0;
    for (const scene of timeline.scenes) {
      if (scene.id === id) return { file, start, length: scene.seconds };
      start += scene.seconds;
    }
    throw new Error(`clip ${name} has no scene ${id}`);
  });
}

function fromImage(png: string, seconds: number, out: string): void {
  ffmpeg(["-loop", "1", "-framerate", String(FPS), "-t", String(seconds), "-i", png, "-vf", FIT, ...ENCODE, out]);
}

/** The selected scenes of a clip, joined, then fitted to the scene: a short one holds its last frame, a long
 * one is sped up (to at most MAX_SPEED_UP). */
function fromClip(source: string, seconds: number, out: string, work: string): void {
  const pieces = clipPieces(source).map((piece, index) => {
    const path = join(work, `${basename(out)}.piece${index}.mp4`);
    ffmpeg(["-ss", piece.start.toFixed(3), "-t", piece.length.toFixed(3), "-i", piece.file, "-vf", FIT, ...ENCODE, path]);
    return path;
  });
  const list = join(work, `${basename(out)}.txt`);
  writeFileSync(list, pieces.map((piece) => `file '${piece}'\n`).join(""));
  const joined = join(work, `${basename(out)}.joined.mp4`);
  ffmpeg(["-f", "concat", "-safe", "0", "-i", list, "-c", "copy", joined]);
  const length = probeDurationSeconds(joined);
  const factor = length / seconds;
  if (factor > MAX_SPEED_UP) throw new Error(`${source} is ${length.toFixed(1)} s: too long for ${seconds} s (at most ${MAX_SPEED_UP}x)`);
  const fit = factor > 1 ? `setpts=PTS/${factor.toFixed(4)}` : `tpad=stop_mode=clone:stop_duration=${(seconds - length).toFixed(3)}`;
  ffmpeg(["-i", joined, "-vf", `${fit},fps=${FPS}`, "-t", String(seconds), ...ENCODE, out]);
}

function concat(paths: string[], out: string, work: string): void {
  const list = join(work, `${basename(out)}.list.txt`);
  writeFileSync(list, paths.map((path) => `file '${path}'\n`).join(""));
  ffmpeg(["-f", "concat", "-safe", "0", "-i", list, "-c", "copy", "-movflags", "+faststart", out]);
}

function cueTime(seconds: number, separator: "," | "."): string {
  const ms = Math.round(seconds * 1000);
  const pad = (value: number, width: number) => String(value).padStart(width, "0");
  return `${pad(Math.floor(ms / 3_600_000), 2)}:${pad(Math.floor((ms % 3_600_000) / 60_000), 2)}:${pad(Math.floor((ms % 60_000) / 1000), 2)}${separator}${pad(ms % 1000, 3)}`;
}

function writeCaptions(cues: Cue[], basePath: string): void {
  const srt = cues.map((cue, i) => `${i + 1}\n${cueTime(cue.start, ",")} --> ${cueTime(cue.end, ",")}\n${cue.text}\n`).join("\n");
  const vtt = cues.map((cue) => `${cueTime(cue.start, ".")} --> ${cueTime(cue.end, ".")}\n${cue.text}\n`).join("\n");
  writeFileSync(`${basePath}.srt`, srt);
  writeFileSync(`${basePath}.vtt`, `WEBVTT\n\n${vtt}`);
}

function wrapLine(text: string): string {
  const lines: string[] = [];
  let line = "";
  for (const word of text.split(/\s+/)) {
    if (line !== "" && `${line} ${word}`.length > PROMPT_CHARS_PER_LINE) {
      lines.push(line);
      line = word;
    } else {
      line = line === "" ? word : `${line} ${word}`;
    }
  }
  return [...lines, line].join("\n");
}

function drawText(file: string, size: number, colour: string, y: number, enable: string): string {
  return `drawtext=fontfile=${PROMPT_FONT}:textfile=${file}:expansion=none:fontsize=${size}:fontcolor=${colour}:x=60:y=${y}:line_spacing=12:enable='${enable}'`;
}

/** The silent cut with a bar under it: the line to speak now (large), the one after it (small), a clock. */
function teleprompter(silent: string, cues: Cue[], out: string, work: string): void {
  const filters = [`pad=${WIDTH}:${HEIGHT + BAR_HEIGHT}:0:0:color=${BAR_COLOUR}`];
  cues.forEach((cue, index) => {
    const show = Math.max(0, cue.start - LEAD_S);
    const hide = index + 1 < cues.length ? Math.max(show, cues[index + 1].start - LEAD_S) : cue.end + 1;
    const window = `between(t,${show.toFixed(3)},${hide.toFixed(3)})`;
    const nowFile = join(work, `now-${index}.txt`);
    writeFileSync(nowFile, wrapLine(cue.text));
    filters.push(drawText(nowFile, PROMPT_SIZE, "white", HEIGHT + 22, window));
    if (index + 1 < cues.length) {
      const nextFile = join(work, `next-${index}.txt`);
      writeFileSync(nextFile, `next: ${cues[index + 1].text.slice(0, 80)}`);
      filters.push(drawText(nextFile, NEXT_SIZE, "0x9AA1A9", HEIGHT + BAR_HEIGHT - 44, window));
    }
  });
  const clock = `drawtext=fontfile=${PROMPT_FONT}:text='%{pts\\:hms}':fontsize=26:fontcolor=0x3FC1B0:x=w-350:y=${HEIGHT + BAR_HEIGHT - 44}`;
  filters.push(clock);
  const script = join(work, `${basename(out)}.filters.txt`);
  writeFileSync(script, filters.join(",\n"));
  ffmpeg(["-i", silent, "-filter_script:v", script, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]);
}

function checkPace(scenes: ScriptScene[], facts: Record<string, string>): void {
  for (const scene of scenes) {
    const pace = wordsPerSecond(scene, facts);
    if (pace > WORDS_PER_SECOND_MAX) throw new Error(`scene ${scene.id}: ${pace.toFixed(2)} words a second is too fast to say`);
    if (pace > WORDS_PER_SECOND_WARN) console.warn(`warning: scene ${scene.id} is ${pace.toFixed(2)} words a second: brisk`);
  }
}

async function main(): Promise<void> {
  const facts = loadFacts();
  const scenes = loadScript();
  checkPace(scenes, facts);
  const work = join(VIDEO_OUT, ".work");
  rmSync(work, { recursive: true, force: true });
  mkdirSync(work, { recursive: true });
  mkdirSync(CAPTIONS_DIR, { recursive: true });
  const frameSources = scenes.map((scene) => scene.source).filter((source) => !source.startsWith("clip:"));
  const frames = await renderFrames(frameSources, facts);
  const segments = new Map<string, string>();
  for (const scene of scenes) {
    const out = join(work, `seg-${scene.id}.mp4`);
    if (scene.source.startsWith("clip:")) fromClip(scene.source, scene.seconds, out, work);
    else fromImage(frames.get(scene.source) as string, scene.seconds, out);
    segments.set(scene.id, out);
  }
  for (const cut of CUTS) {
    const chosen = scenesOf(scenes, cut);
    const silent = join(VIDEO_OUT, `silent-${cut}.mp4`);
    concat(chosen.map((scene) => segments.get(scene.id) as string), silent, work);
    const cues = cuesFor(chosen, facts);
    writeCaptions(cues, join(CAPTIONS_DIR, `captions-${cut}`));
    const prompter = join(VIDEO_OUT, `teleprompter-${cut}.mp4`);
    teleprompter(silent, cues, prompter, work);
    console.log(
      `${cut}: ${chosen.length} scenes, ${totalSeconds(chosen)} s script, ${probeDurationSeconds(silent).toFixed(1)} s video, ` +
        `${cues.length} cues; silent ${kilobytes(statSync(silent).size)}, teleprompter ${kilobytes(statSync(prompter).size)}`,
    );
  }
  rmSync(work, { recursive: true, force: true });
}

main().catch((error) => {
  console.error(String(error instanceof Error ? error.message : error).split("\n").slice(0, 6).join("\n"));
  process.exit(1);
});
