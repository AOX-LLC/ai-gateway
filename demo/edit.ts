// Edits out/<theme>/raw.webm into the README media, driven by out/<theme>/timeline.json.
// Usage: node edit.ts <light|dark>
// Outputs (under out/<theme>/): dashboard.gif, dashboard.mp4, dashboard.webm
import { mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { FPS, HEIGHT, WIDTH, ffmpeg, kilobytes, parseTheme, probeDurationSeconds, readTimeline, themeDir } from "./lib.ts";
import type { Scene } from "./lib.ts";

const GIF_LIMIT_BYTES = 4 * 1024 * 1024;
const VIDEO_LIMIT_BYTES = 1536 * 1024;
const VIDEO_WIDTH = 1280;

interface GifStep {
  width: number;
  fps: number;
  colors: number;
  dither: string;
}

/** Tried in order; the first result under the limit is kept. */
const GIF_STEPS: GifStep[] = [
  { width: 960, fps: 10, colors: 256, dither: "none" },
  { width: 960, fps: 8, colors: 192, dither: "none" },
  { width: 880, fps: 8, colors: 128, dither: "none" },
  { width: 800, fps: 6, colors: 96, dither: "none" },
];

const sizeOf = (path: string) => statSync(path).size;
const seconds = (ms: number) => (ms / 1000).toFixed(3);

// Intermediate clips are near-lossless so the one lossy encode happens at the end.
function buildSceneClip(raw: string, work: string, scene: Scene): string {
  const path = join(work, `scene-${scene.id}.mp4`);
  const filter = `setpts=(PTS-STARTPTS)/${scene.speed},fps=${FPS},scale=${WIDTH}:${HEIGHT}:flags=lanczos,format=yuv420p`;
  ffmpeg([
    "-ss", seconds(scene.startMs), "-t", String((scene.endMs - scene.startMs) / 1000), "-i", raw,
    "-vf", filter, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", path,
  ]);
  return path;
}

// Concat demuxer with stream copy: every clip shares codec, size and frame rate.
function concatClips(clips: string[], out: string, work: string): void {
  const list = join(work, `${out.split("/").pop()}.txt`);
  writeFileSync(list, clips.map((clip) => `file '${clip}'\n`).join(""));
  ffmpeg(["-f", "concat", "-safe", "0", "-i", list, "-c", "copy", out]);
}

function encodeGif(source: string, step: GifStep, palette: string, out: string): void {
  const scale = `fps=${step.fps},scale=${step.width}:-1:flags=lanczos`;
  ffmpeg(["-i", source, "-vf", `${scale},palettegen=max_colors=${step.colors}:stats_mode=diff`, palette]);
  ffmpeg([
    "-i", source, "-i", palette, "-lavfi",
    `${scale}[x];[x][1:v]paletteuse=dither=${step.dither}:diff_mode=rectangle`, "-loop", "0", out,
  ]);
}

function makeGif(source: string, work: string, out: string): void {
  for (const step of GIF_STEPS) {
    encodeGif(source, step, join(work, "palette.png"), out);
    const bytes = sizeOf(out);
    console.log(`gif: ${step.width}px ${step.fps}fps ${step.colors} colours, ${step.dither} dither -> ${kilobytes(bytes)}`);
    if (bytes <= GIF_LIMIT_BYTES) return;
  }
  throw new Error(`the gif is still over ${kilobytes(GIF_LIMIT_BYTES)} at the smallest step: shorten the scenes`);
}

/** Tries each quality in turn and stops at the first result under the limit. */
function encodeUnder(qualities: number[], build: (quality: number) => void, out: string): void {
  for (const quality of qualities) {
    build(quality);
    if (sizeOf(out) <= VIDEO_LIMIT_BYTES) return;
  }
  throw new Error(`${out} is still over ${kilobytes(VIDEO_LIMIT_BYTES)} at the lowest quality`);
}

function makeVideos(source: string, mp4: string, webm: string): void {
  const scale = `scale=${VIDEO_WIDTH}:-2:flags=lanczos`;
  encodeUnder([28, 31, 34, 37], (crf) => ffmpeg([
    "-i", source, "-vf", scale, "-an", "-c:v", "libx264", "-preset", "slow", "-crf", String(crf),
    "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4,
  ]), mp4);
  encodeUnder([36, 40, 44, 48], (crf) => ffmpeg([
    "-i", source, "-vf", scale, "-an", "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", String(crf),
    "-row-mt", "1", "-pix_fmt", "yuv420p", webm,
  ]), webm);
  console.log(`video: mp4 ${kilobytes(sizeOf(mp4))}, webm ${kilobytes(sizeOf(webm))}`);
}

function main(): void {
  const theme = parseTheme(process.argv[2]);
  const dir = themeDir(theme);
  const work = join(dir, ".work");
  rmSync(work, { recursive: true, force: true });
  mkdirSync(work, { recursive: true });
  const { scenes } = readTimeline(theme);
  if (scenes.length === 0) throw new Error("the timeline has no scenes");
  const raw = join(dir, "raw.webm");
  const source = join(work, "source.mp4");
  const clips = scenes.map((scene) => ({ scene, path: buildSceneClip(raw, work, scene) }));
  concatClips(clips.map((clip) => clip.path), source, work);
  const gifSource = join(work, "gif-source.mp4");
  concatClips(clips.filter((clip) => clip.scene.gif).map((clip) => clip.path), gifSource, work);
  console.log(`${theme}: ${scenes.length} scenes, ${probeDurationSeconds(source).toFixed(1)} s (gif ${probeDurationSeconds(gifSource).toFixed(1)} s)`);
  makeGif(gifSource, work, join(dir, "dashboard.gif"));
  makeVideos(source, join(dir, "dashboard.mp4"), join(dir, "dashboard.webm"));
  rmSync(work, { recursive: true, force: true });
}

main();
