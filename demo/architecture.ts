// Draws the architecture diagram for the README, once for each theme, from one definition and the AOX
// Portfolio UI tokens. Usage: node architecture.ts   Output: docs/images/architecture-{light,dark}.svg
//
// What it shows is docs/architecture.md's request flow: bearer auth and the protocol guard (always on),
// the pipeline in its fixed order, the upstream MCP servers on a network with no way out, and the places
// everything is recorded. Edit this file and re-run it; the SVGs are generated and are not edited by hand.
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { REPO_DIR, THEMES } from "./lib.ts";
import type { Theme } from "./lib.ts";

interface Palette {
  bg: string;
  surface: string;
  muted: string;
  border: string;
  text: string;
  secondary: string;
  faint: string;
  accent: string;
  accentSoft: string;
  danger: string;
}

const PALETTES: Record<Theme, Palette> = {
  light: {
    bg: "#F6F7F8", surface: "#FFFFFF", muted: "#F0F2F4", border: "#C5CBD1", text: "#15181B",
    secondary: "#3D444B", faint: "#5B636B", accent: "#0F7A6F", accentSoft: "#E3F2EF", danger: "#B4233A",
  },
  dark: {
    bg: "#0E1012", surface: "#15181B", muted: "#1C2024", border: "#3A4148", text: "#E6E8EA",
    secondary: "#C3C8CD", faint: "#9AA1A9", accent: "#3FC1B0", accentSoft: "#12302C", danger: "#F2707F",
  },
};

const WIDTH = 1240;
const HEIGHT = 696;
const SANS = "'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', sans-serif";
const MONO = "'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace";

// The order is gateway/src/ai_gateway/pipeline/registry.py's LAYER_ORDER; the hooks are the ones each layer
// implements: canary only reads what a call sends, schema also checks a result against the pinned output schema.
const BEFORE_CALL = ["scope", "allowlist", "rate limit", "schema", "pinned", "egress", "canary", "classifier", "approval"];
const AFTER_CALL = ["schema", "egress", "classifier"];
const CHIPS_PER_ROW = 5;
const CHIP_WIDTH = 100;
const CHIP_PITCH = 108;

function escapeXml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

class Drawing {
  private readonly parts: string[] = [];
  private readonly p: Palette;

  constructor(palette: Palette) {
    this.p = palette;
  }

  box(x: number, y: number, w: number, h: number, options: { fill?: string; stroke?: string; dash?: boolean; radius?: number } = {}): void {
    const { fill = this.p.surface, stroke = this.p.border, dash = false, radius = 6 } = options;
    this.parts.push(
      `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${radius}" fill="${fill}" stroke="${stroke}" stroke-width="1.5"${dash ? ' stroke-dasharray="6 5"' : ""}/>`,
    );
  }

  text(x: number, y: number, content: string, options: { size?: number; weight?: number; color?: string; mono?: boolean; anchor?: "start" | "middle" | "end"; spacing?: number } = {}): void {
    const { size = 15, weight = 400, color = this.p.text, mono = false, anchor = "start", spacing = 0 } = options;
    this.parts.push(
      `<text x="${x}" y="${y}" font-family="${mono ? MONO : SANS}" font-size="${size}" font-weight="${weight}" fill="${color}" text-anchor="${anchor}"${spacing ? ` letter-spacing="${spacing}"` : ""}>${escapeXml(content)}</text>`,
    );
  }

  /** A layer or service as a labelled chip; `accent` marks the layers a person can see at work. */
  chip(x: number, y: number, w: number, h: number, label: string, options: { accent?: boolean; mono?: boolean } = {}): void {
    this.box(x, y, w, h, { fill: options.accent ? this.p.accentSoft : this.p.muted, stroke: options.accent ? this.p.accent : this.p.border, radius: 4 });
    this.text(x + w / 2, y + h / 2 + 5, label, { size: 14.5, weight: 500, mono: options.mono ?? true, anchor: "middle" });
  }

  /** A line with an arrowhead at its last point. */
  arrow(points: [number, number][], options: { color?: string; dash?: boolean } = {}): void {
    const { color = this.p.secondary, dash = false } = options;
    const [last, before] = [points[points.length - 1], points[points.length - 2]];
    const angle = Math.atan2(last[1] - before[1], last[0] - before[0]);
    const head = 9;
    const wing = (turn: number): string =>
      `${(last[0] - head * Math.cos(angle + turn)).toFixed(1)},${(last[1] - head * Math.sin(angle + turn)).toFixed(1)}`;
    this.parts.push(
      `<polyline points="${points.map(([x, y]) => `${x},${y}`).join(" ")}" fill="none" stroke="${color}" stroke-width="1.8"${dash ? ' stroke-dasharray="6 5"' : ""}/>`,
      `<polygon points="${last[0]},${last[1]} ${wing(0.45)} ${wing(-0.45)}" fill="${color}"/>`,
    );
  }

  svg(): string {
    return [
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${WIDTH} ${HEIGHT}" width="${WIDTH}" height="${HEIGHT}" role="img" aria-labelledby="t d">`,
      `<title id="t">AI Gateway architecture</title>`,
      `<desc id="d">AI clients connect with a bearer token to the gateway, which runs a fixed pipeline of layers before and after each call to the upstream MCP servers on an internal network. Decisions, approvals and telemetry go to PostgreSQL; a read-only dashboard shows them and a person approves writes. Harborline Supply Co. and its data are fictional.</desc>`,
      `<rect width="${WIDTH}" height="${HEIGHT}" fill="${this.p.bg}"/>`,
      ...this.parts,
      `</svg>`,
    ].join("\n");
  }
}

function draw(theme: Theme): string {
  const p = PALETTES[theme];
  const d = new Drawing(p);
  const label = { size: 12, weight: 500, color: p.faint, mono: true, spacing: 1 };

  // AI clients
  d.box(24, 150, 210, 250);
  d.text(129, 184, "AI CLIENTS", { ...label, anchor: "middle" });
  d.text(129, 222, "Claude Code,", { size: 16, weight: 600, anchor: "middle" });
  d.text(129, 244, "agents, apps", { size: 16, weight: 600, anchor: "middle" });
  d.text(129, 284, "MCP over HTTP", { size: 14, color: p.secondary, anchor: "middle" });
  d.text(129, 306, "one bearer token", { size: 14, color: p.secondary, anchor: "middle" });
  d.text(129, 328, "per client, scoped", { size: 14, color: p.secondary, anchor: "middle" });
  d.text(129, 368, "the client may be steered", { size: 14, color: p.faint, anchor: "middle" });
  d.text(129, 388, "by text it reads", { size: 14, color: p.faint, anchor: "middle" });

  // The gateway
  d.box(310, 40, 610, 520, { fill: p.surface, stroke: p.accent });
  d.text(334, 76, "AI GATEWAY", { size: 12, weight: 500, color: p.accent, mono: true, spacing: 1 });
  d.text(334, 100, "Every call is checked in a fixed order, in and out", { size: 16, weight: 600 });

  d.chip(334, 118, 168, 38, "bearer auth");
  d.chip(514, 118, 168, 38, "protocol guard");
  d.text(698, 134, "always on: no setting", { size: 13.5, color: p.faint });
  d.text(698, 151, "turns them off", { size: 13.5, color: p.faint });

  d.box(334, 176, 562, 250, { fill: p.bg });
  d.text(350, 202, "PIPELINE  ·  each layer: enforce, monitor or off", { ...label });
  d.text(350, 232, "before the call", { size: 14, weight: 600, color: p.secondary });
  BEFORE_CALL.forEach((name, i) => {
    const x = 350 + (i % CHIPS_PER_ROW) * CHIP_PITCH;
    const y = 244 + Math.floor(i / CHIPS_PER_ROW) * 48;
    d.chip(x, y, CHIP_WIDTH, 38, name, { accent: name === "approval" });
  });
  d.text(350, 358, "after the call, on the result", { size: 14, weight: 600, color: p.secondary });
  AFTER_CALL.forEach((name, i) => d.chip(350 + i * CHIP_PITCH, 368, CHIP_WIDTH, 30, name));
  d.text(350, 416, "pinned: pinned tool descriptions. tools/list is filtered by scope and pinned.", { size: 13.5, color: p.faint });

  d.box(334, 444, 562, 56, { fill: p.accentSoft, stroke: p.accent });
  d.text(350, 469, "One decision record per request", { size: 15, weight: 600 });
  d.text(350, 488, "which layer decided, how long it took; never the arguments or the results", { size: 13.5, color: p.secondary });
  d.text(334, 530, "A write waits for a person; the arguments forwarded are the ones every layer saw.", { size: 13.5, color: p.faint });

  // Upstream MCP servers
  d.box(980, 110, 236, 310, { dash: true });
  d.text(998, 140, "UPSTREAM MCP SERVERS", { ...label });
  d.text(998, 162, "internal network, no route out", { size: 13.5, color: p.secondary });
  ["ticketing", "crm", "handbook"].forEach((name, i) => d.chip(998, 182 + i * 50, 200, 38, name));
  d.text(998, 352, "Harborline Supply Co.", { size: 14, weight: 600 });
  d.text(998, 372, "(fictional), synthetic data", { size: 14, color: p.secondary });
  d.text(998, 396, "each with its own role", { size: 13.5, color: p.faint });

  // Storage and the people around it
  d.box(310, 600, 610, 84);
  d.text(334, 628, "POSTGRESQL + PGVECTOR", { ...label });
  d.text(334, 652, "policy · approvals · audit log · telemetry", { size: 15, weight: 600 });
  d.text(334, 672, "a separate database role for the gateway, the approver and the dashboard", { size: 13.5, color: p.secondary });

  d.box(24, 600, 250, 84);
  d.text(44, 628, "DASHBOARD", { ...label });
  d.text(44, 656, "read-only · signed in", { size: 15, weight: 600 });

  d.box(956, 600, 260, 84);
  d.text(976, 628, "APPROVER (A PERSON)", { ...label });
  d.text(976, 656, "own login, decides writes", { size: 15, weight: 600 });

  // Flows
  d.arrow([[234, 275], [310, 275]]);
  d.arrow([[920, 265], [980, 265]]);
  d.arrow([[615, 560], [615, 600]]);
  d.arrow([[310, 642], [274, 642]], { color: p.accent });
  d.arrow([[956, 642], [920, 642]], { color: p.accent });
  d.text(292, 592, "reads telemetry", { size: 12.5, color: p.faint, anchor: "middle" });
  d.text(938, 592, "approves", { size: 12.5, color: p.faint, anchor: "middle" });
  return d.svg();
}

const outDir = join(REPO_DIR, "docs", "images");
mkdirSync(outDir, { recursive: true });
for (const theme of THEMES) {
  const path = join(outDir, `architecture-${theme}.svg`);
  writeFileSync(path, `${draw(theme)}\n`);
  console.log(path);
}
