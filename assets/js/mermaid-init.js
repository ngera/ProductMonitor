/**
 * Turn Jekyll/Rouge ```mermaid fences into rendered diagrams.
 * GitHub.com renders these natively; GitHub Pages does not.
 */
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";

function isDark() {
  const root = document.documentElement;
  const attr = root.getAttribute("data-theme") || root.getAttribute("data-color-mode");
  if (attr === "dark" || attr === "light") return attr === "dark";
  if (root.classList.contains("dark")) return true;
  if (root.classList.contains("light")) return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

document.addEventListener("DOMContentLoaded", async () => {
  const blocks = document.querySelectorAll("pre > code.language-mermaid");
  if (!blocks.length) return;

  const nodes = [];
  blocks.forEach((block) => {
    const pre = block.parentElement;
    if (!pre) return;
    const container = document.createElement("div");
    container.className = "mermaid";
    container.textContent = block.textContent;
    pre.replaceWith(container);
    nodes.push(container);
  });

  mermaid.initialize({
    startOnLoad: false,
    theme: isDark() ? "dark" : "neutral",
    securityLevel: "strict",
    themeVariables: {
      fontSize: "18px",
      fontFamily: "var(--font-ui, system-ui, sans-serif)",
    },
    flowchart: {
      htmlLabels: true,
      curve: "basis",
      useMaxWidth: true,
      nodeSpacing: 36,
      rankSpacing: 48,
      padding: 20,
    },
  });

  await mermaid.run({ nodes, suppressErrors: true });
});
