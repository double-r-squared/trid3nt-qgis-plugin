// GRACE-2 web - landing hero contour field.
//
// A self-contained, dependency-free animated topographic-contour backdrop for
// the marketing landing hero. It draws nested iso-contour rings from a small
// sum-of-radial-sources scalar field onto a <canvas>, then gently advects the
// field over time and warps it toward the pointer so the contours "breathe"
// and bend around the cursor (a subtle terrain/parallax ripple - fitting for a
// flood / hazard modeling product).
//
// Design goals:
//   - performant: a coarse marching-squares grid (cell ~ 22px), one rAF loop,
//     pointer state read on each frame (no per-move React renders), DPR-capped.
//   - tasteful: thin, low-opacity strokes in a single cool accent; the field
//     resolves to smooth concentric "hills" rather than noise.
//   - accessible: honours prefers-reduced-motion -> renders ONE static frame
//     (still elegant, just not animated) and never starts the rAF loop.
//
// It is purely decorative (aria-hidden via the parent) and owns no app state.

import { useEffect, useRef } from "react";

interface Source {
  // Field-space position in [0,1] (resolution-independent).
  x: number;
  y: number;
  // Drift velocity (units of field-space per second).
  vx: number;
  vy: number;
  // Relative amplitude / falloff of this radial bump.
  amp: number;
  falloff: number;
}

interface Pt {
  x: number;
  y: number;
}

// A fixed, hand-tuned set of moving "peaks" - deterministic so the look is
// stable across reloads. Velocities are slow; the field is a slow tide.
const SOURCE_SEED: readonly Source[] = [
  { x: 0.18, y: 0.28, vx: 0.012, vy: 0.008, amp: 1.0, falloff: 2.6 },
  { x: 0.74, y: 0.2, vx: -0.01, vy: 0.011, amp: 0.85, falloff: 3.0 },
  { x: 0.52, y: 0.62, vx: 0.009, vy: -0.013, amp: 0.95, falloff: 2.2 },
  { x: 0.9, y: 0.74, vx: -0.014, vy: -0.007, amp: 0.7, falloff: 3.4 },
  { x: 0.08, y: 0.82, vx: 0.011, vy: -0.009, amp: 0.6, falloff: 3.2 },
];

// Iso-levels to trace. More levels near mid-field gives the dense, map-like
// contour banding without overdrawing.
const LEVELS: readonly number[] = [
  0.16, 0.26, 0.36, 0.46, 0.56, 0.66, 0.76, 0.86, 0.96,
];

export interface ContourFieldProps {
  /** Accent stroke colour (rgb triplet string, e.g. "120, 190, 220"). */
  rgb?: string;
  /** Optional className passed through to the canvas. */
  className?: string;
}

export function ContourField({
  rgb = "120, 196, 224",
  className,
}: ContourFieldProps): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const cv0 = canvasRef.current;
    if (!cv0) return;
    const cx0 = cv0.getContext("2d");
    if (!cx0) return;
    // Non-null locals so nested closures keep the narrowing.
    const cv: HTMLCanvasElement = cv0;
    const cx: CanvasRenderingContext2D = cx0;

    const reduce =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // Live, mutable copy of the seed sources (we mutate positions in place).
    const sources: Source[] = SOURCE_SEED.map((s) => ({ ...s }));

    // Pointer state, in field-space [0,1]; updated on move WITHOUT re-render.
    // Centred + "absent" until the user moves so the static frame is neutral.
    const pointer = { x: 0.5, y: 0.5, active: false };

    let width = 1;
    let height = 1;
    let cols = 1;
    let rows = 1;
    let cell = 22; // px between grid samples (CSS px)
    let dpr = 1;

    function resize(): void {
      const parent = cv.parentElement;
      const w = parent ? parent.clientWidth : window.innerWidth;
      const h = parent ? parent.clientHeight : window.innerHeight;
      width = Math.max(1, w);
      height = Math.max(1, h);
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      cell = width < 640 ? 26 : 22;
      cols = Math.ceil(width / cell) + 1;
      rows = Math.ceil(height / cell) + 1;
      cv.width = Math.round(width * dpr);
      cv.height = Math.round(height * dpr);
      cv.style.width = width + "px";
      cv.style.height = height + "px";
      cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    // Scalar field value at field-space (fx, fy). Sum of gaussian-ish radial
    // bumps, plus a pointer well that locally lifts the field so contours
    // crowd toward the cursor.
    function sample(fx: number, fy: number): number {
      let v = 0;
      for (let i = 0; i < sources.length; i++) {
        const s = sources[i];
        if (!s) continue;
        const dx = fx - s.x;
        const dy = fy - s.y;
        const d2 = dx * dx + dy * dy;
        v += s.amp * Math.exp(-d2 * s.falloff * 6);
      }
      if (pointer.active) {
        const dx = fx - pointer.x;
        const dy = fy - pointer.y;
        const d2 = dx * dx + dy * dy;
        // A soft positive lobe around the pointer; gentle so it bends, not spikes.
        v += 0.55 * Math.exp(-d2 * 26);
      }
      return v;
    }

    // Linear interp of the crossing point of `level` between two grid corners.
    function frac(a: number, b: number, level: number): number {
      const d = b - a;
      if (Math.abs(d) < 1e-6) return 0.5;
      return (level - a) / d;
    }

    function draw(): void {
      cx.clearRect(0, 0, width, height);

      // Precompute the field grid once per frame.
      const grid = new Float32Array(cols * rows);
      for (let r = 0; r < rows; r++) {
        const fy = (r * cell) / height;
        for (let c = 0; c < cols; c++) {
          const fx = (c * cell) / width;
          grid[r * cols + c] = sample(fx, fy);
        }
      }
      const at = (r: number, c: number): number => grid[r * cols + c] ?? 0;

      const seg = (a: Pt, b: Pt): void => {
        cx.moveTo(a.x, a.y);
        cx.lineTo(b.x, b.y);
      };

      for (let li = 0; li < LEVELS.length; li++) {
        const level = LEVELS[li] ?? 0.5;
        // Slightly brighter mid-levels for depth; outer rings fade.
        const dist = Math.abs(li - (LEVELS.length - 1) / 2);
        const alpha = 0.16 + 0.1 * (1 - dist / (LEVELS.length / 2));
        cx.lineWidth = li % 4 === 0 ? 1.4 : 0.8; // accent index lines
        cx.strokeStyle = "rgba(" + rgb + ", " + alpha.toFixed(3) + ")";
        cx.beginPath();

        for (let r = 0; r < rows - 1; r++) {
          for (let c = 0; c < cols - 1; c++) {
            const tl = at(r, c);
            const tr = at(r, c + 1);
            const br = at(r + 1, c + 1);
            const bl = at(r + 1, c);

            let idx = 0;
            if (tl > level) idx |= 8;
            if (tr > level) idx |= 4;
            if (br > level) idx |= 2;
            if (bl > level) idx |= 1;
            if (idx === 0 || idx === 15) continue;

            const x0 = c * cell;
            const y0 = r * cell;
            // Edge crossing points (top, right, bottom, left).
            const top: Pt = { x: x0 + cell * frac(tl, tr, level), y: y0 };
            const right: Pt = {
              x: x0 + cell,
              y: y0 + cell * frac(tr, br, level),
            };
            const bottom: Pt = {
              x: x0 + cell * frac(bl, br, level),
              y: y0 + cell,
            };
            const left: Pt = { x: x0, y: y0 + cell * frac(tl, bl, level) };

            switch (idx) {
              case 1:
              case 14:
                seg(left, bottom);
                break;
              case 2:
              case 13:
                seg(bottom, right);
                break;
              case 3:
              case 12:
                seg(left, right);
                break;
              case 4:
              case 11:
                seg(top, right);
                break;
              case 5:
                seg(left, top);
                seg(bottom, right);
                break;
              case 6:
              case 9:
                seg(top, bottom);
                break;
              case 7:
              case 8:
                seg(left, top);
                break;
              case 10:
                seg(left, bottom);
                seg(top, right);
                break;
              default:
                break;
            }
          }
        }
        cx.stroke();
      }
    }

    // Advance source positions by dt seconds, bouncing softly off [0,1] bounds.
    function step(dt: number): void {
      for (let i = 0; i < sources.length; i++) {
        const s = sources[i];
        if (!s) continue;
        let nx = s.x + s.vx * dt;
        let ny = s.y + s.vy * dt;
        if (nx < 0.04 || nx > 0.96) s.vx *= -1;
        if (ny < 0.04 || ny > 0.96) s.vy *= -1;
        nx = Math.min(0.96, Math.max(0.04, nx));
        ny = Math.min(0.96, Math.max(0.04, ny));
        s.x = nx;
        s.y = ny;
      }
    }

    let raf = 0;
    let last = 0;

    function frame(now: number): void {
      const dt = last ? Math.min((now - last) / 1000, 0.05) : 0;
      last = now;
      step(dt);
      draw();
      raf = requestAnimationFrame(frame);
    }

    function onPointerMove(e: PointerEvent): void {
      const rect = cv.getBoundingClientRect();
      pointer.x = (e.clientX - rect.left) / Math.max(1, rect.width);
      pointer.y = (e.clientY - rect.top) / Math.max(1, rect.height);
      pointer.active = true;
    }
    function onPointerLeave(): void {
      pointer.active = false;
    }

    resize();
    const onResize = (): void => {
      resize();
      if (reduce) draw();
    };
    window.addEventListener("resize", onResize);

    if (reduce) {
      // One elegant static frame; no animation, no pointer interactivity.
      draw();
    } else {
      window.addEventListener("pointermove", onPointerMove, { passive: true });
      window.addEventListener("pointerleave", onPointerLeave);
      raf = requestAnimationFrame(frame);
    }

    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerleave", onPointerLeave);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [rgb]);

  return (
    <canvas
      ref={canvasRef}
      className={className}
      data-testid="grace2-landing-contours"
    />
  );
}
