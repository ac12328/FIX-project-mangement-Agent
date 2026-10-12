/** Rasterize the clipboard SVG, fed by the same frozen, ordered items as the UI. */
export async function reportImagePng(svg: SVGSVGElement): Promise<Blob> {
  const clone = svg.cloneNode(true) as SVGSVGElement;
  const sourceNodes = [svg, ...Array.from(svg.querySelectorAll("*"))];
  const clonedNodes = [clone, ...Array.from(clone.querySelectorAll("*"))];
  sourceNodes.forEach((node, index) => {
    const computed = getComputedStyle(node);
    for (const attribute of ["fill", "stroke"] as const) {
      if (node.hasAttribute(attribute)) clonedNodes[index].setAttribute(attribute, computed.getPropertyValue(attribute));
    }
  });
  clone.removeAttribute("class");
  const { width, height } = svg.viewBox.baseVal;
  const scale = Math.min(2, 16000 / height, Math.sqrt(32_000_000 / (width * height)));
  if (scale < 1) throw new Error("This chart is too large to copy clearly. Try the My projects scope, or copy the complete Markdown report.");
  const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(clone)], { type: "image/svg+xml;charset=utf-8" }));
  try {
    const image = new Image();
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("Could not render the chart image. Please retry."));
      image.src = url;
    });
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Image copying is not available in this browser.");
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve, reject) => canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("Could not create PNG image.")), "image/png"));
  } finally { URL.revokeObjectURL(url); }
}

/** Called directly in the click handler, before awaiting image generation. */
export async function writeReportImageClipboard(png: Promise<Blob>): Promise<void> {
  if (!window.isSecureContext || !navigator.clipboard?.write || typeof ClipboardItem === "undefined") {
    throw new Error("Automatic image copy is unavailable. Right-click the preview and choose Copy image.");
  }
  await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
}
