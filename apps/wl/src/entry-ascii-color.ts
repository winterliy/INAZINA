// Colour ASCII for the procedural scenes. Three's AsciiEffect in colour mode writes one
// <span style="color:…"> per character into the DOM every frame, which ran the entry at ~8 fps.
// This draws the same grid straight onto a canvas instead: render the scene into a tiny target
// (one pixel per character), read it back once, and fillText each lit cell in its pixel's colour.
// It has the parts of AsciiEffect's interface the entry uses (domElement, setSize, render).

type Three = typeof import("three");

export function colorAscii(THREE: Three, renderer: import("three").WebGLRenderer, charSet: string, resolution: number) {
  const canvas = document.createElement("canvas");
  const ctx = canvas.getContext("2d")!;
  const target = new THREE.WebGLRenderTarget(1, 1);
  const chars = charSet.trimStart() || "@";  // a space never needs drawing
  // Colours rounded to 4 bits a channel, so style strings come from a fixed table.
  const styles = Array.from({ length: 4096 }, (_, k) => `rgb(${(k >> 8) * 17},${((k >> 4) & 15) * 17},${(k & 15) * 17})`);
  const clear = new THREE.Color();
  let cols = 1, rows = 1, cell = 1, pixels = new Uint8Array(4);

  function setSize(width: number, height: number) {
    const dpr = Math.min(2, devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    cols = Math.max(1, Math.round(width * resolution));
    rows = Math.max(1, Math.round(height * resolution));
    cell = canvas.width / cols;
    target.setSize(cols, rows);
    pixels = new Uint8Array(cols * rows * 4);
    ctx.font = `${Math.round(cell * 1.25)}px ui-monospace, Menlo, monospace`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
  }

  function render(scene: import("three").Scene, camera: import("three").Camera) {
    const alpha = renderer.getClearAlpha();
    renderer.getClearColor(clear);
    renderer.setClearColor(0x000000, 0);  // empty cells stay transparent and are skipped
    renderer.setRenderTarget(target);
    renderer.clear();
    renderer.render(scene, camera);
    renderer.readRenderTargetPixels(target, 0, 0, cols, rows, pixels);
    renderer.setRenderTarget(null);
    renderer.setClearColor(clear, alpha);

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const last = chars.length - 1;
    let style = -1;
    for (let y = 0; y < rows; y++) {
      const row = (rows - 1 - y) * cols * 4;  // WebGL rows run bottom-up
      const cy = (y + 0.5) * cell;
      for (let x = 0; x < cols; x++) {
        const i = row + x * 4;
        if (pixels[i + 3] < 16) continue;
        const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
        const level = Math.round(((r * 0.3 + g * 0.59 + b * 0.11) / 255) * last);
        const k = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4);
        if (k !== style) { ctx.fillStyle = styles[k]; style = k; }
        ctx.fillText(chars[level], (x + 0.5) * cell, cy);
      }
    }
  }

  return { domElement: canvas, setSize, render, dispose: () => target.dispose() };
}
