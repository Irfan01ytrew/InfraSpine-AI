/** Light, dark or follow-the-system color theme, remembered per browser. */
export type ThemeChoice = "light" | "dark" | "system";

const STORAGE_KEY = "infraspine_theme";

export function savedTheme(): ThemeChoice {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === "light" || value === "dark") return value;
  } catch { /* storage unavailable */ }
  return "system";
}

/** "system" removes the attribute so the stylesheet follows prefers-color-scheme. */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
}

export function setTheme(choice: ThemeChoice): void {
  applyTheme(choice);
  try {
    if (choice === "system") localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, choice);
  } catch { /* storage unavailable */ }
}
