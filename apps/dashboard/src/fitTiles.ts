/** A big number never leaves its tile: if it is wider than the tile, its font shrinks until it fits.
 *  Only tiles that were added, changed text or changed width are refitted, and every pass reads all
 *  layout first and then writes all styles, so the browser lays out once per pass instead of once per tile. */
const SELECTOR = ".tile-value";
const MIN = 16;
const dirty = new Set<HTMLElement>();
const widths = new WeakMap<Element, number>();
const watched = new WeakSet<Element>();
let queued = false;
let resizer: ResizeObserver | undefined;

function fit() {
  queued = false;
  const items = [...dirty].filter((element) => element.isConnected);
  dirty.clear();
  if (!items.length) return;
  // write: reset
  for (const element of items) { element.style.fontSize = ""; element.style.whiteSpace = "nowrap"; }
  // read: one layout for all tiles
  const measured = items.map((element) => ({ element, natural: parseFloat(getComputedStyle(element).fontSize), scroll: element.scrollWidth, client: element.clientWidth }));
  // write: first estimate
  let open: { element: HTMLElement; size: number }[] = [];
  for (const { element, natural, scroll, client } of measured) {
    if (!client || scroll <= client + 1) continue;
    const size = Math.max(MIN, Math.floor(natural * client / scroll));
    open.push({ element, size });
  }
  for (const { element, size } of open) element.style.fontSize = `${size}px`;
  // the width does not scale exactly with the size (letter spacing, tabular figures): step down until it fits
  for (let pass = 0; pass < 40 && open.length; pass += 1) {
    const over = open.filter(({ element, size }) => size > MIN && element.scrollWidth > element.clientWidth + 1);
    for (const item of over) { item.size -= 1; item.element.style.fontSize = `${item.size}px`; }
    open = over;
  }
}

const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(fit); } };

function mark(element: HTMLElement) {
  dirty.add(element);
  if (!watched.has(element)) { watched.add(element); resizer?.observe(element); }
  schedule();
}

function collect(node: Node) {
  if (!(node instanceof HTMLElement)) return;
  if (node.matches(SELECTOR)) mark(node);
  node.querySelectorAll<HTMLElement>(SELECTOR).forEach(mark);
}

export function fitTiles() {
  resizer = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const width = entry.contentRect.width;
      if (widths.get(entry.target) === width) continue;
      widths.set(entry.target, width);
      dirty.add(entry.target as HTMLElement);
    }
    if (dirty.size) schedule();
  });
  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === "characterData") record.target.parentElement?.closest<HTMLElement>(SELECTOR) && mark(record.target.parentElement.closest<HTMLElement>(SELECTOR)!);
      else {
        record.addedNodes.forEach(collect);
        if (record.target instanceof HTMLElement) { const tile = record.target.closest<HTMLElement>(SELECTOR); if (tile) mark(tile); }
      }
    }
  }).observe(document.body, { childList: true, subtree: true, characterData: true });
  document.querySelectorAll<HTMLElement>(SELECTOR).forEach(mark);
  void document.fonts?.ready.then(() => document.querySelectorAll<HTMLElement>(SELECTOR).forEach(mark));
}
