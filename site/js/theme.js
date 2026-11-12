/* Runs in the head, before the page paints: marks the page as scripted and applies the stored theme to the data-theme attribute (light or dark; none means auto). */
(() => {
  const root = document.documentElement;
  root.classList.add("js");
  try {
    const choice = globalThis.localStorage.getItem("venom-theme");
    if (choice === "light" || choice === "dark") {
      root.dataset.theme = choice;
    }
  } catch {
    delete root.dataset.theme;
  }
})();
