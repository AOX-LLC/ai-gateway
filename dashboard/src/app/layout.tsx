import type { Metadata } from "next";
import { cookies } from "next/headers";
import type { ReactNode } from "react";
import "@/styles/portfolio-ui.css";
import "@/styles/portfolio-ui-components.css";
import "./app.css";
import { plexMono, plexSans, spaceGrotesk } from "./fonts";

export const metadata: Metadata = {
  title: "AI Gateway",
  description: "Read-only overview of the AI Gateway. Harborline Supply Co. is fictional.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: ReactNode }) {
  const theme = (await cookies()).get("aig_theme")?.value === "light" ? "light" : "dark";
  return (
    <html
      lang="en"
      data-theme={theme}
      data-accent="verdigris"
      data-density="compact"
      className={`${plexSans.variable} ${plexMono.variable} ${spaceGrotesk.variable}`}
    >
      <body>{children}</body>
    </html>
  );
}
