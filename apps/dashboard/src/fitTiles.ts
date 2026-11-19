/** A big number never leaves its tile: if it is wider than the tile, its font shrinks until it fits. */
const SELECTOR = ".tile-value";
let queued = false;

function fit() {
  queued = false;
  document.querySelectorAll<HTMLElement>(SELECTOR).forEach((element) => {
    element.style.fontSize = "";
    element.style.whiteSpace = "nowrap";
    const natural = parseFloat(getComputedStyle(element).fontSize);
    if (element.scrollWidth <= element.clientWidth + 1 || !element.clientWidth) return;
    let size = Math.max(16, Math.floor(natural * element.clientWidth / element.scrollWidth));
    element.style.fontSize = `${size}px`;
    // measure again: the width does not scale exactly with the size (letter spacing, tabular figures)
    while (element.scrollWidth > element.clientWidth + 1 && size > 16) { size -= 1; element.style.fontSize = `${size}px`; }
  });
}

const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(fit); } };

export function fitTiles() {
  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true, characterData: true });
  window.addEventListener("resize", schedule);
  schedule();
  void document.fonts?.ready.then(schedule);
}
