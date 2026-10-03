import localFont from "next/font/local";

/** The three families of the UI system, self-hosted: nothing is fetched from a font service. Each is
 * licensed under the SIL Open Font License 1.1 (the notices come with Phase 5c-3). */

export const plexSans = localFont({
  src: [
    { path: "../fonts/ibm-plex-sans/ibm-plex-sans-latin-400-normal.woff2", weight: "400", style: "normal" },
    { path: "../fonts/ibm-plex-sans/ibm-plex-sans-latin-400-italic.woff2", weight: "400", style: "italic" },
    { path: "../fonts/ibm-plex-sans/ibm-plex-sans-latin-500-normal.woff2", weight: "500", style: "normal" },
    { path: "../fonts/ibm-plex-sans/ibm-plex-sans-latin-600-normal.woff2", weight: "600", style: "normal" },
  ],
  variable: "--font-plex-sans",
  display: "swap",
});

export const plexMono = localFont({
  src: [
    { path: "../fonts/ibm-plex-mono/ibm-plex-mono-latin-400-normal.woff2", weight: "400", style: "normal" },
    { path: "../fonts/ibm-plex-mono/ibm-plex-mono-latin-500-normal.woff2", weight: "500", style: "normal" },
  ],
  variable: "--font-plex-mono",
  display: "swap",
});

export const spaceGrotesk = localFont({
  src: [
    { path: "../fonts/space-grotesk/space-grotesk-latin-500-normal.woff2", weight: "500", style: "normal" },
    { path: "../fonts/space-grotesk/space-grotesk-latin-600-normal.woff2", weight: "600", style: "normal" },
  ],
  variable: "--font-space-grotesk",
  display: "swap",
});
