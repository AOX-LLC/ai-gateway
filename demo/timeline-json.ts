// Writes the published form of a recording's timeline: each scene's id and its length in the edited clip
// (the recorded length divided by its speed). The video builder cuts clips by scene name from this, so a
// cut can be rebuilt from docs/media alone.
// Usage: node timeline-json.ts <light|dark> <take or ""> <out.json>
import { writeFileSync } from "node:fs";
import { parseTheme, readTimeline } from "./lib.ts";

const [, , themeArgument, take = "", out] = process.argv;
if (!out) throw new Error('usage: node timeline-json.ts <light|dark> <take or ""> <out.json>');
const { scenes } = readTimeline(parseTheme(themeArgument), take);
const published = scenes.map((scene) => ({
  id: scene.id,
  seconds: Number(((scene.endMs - scene.startMs) / 1000 / scene.speed).toFixed(3)),
}));
writeFileSync(out, `${JSON.stringify({ scenes: published }, null, 2)}\n`);
console.log(`${out}: ${published.length} scenes`);
