import localFont from "next/font/local";

/** The three families of the UI system, self-hosted: nothing is fetched from a font service. Each is
 * licensed under the SIL Open Font License 1.1 (see THIRD_PARTY_NOTICES.md). */

export const plexSans = localFont({
  src: [
    { path: "../fonts/ibm-plex-sans/IBMPlexSans-Regular-Latin1.woff2", weight: "400", style: "normal" },
    { path: "../fonts/ibm-plex-sans/IBMPlexSans-Italic-Latin1.woff2", weight: "400", style: "italic" },
    { path: "../fonts/ibm-plex-sans/IBMPlexSans-Medium-Latin1.woff2", weight: "500", style: "normal" },
    { path: "../fonts/ibm-plex-sans/IBMPlexSans-SemiBold-Latin1.woff2", weight: "600", style: "normal" },
  ],
  variable: "--font-plex-sans",
  display: "swap",
});

export const plexMono = localFont({
  src: [
    { path: "../fonts/ibm-plex-mono/IBMPlexMono-Regular-Latin1.woff2", weight: "400", style: "normal" },
    { path: "../fonts/ibm-plex-mono/IBMPlexMono-Medium-Latin1.woff2", weight: "500", style: "normal" },
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
