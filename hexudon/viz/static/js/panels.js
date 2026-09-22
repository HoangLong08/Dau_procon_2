// Bảng điểm, khung suy luận của một bot trong một ngày, nhật ký.
import { forecastAccuracy, standings } from "./store.js";

export const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fmt = x => Math.round(x).toLocaleString("vi-VN");
const fmtT = t => {
  const s = Math.max(0, t);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;
};

function checkText(c) {
  if (c.error) return `đối chiếu replay lỗi: ${c.error}`;
  if (c.ok) return `đối chiếu ${c.days_checked} ngày với replay của server: khớp hoàn toàn`;
  return `LỆCH LUẬT ở ngày ${(c.days_with_mismatch || []).map(d => d + 1).join(", ")}`;
}

export function renderScoreboard(el, m) {
  const rows = standings(m);
  if (!rows.length) { el.innerHTML = '<tr><td class="muted">Chưa có trận.</td></tr>'; return; }
  let html = "<tr><th></th><th>Bot</th><th>L</th><th>D</th><th>Q</th><th>Hạng</th></tr>";
  for (const r of rows) {
    const srv = m.serverScores[r.name];
    const s = srv?.score;
    const [L, D, Q] = s ? [s.L, s.D, s.Q] : r.total;
    const rank = s ? `${s.rank ?? "?"}/${s.teams} trên server` : String(r.rank);
    html += `<tr><td><span class="dot" style="background:${r.color}"></span></td><td>${esc(r.name)}</td>` +
            `<td>${esc(L)}</td><td>${esc(D)}</td><td>${esc(Q)}</td><td>${esc(rank)}</td></tr>`;
    if (srv?.check) html += `<tr><td></td><td colspan="5" class="muted">${esc(checkText(srv.check))}</td></tr>`;
  }
  el.innerHTML = html;
}

function rolesBlock(roles) {
  const chosen = JSON.stringify(roles.chosen);
  const phaseText = c => ({ rollout: "chơi thử tham lam", refine: "tinh chỉnh bằng ALNS",
                            skipped: `bỏ (chỉ chơi được ${c.days} ngày)` })[c.phase] || c.phase;
  const rows = roles.candidates.map(c => {
    const pick = chosen === JSON.stringify(c.kinds) && c.phase !== "skipped";
    return `<tr class="${pick ? "chosen" : ""}"><td>${pick ? "→" : ""}</td><td>${esc(c.label)}</td>` +
           `<td>${c.score[0]}</td><td>${c.score[1]}</td><td>${c.score[2]}</td><td class="muted">${esc(phaseText(c))}</td></tr>`;
  }).join("");
  const summary = roles.chosen
    ? `Chọn: ${roles.chosen.filter(k => k === 1).length} xe tiếp nhiên liệu / ${roles.chosen.length} xe`
    : "Đang chọn…";
  const table = rows
    ? `<table class="roles"><tr><th></th><th>Phương án</th><th>L</th><th>D</th><th>Q</th><th></th></tr>${rows}</table>`
    : "";
  return `<h3>Vai trò</h3><p>${esc(summary)}</p>${table}`;
}

function weights(title, w) {
  const entries = Object.entries(w || {});
  if (!entries.length) return "";
  const max = Math.max(1e-9, ...entries.map(e => e[1]));
  return `<div class="ops"><b>${title}</b>` + entries.map(([k, val]) =>
    `<div class="op"><span>${esc(k)}</span><i style="width:${Math.round((val / max) * 100)}%"></i><em>${val.toFixed(2)}</em></div>`).join("") + "</div>";
}

function alnsBlock(t) {
  const last = t.progress.at(-1);
  const best = t.better.at(-1) || t.safe;
  const lines = [];
  if (t.safe) lines.push(`Bài an toàn: ${t.safe.types} loại / ${t.safe.servings} phần, dựng trong ${t.safe.ms} ms`);
  if (last) lines.push(`ALNS: ${last.iteration} vòng trong ${last.elapsed.toFixed(2)} s, ${t.better.length} lần tìm được bài tốt hơn`);
  if (best) lines.push(`Bài tốt nhất: ${best.types} loại / ${best.servings} phần (điểm mục tiêu ${fmt(best.score)})`);
  const ops = last ? weights("Toán tử phá", last.destroy) + weights("Toán tử sửa", last.repair) : "";
  return `<h3>Tìm kiếm</h3><p>${lines.map(esc).join("<br>")}</p><canvas class="alns"></canvas>` +
         `<p class="muted">xanh: điểm tốt nhất · xám: điểm hiện tại · chấm: lần cải thiện · nét đứt: bài an toàn</p>${ops}`;
}

function drawAlns(canvas, t) {
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 320, H = canvas.clientHeight || 110, P = 6;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const pts = t.progress;
  const ys = [...pts.flatMap(p => [p.best, p.current]), ...t.better.map(b => b.score)];
  if (t.safe) ys.push(t.safe.score);
  if (!ys.length) return;
  const xmax = Math.max(0.001, ...pts.map(p => p.elapsed), ...t.better.map(b => b.elapsed));
  const ymin = Math.min(...ys), span = Math.max(...ys) - ymin || 1;
  const X = x => P + (x / xmax) * (W - 2 * P);
  const Y = y => H - P - ((y - ymin) / span) * (H - 2 * P);
  const css = getComputedStyle(document.documentElement);
  const line = (key, color) => {
    if (!pts.length) return;
    ctx.setLineDash([]);
    ctx.strokeStyle = color;
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(X(p.elapsed), Y(p[key])) : ctx.moveTo(X(p.elapsed), Y(p[key]))));
    ctx.stroke();
  };
  if (t.safe) {
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = css.getPropertyValue("--muted");
    ctx.beginPath();
    ctx.moveTo(P, Y(t.safe.score));
    ctx.lineTo(W - P, Y(t.safe.score));
    ctx.stroke();
  }
  line("current", css.getPropertyValue("--muted"));
  line("best", css.getPropertyValue("--accent"));
  ctx.fillStyle = css.getPropertyValue("--ok");
  t.better.forEach(b => { ctx.beginPath(); ctx.arc(X(b.elapsed), Y(b.score), 3, 0, 2 * Math.PI); ctx.fill(); });
}

function submitsBlock(t) {
  if (!t.submits.length) return "";
  return "<h3>Gửi bài</h3><ol class='submits'>" + t.submits.map(s =>
    `<li class="${s.ok ? "ok" : "bad"}">#${s.n} lúc ${s.elapsed.toFixed(2)} s — ${s.ok ? "được nhận" : `bị từ chối: ${esc(s.reason)}`}</li>`
  ).join("") + "</ol>";
}

function resultBlock(r) {
  const err = r.error
    ? `<br><b class="bad">Bài lỗi ${esc(r.error.code)}: xe ${r.error.agent}, bước ${r.error.step} — ${esc(r.error.detail || "")}</b>`
    : "";
  return `<h3>Kết quả ngày</h3><p>${r.types} loại, ${r.servings} phần; loại thu được: ${r.brands.join(", ") || "—"}${err}</p>`;
}

export function renderReasoning(el, m, view) {
  const bot = view.bot;
  if (!bot) { el.innerHTML = '<p class="muted">Chưa có bot.</p>'; return; }
  const parts = [];
  if (m.roles[bot]) parts.push(rolesBlock(m.roles[bot]));
  const day = m.days[view.day];
  const t = day?.think[bot];
  const acc = forecastAccuracy(m, view.day, bot);
  if (acc) {
    parts.push(`<h3>Dự báo giao thông</h3><p>${acc.total
      ? `Ngày ${view.day + 1}: đúng ${acc.hit}/${acc.total} ô đường đông hoặc tắc`
      : `Ngày ${view.day + 1}: dự báo và thực tế đều thông thoáng`}</p>`);
  }
  if (t) parts.push(alnsBlock(t), submitsBlock(t));
  const res = day?.results[bot];
  if (res) parts.push(resultBlock(res));
  if (!t && !res && !m.roles[bot]) {
    parts.push('<p class="muted">Bot này không có suy luận để xem (bot không tìm kiếm).</p>');
  }
  el.innerHTML = parts.join("");
  const canvas = el.querySelector("canvas.alns");
  if (canvas && t) drawAlns(canvas, t);
}

export function renderLog(el, m, colorOf) {
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
  el.innerHTML = m.logs.slice(-300).map(l =>
    `<li class="${l.level === "warn" ? "warn" : ""}"><time>${fmtT(l.t)}</time> ` +
    `<b style="color:${colorOf(l.bot)}">${esc(l.bot || "trận")}</b> ${esc(l.text)}</li>`).join("");
  if (atBottom) el.scrollTop = el.scrollHeight;
}
