/**
 * Aircraft icons, drawn once onto canvases pointing north. They are white with
 * grey shading, a dark outline and a soft shadow; each billboard tints them with
 * its altitude colour (the tint multiplies, so the shading carries through).
 */

const SIZE = 128; // drawn on a 64-unit grid at 2x for crisp edges

type Shape = { outline: string; details?: (ctx: CanvasRenderingContext2D) => void };

function render(shape: Shape): string {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = SIZE;
  const ctx = canvas.getContext("2d")!;
  ctx.scale(SIZE / 64, SIZE / 64);
  const body = new Path2D(shape.outline);

  // Soft drop shadow, as if the aircraft were above the map.
  ctx.save();
  ctx.translate(1.2, 2);
  ctx.filter = "blur(1.6px)";
  ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
  ctx.fill(body);
  ctx.restore();

  ctx.lineJoin = "round";
  ctx.strokeStyle = "rgba(8, 10, 16, 0.9)";
  ctx.lineWidth = 2.2;
  ctx.stroke(body);

  // Light from the left: bright upper surfaces, slightly darker on the right.
  const shade = ctx.createLinearGradient(8, 0, 56, 0);
  shade.addColorStop(0, "#ffffff");
  shade.addColorStop(0.55, "#f3f5f8");
  shade.addColorStop(1, "#d5dae2");
  ctx.fillStyle = shade;
  ctx.fill(body);

  shape.details?.(ctx);
  return canvas.toDataURL();
}

// Swept-wing twin-engine airliner.
const AIRLINER: Shape = {
  outline:
    "M32 2.5 C34.1 2.5 35.2 5.5 35.2 9.5 L35.2 22.8 L56.5 34.2 C58.6 35.3 59.5 36.3 59.5 37.6 L59.5 39 " +
    "L35.2 32.6 L34.6 47.4 L43 53 C43.8 53.5 44 54.1 44 55 L44 56.4 L33 54 L32 57.6 L31 54 L20 56.4 " +
    "L20 55 C20 54.1 20.2 53.5 21 53 L29.4 47.4 L28.8 32.6 L4.5 39 L4.5 37.6 C4.5 36.3 5.4 35.3 7.5 34.2 " +
    "L28.8 22.8 L28.8 9.5 C28.8 5.5 29.9 2.5 32 2.5 Z",
  details(ctx) {
    // Engines hanging under the wings.
    ctx.fillStyle = "#aeb5c0";
    ctx.strokeStyle = "rgba(8, 10, 16, 0.8)";
    ctx.lineWidth = 1;
    for (const x of [21.2, 42.8]) {
      ctx.beginPath();
      ctx.roundRect(x - 1.7, 25.6, 3.4, 7.2, 1.6);
      ctx.fill();
      ctx.stroke();
    }
    // Cockpit windows.
    ctx.fillStyle = "rgba(20, 28, 40, 0.75)";
    ctx.beginPath();
    ctx.ellipse(32, 7.4, 1.9, 1.2, 0, 0, Math.PI * 2);
    ctx.fill();
  },
};

// High-wing light aircraft (Cessna-style).
const LIGHT: Shape = {
  outline:
    "M32 7 C33.8 7 34.6 9 34.6 11.5 L34.6 19.5 L57 20.5 C58.2 20.6 58.8 21.3 58.8 22.4 L58.8 24.6 " +
    "C58.8 25.6 58.2 26.2 57 26.2 L34.4 26.6 L33.4 45 L41.5 46.2 C42.3 46.3 42.8 46.9 42.8 47.7 " +
    "L42.8 49.6 L32 49 L21.2 49.6 L21.2 47.7 C21.2 46.9 21.7 46.3 22.5 46.2 L30.6 45 L29.6 26.6 " +
    "L7 26.2 C5.8 26.2 5.2 25.6 5.2 24.6 L5.2 22.4 C5.2 21.3 5.8 20.6 7 20.5 L29.4 19.5 L29.4 11.5 " +
    "C29.4 9 30.2 7 32 7 Z",
  details(ctx) {
    ctx.strokeStyle = "rgba(8, 10, 16, 0.85)";
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.moveTo(26.5, 6.2);
    ctx.lineTo(37.5, 6.2); // propeller
    ctx.stroke();
  },
};

// Helicopter: cabin, tail boom and a translucent rotor disc with two blades.
const HELICOPTER: Shape = {
  outline:
    "M32 16 C36.6 16 39.4 20 39.4 25.2 C39.4 29.8 37.4 33 34.6 34.4 L33.6 51 L39 52.6 L39 55.6 " +
    "L25 55.6 L25 52.6 L30.4 51 L29.4 34.4 C26.6 33 24.6 29.8 24.6 25.2 C24.6 20 27.4 16 32 16 Z",
  details(ctx) {
    ctx.fillStyle = "rgba(255, 255, 255, 0.16)";
    ctx.strokeStyle = "rgba(255, 255, 255, 0.55)";
    ctx.lineWidth = 0.9;
    ctx.beginPath();
    ctx.arc(32, 25, 21, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(14, 13);
    ctx.lineTo(50, 37);
    ctx.moveTo(50, 13);
    ctx.lineTo(14, 37);
    ctx.stroke();
  },
};

export const ICONS = {
  airliner: render(AIRLINER),
  light: render(LIGHT),
  helicopter: render(HELICOPTER),
};

/** ADS-B emitter category → icon (A1 light, A2 small, A7 rotorcraft, B1 glider…). */
export function iconFor(category: string | null | undefined, typeCode: string): string {
  if (category === "A7") return ICONS.helicopter;
  if (category === "A1" || category === "B1" || category === "B4") return ICONS.light;
  if (!category && /^(C1[5-8]\d|C2[0-1]\d|P28|PA\d|SR2|DA[24]|R[24]4)/.test(typeCode)) {
    return /^R[24]4/.test(typeCode) ? ICONS.helicopter : ICONS.light;
  }
  return ICONS.airliner;
}

/** Relative icon size by ADS-B category: heavy jets bigger, light aircraft smaller. */
export function sizeFor(category: string | null | undefined): number {
  switch (category) {
    case "A5": return 1.22; // heavy (747, 777, A350…)
    case "A4": return 1.08; // high-vortex large (757)
    case "A3": return 1.0; // large (A320, 737…)
    case "A2": return 0.86; // small (regional, business jets)
    case "A1": return 0.74; // light
    case "A7": return 0.86; // rotorcraft
    default: return 0.95;
  }
}
