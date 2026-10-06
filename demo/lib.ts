// Shared helpers for the media pipeline: paths, the timeline contract, and process wrappers.
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export type Theme = "light" | "dark";
export const THEMES: Theme[] = ["light", "dark"];

/** One stretch of the recording. `speed` 1 plays it as recorded; a wait is sped up. */
export interface Scene {
  id: string;
  startMs: number;
  endMs: number;
  speed: number;
  caption: string;
  /** Part of the README GIF. A scroll changes every pixel and a GIF cannot hold that small, so the GIF
   * cuts between the still scenes and only the MP4 and WebM keep the scrolls. */
  gif: boolean;
}

export interface Timeline {
  project: string;
  theme: Theme;
  scenes: Scene[];
}

export interface Config {
  eyebrow: string;
  title: string;
  subtitle: string;
  sampleLabel: string;
  repoUrl: string;
  logos: Record<Theme, string>;
  check: { allowedRepoPattern: string; requiredText: string; requiredTextPattern: string };
}

export const DEMO_DIR = dirname(fileURLToPath(import.meta.url));
export const REPO_DIR = join(DEMO_DIR, "..");
export const OUT_DIR = join(DEMO_DIR, "out");
export const MEDIA_DIR = join(REPO_DIR, "docs", "media");
export const IMAGES_DIR = join(REPO_DIR, "docs", "images");
export const WIDTH = 1440;
export const HEIGHT = 900;
export const FPS = 30;

/** Where a theme's recording lives; a named take (the real-client clips) has a directory of its own. */
export function themeDir(theme: Theme, take = ""): string {
  return join(OUT_DIR, theme, take);
}

export function parseTheme(value: string | undefined): Theme {
  if (value === "light" || value === "dark") return value;
  throw new Error(`theme must be "light" or "dark", got ${JSON.stringify(value)}`);
}

export function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

export function readConfig(): Config {
  return readJson<Config>(join(DEMO_DIR, "config.json"));
}

export function readTimeline(theme: Theme, take = ""): Timeline {
  const timeline = readJson<Timeline>(join(themeDir(theme, take), "timeline.json"));
  for (const scene of timeline.scenes) {
    if (!(scene.endMs > scene.startMs)) throw new Error(`scene ${scene.id}: endMs must be after startMs`);
    if (!(scene.speed >= 1)) throw new Error(`scene ${scene.id}: speed must be 1 or more`);
  }
  return timeline;
}

export function run(command: string, args: string[]): string {
  const result = spawnSync(command, args, { encoding: "utf8", maxBuffer: 256 * 1024 * 1024 });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    const tail = (result.stderr || "").trim().split("\n").slice(-12).join("\n");
    throw new Error(`${command} exited ${result.status}\n${tail}`);
  }
  return result.stdout;
}

export function ffmpeg(args: string[]): void {
  run("ffmpeg", ["-hide_banner", "-loglevel", "error", "-y", ...args]);
}

export function probeDurationSeconds(path: string): number {
  const out = run("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path]);
  return Number(out.trim());
}

export function probeWidth(path: string): number {
  const out = run("ffprobe", [
    "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width", "-of", "default=nw=1:nk=1", path,
  ]);
  return Number(out.trim());
}

export function kilobytes(bytes: number): string {
  return `${(bytes / 1024).toFixed(0)} KB`;
}
