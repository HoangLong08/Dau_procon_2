// Vẽ bản đồ lục giác even-r (hàng chẵn lệch phải nửa ô): địa hình, giao thông, spot, lộ trình, xe.
import { positionsAt } from "./store.js";

const SQ3 = Math.sqrt(3);
const TERRAIN_VAR = ["--plain", "--road", "--mountain", "--pond"];

export function brandColor(brand) {
  return `hsl(${Math.round((brand * 137.508) % 360)} 62% 46%)`;
}

function layout(config, canvas) {
  const avail = Math.max(240, (canvas.parentElement?.clientWidth || 640) - 24);
  const size = Math.max(5, Math.min(22, avail / (SQ3 * (config.width + 0.5))));
  const w = SQ3 * size;
  return { size, w,
           width: Math.ceil(w * (config.width + 0.5)) + 4,
           height: Math.ceil(size * 1.5 * (config.height - 1) + 2 * size) + 4 };
}

function center(L, config, cell) {
  const row = Math.floor(cell / config.width);
  const col = cell % config.width;
  return [2 + L.w * (col + 0.5 + (row % 2 === 0 ? 0.5 : 0)), 2 + L.size + row * L.size * 1.5];
}

function hexPath(ctx, x, y, r) {
  ctx.beginPath();
  for (let k = 0; k < 6; k++) {
    const a = (Math.PI / 180) * (60 * k - 90);
    const px = x + r * Math.cos(a), py = y + r * Math.sin(a);
    if (k === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
  }
  ctx.closePath();
}

function drawRoutes(ctx, L, config, pos, color, dash, alpha) {
  if (!pos.length) return;
  ctx.save();
  ctx.strokeStyle = color;
  ctx.globalAlpha = alpha;
  ctx.lineWidth = Math.max(1.5, L.size * 0.18);
  ctx.lineJoin = "round";
  ctx.setLineDash(dash);
  for (let i = 0; i < pos[0].length; i++) {
    ctx.beginPath();
    let prev = -1;
    for (const step of pos) {
      const cell = step[i];
      if (cell === prev) continue;
      const [x, y] = center(L, config, cell);
      if (prev === -1) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      prev = cell;
    }
    ctx.stroke();
  }
  ctx.restore();
}

function collectedBy(result, step) {
  const got = new Set();
  for (const [t, , spot] of result?.trace?.collect || []) if (t <= step) got.add(spot);
  return got;
}

export function draw(canvas, m, view) {
  const ctx = canvas.getContext("2d");
  const cfg = m.config;
  if (!cfg) { canvas.width = 0; canvas.height = 0; return; }
  const L = layout(cfg, canvas);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(L.width * dpr);
  canvas.height = Math.round(L.height * dpr);
  canvas.style.width = `${L.width}px`;
  canvas.style.height = `${L.height}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const css = getComputedStyle(document.documentElement);
  const v = name => css.getPropertyValue(name).trim();
  const day = m.days[view.day];
  const status = day?.status || {};

  for (let c = 0; c < cfg.cells.length; c++) {
    const [x, y] = center(L, cfg, c);
    hexPath(ctx, x, y, L.size - 0.6);
    ctx.fillStyle = v(TERRAIN_VAR[cfg.cells[c]] || "--plain");
    ctx.fill();
    const s = status[c];
    if (s) {
      ctx.globalAlpha = 0.65;
      ctx.fillStyle = v(s === 2 ? "--jam" : "--busy");
      ctx.fill();
      ctx.globalAlpha = 1;
    }
  }

  const result = day?.results[view.bot];
  const got = collectedBy(result, view.step);
  cfg.spots.forEach((s, i) => {
    const [x, y] = center(L, cfg, s.pos);
    ctx.globalAlpha = got.has(i) ? 0.3 : 1;
    ctx.beginPath();
    ctx.arc(x, y, L.size * 0.6, 0, 2 * Math.PI);
    ctx.fillStyle = brandColor(s.brand);
    ctx.fill();
    if (L.size >= 9) {
      ctx.fillStyle = "#fff";
      ctx.font = `600 ${Math.round(L.size * 0.62)}px system-ui`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(s.brand), x, y + 0.5);
    }
    ctx.globalAlpha = 1;
  });

  if (view.bot && day && view.bot in m.botIndex) {
    const color = m.bots[m.botIndex[view.bot]].color;
    const think = day.think[view.bot];
    const plan = think && (think.better.at(-1) || think.safe);
    if (result?.trace?.pos?.length) drawRoutes(ctx, L, cfg, result.trace.pos.slice(0, view.step + 1), color, [], 0.55);
    else if (plan?.trace?.pos?.length) drawRoutes(ctx, L, cfg, plan.trace.pos, color, [5, 4], 0.9);
  }

  const teams = positionsAt(m, view.day, view.step);
  const n = teams.length;
  teams.forEach((team, ti) => {
    const ang = (ti / Math.max(1, n)) * 2 * Math.PI;
    const r = n > 1 ? L.size * 0.32 : 0;
    const s = Math.max(2.5, L.size * 0.34);
    team.agents.forEach((a, ai) => {
      const [cx, cy] = center(L, cfg, a.pos);
      const x = cx + r * Math.cos(ang), y = cy + r * Math.sin(ang);
      ctx.beginPath();
      if (a.kind === 1) ctx.rect(x - s, y - s, 2 * s, 2 * s); else ctx.arc(x, y, s, 0, 2 * Math.PI);
      ctx.fillStyle = team.color;
      ctx.fill();
      ctx.lineWidth = team.bot === view.bot ? 2 : 1;
      ctx.strokeStyle = team.bot === view.bot ? v("--ink") : "#ffffff";
      ctx.stroke();
      if (ai === team.errorAgent) {
        ctx.beginPath();
        ctx.arc(x, y, s + 4, 0, 2 * Math.PI);
        ctx.strokeStyle = v("--warn");
        ctx.lineWidth = 2.5;
        ctx.stroke();
      }
    });
  });
}
