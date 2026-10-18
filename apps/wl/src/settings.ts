// Viewer settings (theme, font, size, chat style, …). Palettes come from the
// Figma "Terminal Window UI – 485 Themes" reference: BG, FG, cursor, selection
// and ANSI 0–15. Settings live in this browser only (localStorage).

export type Theme = { id: string; name: string; light: boolean; bg: string; fg: string; cursor: string; selection: string; ansi: string[] };

const theme = (id: string, name: string, light: boolean, [bg, fg, cursor, selection]: string[], ansi: string): Theme =>
  ({ id, name, light, bg, fg, cursor, selection, ansi: ansi.split(" ").map((hex) => `#${hex}`) });

export const THEMES: Theme[] = [
  // The entry screen's colours (white on #007BE5). Its ANSI colours are light so that
  // accents, author colours and states stay readable on the blue.
  theme("laplace-blue", "Laplace Blue", false, ["#007be5", "#ffffff", "#ffffff", "#0a5fb4"],
    "003d73 ffb4b4 a6ffcb ffe486 d6ecff ffc2f1 9ff4ff eaf4ff 7cbcf5 ffcaca c4ffdb fff0b3 ebf6ff ffd6f6 c4f9ff ffffff"),
  theme("dracula", "Dracula", false, ["#282a36", "#f8f8f2", "#f8f8f2", "#44475a"],
    "21222c ff5555 50fa7b f1fa8c bd93f9 ff79c6 8be9fd f8f8f2 6272a4 ff6e6e 69ff94 ffffa5 d6acff ff92df a4ffff ffffff"),
  theme("tokyonight", "TokyoNight", false, ["#1a1b26", "#c0caf5", "#c0caf5", "#33467c"],
    "15161e f7768e 9ece6a e0af68 7aa2f7 bb9af7 7dcfff a9b1d6 414868 f7768e 9ece6a e0af68 7aa2f7 bb9af7 7dcfff c0caf5"),
  theme("catppuccin-mocha", "Catppuccin Mocha", false, ["#1e1e2e", "#cdd6f4", "#f5e0dc", "#585b70"],
    "45475a f38ba8 a6e3a1 f9e2af 89b4fa f5c2e7 94e2d5 a6adc8 585b70 f37799 89d88b ebd391 74a8fc f2aede 6bd7ca bac2de"),
  theme("nord", "Nord", false, ["#2e3440", "#d8dee9", "#eceff4", "#434c5e"],
    "3b4252 bf616a a3be8c ebcb8b 81a1c1 b48ead 88c0d0 e5e9f0 596377 bf616a a3be8c ebcb8b 81a1c1 b48ead 8fbcbb eceff4"),
  theme("gruvbox-dark", "Gruvbox Dark", false, ["#282828", "#ebdbb2", "#ebdbb2", "#665c54"],
    "282828 cc241d 98971a d79921 458588 b16286 689d6a a89984 928374 fb4934 b8bb26 fabd2f 83a598 d3869b 8ec07c ebdbb2"),
  theme("solarized-dark", "Solarized Dark", false, ["#002b36", "#839496", "#839496", "#073642"],
    "073642 dc322f 859900 b58900 268bd2 d33682 2aa198 eee8d5 335e69 cb4b16 586e75 657b83 839496 6c71c4 93a1a1 fdf6e3"),
  theme("rose-pine", "Rosé Pine", false, ["#191724", "#e0def4", "#e0def4", "#403d52"],
    "26233a eb6f92 31748f f6c177 9ccfd8 c4a7e7 ebbcba e0def4 6e6a86 eb6f92 31748f f6c177 9ccfd8 c4a7e7 ebbcba e0def4"),
  theme("monokai-pro", "Monokai Pro", false, ["#2d2a2e", "#fcfcfa", "#c1c0c0", "#5b595c"],
    "2d2a2e ff6188 a9dc76 ffd866 fc9867 ab9df2 78dce8 fcfcfa 727072 ff6188 a9dc76 ffd866 fc9867 ab9df2 78dce8 fcfcfa"),
  theme("atom-one-dark", "Atom One Dark", false, ["#21252b", "#abb2bf", "#abb2bf", "#323844"],
    "21252b e06c75 98c379 e5c07b 61afef c678dd 56b6c2 abb2bf 767676 e06c75 98c379 e5c07b 61afef c678dd 56b6c2 abb2bf"),
  theme("github-dark", "GitHub Dark", false, ["#0d1117", "#e6edf3", "#2f81f7", "#264f78"],
    "484f58 ff7b72 3fb950 d29922 58a6ff bc8cff 39c5cf b1bac4 6e7681 ffa198 56d364 e3b341 79c0ff d2a8ff 56d4dd ffffff"),
  theme("github-light", "GitHub Light", true, ["#ffffff", "#1f2328", "#0969da", "#b6d7ff"],
    "24292f cf222e 116329 4d2d00 0969da 8250df 1b7c83 6e7781 57606a a40e26 1a7f37 633c01 218bff a475f9 3192aa 8c959f"),
  theme("solarized-light", "Solarized Light", true, ["#fdf6e3", "#657b83", "#657b83", "#eee8d5"],
    "073642 dc322f 859900 b58900 268bd2 d33682 2aa198 bbb5a2 002b36 cb4b16 586e75 657b83 839496 6c71c4 93a1a1 fdf6e3"),
  theme("catppuccin-latte", "Catppuccin Latte", true, ["#eff1f5", "#4c4f69", "#dc8a78", "#acb0be"],
    "5c5f77 d20f39 40a02b df8e1d 1e66f5 ea76cb 179299 acb0be 6c6f85 de293e 49af3d eea02d 456eff fe85d8 2d9fa8 bcc0cc"),
];

export type Settings = {
  theme: string;            // "system" = the original light/dark reading theme
  font: "sans" | "mono";
  zoom: number;             // reading area scale, %
  chat: "bubble" | "log";
  autoRefresh: boolean;
  home: string;             // page shown when the URL has no #/page
  author: "all" | "CLAUDE" | "CODEX";
  status: "all" | "open" | "disputed" | "verified";  // chat: claims by current status (+ their replies)
  order: "asc" | "desc";
  collapse: boolean;        // fold detail / note under a "자세히" toggle
  notify: boolean;          // count new records in the tab title while the tab is hidden
};

export const DEFAULTS: Settings = { theme: "laplace-blue", font: "sans", zoom: 100, chat: "bubble", autoRefresh: true, home: "overview",
  author: "all", status: "all", order: "asc", collapse: false, notify: true };
export const ZOOMS = [90, 100, 110, 125];
const KEY = "wl.settings";

/** Keep only known values, so an old or hand-edited entry can't break the page. */
export function normalize(raw: unknown, pages: string[] = []): Settings {
  const value = raw && typeof raw === "object" ? raw as Record<string, unknown> : {};
  const pick = <T>(ok: (v: unknown) => boolean, key: keyof Settings): T => (ok(value[key]) ? value[key] : DEFAULTS[key]) as T;
  return {
    theme: pick((v) => v === "system" || THEMES.some((t) => t.id === v), "theme"),
    font: pick((v) => v === "sans" || v === "mono", "font"),
    zoom: pick((v) => ZOOMS.includes(v as number), "zoom"),
    chat: pick((v) => v === "bubble" || v === "log", "chat"),
    autoRefresh: pick((v) => typeof v === "boolean", "autoRefresh"),
    home: pick((v) => typeof v === "string" && (!pages.length || pages.includes(v)), "home"),
    author: pick((v) => v === "all" || v === "CLAUDE" || v === "CODEX", "author"),
    status: pick((v) => v === "all" || v === "open" || v === "disputed" || v === "verified", "status"),
    order: pick((v) => v === "asc" || v === "desc", "order"),
    collapse: pick((v) => typeof v === "boolean", "collapse"),
    notify: pick((v) => typeof v === "boolean", "notify"),
  };
}

export function load(pages: string[]): Settings {
  try { return normalize(JSON.parse(localStorage.getItem(KEY) ?? "null"), pages); } catch { return { ...DEFAULTS }; }
}

export function save(settings: Settings) {
  try { localStorage.setItem(KEY, JSON.stringify(settings)); } catch { /* private mode: settings last for this visit */ }
}

/** Put the settings on <html>: palette as CSS variables, the rest as data attributes. */
export function apply(settings: Settings) {
  const root = document.documentElement;
  const palette = THEMES.find((t) => t.id === settings.theme);
  const vars = ["--t-bg", "--t-fg", "--t-cursor", "--t-sel", ...Array.from({ length: 16 }, (_, i) => `--a${i}`)];
  if (palette) {
    [palette.bg, palette.fg, palette.cursor, palette.selection, ...palette.ansi].forEach((color, i) => root.style.setProperty(vars[i], color));
    root.dataset.term = palette.light ? "light" : "dark";
  } else {
    for (const name of vars) root.style.removeProperty(name);
    delete root.dataset.term;
  }
  root.dataset.theme = settings.theme;  // lets a theme fine-tune what the palette alone cannot
  root.dataset.font = settings.font;
  root.dataset.chat = settings.chat;
  root.style.setProperty("--zoom", String(settings.zoom / 100));
}
