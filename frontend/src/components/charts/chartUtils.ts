/* Pure color helpers shared across the canvas charts. Kept out of component
   files so React Fast Refresh treats those as component-only modules. */

/** Apply alpha to a hex or rgb color, returning an rgba() string. */
export function hexA(color: string, alpha: number): string {
  if (color.startsWith("#")) {
    let hex = color.slice(1);
    if (hex.length === 3) hex = hex.split("").map((c) => c + c).join("");
    const r = parseInt(hex.slice(0, 2), 16);
    const g = parseInt(hex.slice(2, 4), 16);
    const b = parseInt(hex.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }
  if (color.startsWith("rgb")) {
    return color.replace(/rgba?\(([^)]+)\)/, (_, inner) => {
      const parts = String(inner).split(",").slice(0, 3).map((p) => p.trim());
      return `rgba(${parts.join(", ")}, ${alpha})`;
    });
  }
  return color;
}
