"use client";

import { useState } from "react";
import { Icon } from "./Icon";

/** Dark is the default. The choice is a cookie the server reads, so the first paint is already in
 * the right theme and nothing runs before it. */
export function ThemeToggle({ initial }: { initial: "dark" | "light" }) {
  const [theme, setTheme] = useState(initial);
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    document.cookie = `aig_theme=${next}; Path=/; Max-Age=31536000; SameSite=Strict; Secure`;
    setTheme(next);
  };
  return (
    <button type="button" className="pui-btn pui-btn--icon" onClick={toggle} aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}>
      <Icon name={theme === "dark" ? "sun" : "moon"} />
    </button>
  );
}
