/** Outline icons in the UI system's style (24-unit grid, 1.75 stroke, currentColor), one place for
 * every path. Decorative: the words next to them carry the meaning. */

const PATHS = {
  activity: ["M3 12h4l3 8l4 -16l3 8h4"],
  list: ["M9 6h11", "M9 12h11", "M9 18h11", "M5 6v.01", "M5 12v.01", "M5 18v.01"],
  clock: ["M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0", "M12 7v5l3 3"],
  check: ["M5 12l5 5l10 -10"],
  ban: ["M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0", "M5.7 5.7l12.6 12.6"],
  alert: ["M12 9v4", "M12 16h.01", "M10.3 3.9l-8.4 14.5a1.9 1.9 0 0 0 1.6 2.9h16.9a1.9 1.9 0 0 0 1.6 -2.9l-8.5 -14.5a1.9 1.9 0 0 0 -3.2 0z"],
  info: ["M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0", "M12 9h.01", "M11 12h1v4h1"],
  x: ["M18 6l-12 12", "M6 6l12 12"],
  sun: ["M8 12a4 4 0 1 0 8 0a4 4 0 1 0 -8 0", "M3 12h1", "M12 3v1", "M20 12h1", "M12 20v1", "M5.6 5.6l.7 .7", "M18.4 5.6l-.7 .7", "M17.7 17.7l.7 .7", "M6.3 17.7l-.7 .7"],
  moon: ["M12 3a6 6 0 0 0 9 9a9 9 0 1 1 -9 -9"],
  logout: ["M14 8v-2a2 2 0 0 0 -2 -2h-7a2 2 0 0 0 -2 2v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2 -2v-2", "M9 12h12l-3 -3", "M18 15l3 -3"],
  left: ["M15 6l-6 6l6 6"],
  right: ["M9 6l6 6l-6 6"],
  inbox: ["M4 4h16v12h-5l-1 2h-4l-1 -2h-4z", "M4 12h4l1 2h6l1 -2h4"],
  coin: ["M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0", "M14.8 9a2 2 0 0 0 -1.8 -1h-2a2 2 0 0 0 0 4h2a2 2 0 0 1 0 4h-2a2 2 0 0 1 -1.8 -1", "M12 6v2", "M12 16v2"],
  refresh: ["M20 11a8.1 8.1 0 0 0 -15.5 -2m-.5 -4v4h4", "M4 13a8.1 8.1 0 0 0 15.5 2m.5 4v-4h-4"],
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size }: { name: IconName; size?: "sm" | "lg" }) {
  const className = size ? `pui-icon pui-icon--${size}` : "pui-icon";
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      {PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
