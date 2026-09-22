// Mô hình dẫn xuất từ dòng sự kiện. Thuần dữ liệu: không đụng DOM.
export const TEAM_COLORS = ["#2563eb", "#dc2626", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#65a30d"];
const OTHER_COLOR = "#9ca3af";

export function createModel() {
  return { config: null, mode: null, bots: [], botIndex: {}, days: {}, lastDay: -1,
           roles: {}, logs: [], ranking: null, serverScores: {}, runEnds: [], t0: null };
}

function ensureBot(m, name, info = {}) {
  if (!(name in m.botIndex)) {
    m.botIndex[name] = m.bots.length;
    m.bots.push({ name, type: info.type || "search", seconds: info.seconds ?? null,
                  color: TEAM_COLORS[m.bots.length % TEAM_COLORS.length], total: [0, 0, 0], rank: 1 });
  }
  return m.bots[m.botIndex[name]];
}

function dayOf(m, d) {
  if (!m.days[d]) m.days[d] = { steps: 0, status: {}, teams: {}, others: [], results: {}, think: {} };
  return m.days[d];
}

function thinkOf(m, d, bot) {
  const think = dayOf(m, d).think;
  if (!think[bot]) think[bot] = { safe: null, progress: [], better: [], submits: [], forecast: null };
  return think[bot];
}

function rolesOf(m, bot) {
  if (!m.roles[bot]) m.roles[bot] = { candidates: [], chosen: null };
  return m.roles[bot];
}

function note(m, ev, text, level = "info") {
  m.logs.push({ t: ev.t - m.t0, bot: ev.bot, text, level });
  if (m.logs.length > 600) m.logs.splice(0, m.logs.length - 600);
}

export function applyEvent(m, ev) {
  if (m.t0 === null) m.t0 = ev.t;
  const d = ev.data || {};
  const bot = ev.bot;
  switch (ev.kind) {
    case "match_start":
      if (d.config) m.config = d.config;
      m.mode = d.mode || m.mode;
      (d.bots || (bot ? [{ name: bot }] : [])).forEach(b => ensureBot(m, b.name, b));
      break;
    case "roles_candidate":
      ensureBot(m, bot);
      rolesOf(m, bot).candidates.push(d);
      break;
    case "roles_chosen":
      ensureBot(m, bot);
      rolesOf(m, bot).chosen = d.kinds;
      break;
    case "day_start": {
      const x = dayOf(m, d.day);
      x.steps = d.steps;
      x.status = d.status || {};
      if (d.teams) Object.assign(x.teams, d.teams);
      if (d.agents && bot) { ensureBot(m, bot); x.teams[bot] = d.agents; }
      if (d.others) x.others = d.others;
      m.lastDay = Math.max(m.lastDay, d.day);
      break;
    }
    case "forecast":
      thinkOf(m, d.day, bot).forecast = d.status;
      break;
    case "plan_safe":
      thinkOf(m, d.day, bot).safe = d;
      note(m, ev, `ngày ${d.day + 1}: bài an toàn ${d.types} loại / ${d.servings} phần, dựng trong ${d.ms} ms`);
      break;
    case "alns_progress":
      thinkOf(m, d.day, bot).progress.push(d);
      break;
    case "alns_better":
      thinkOf(m, d.day, bot).better.push(d);
      note(m, ev, `ngày ${d.day + 1}: ALNS tìm được bài tốt hơn lúc ${d.elapsed.toFixed(2)} s: ${d.types} loại / ${d.servings} phần`);
      break;
    case "submit":
      thinkOf(m, d.day, bot).submits.push(d);
      note(m, ev, `ngày ${d.day + 1}: gửi bài #${d.n} → ${d.ok ? "được nhận" : `bị từ chối (${d.reason})`}`,
           d.ok ? "info" : "warn");
      break;
    case "day_result": {
      const b = ensureBot(m, bot);
      dayOf(m, d.day).results[bot] = d;
      if (d.total) b.total = d.total;
      note(m, ev, `ngày ${d.day + 1}: ${d.types} loại, ${d.servings} phần` +
                  (d.ok ? "" : ` — BÀI LỖI ${d.error?.code || ""}`), d.ok ? "info" : "warn");
      break;
    }
    case "log":
      note(m, ev, d.text, d.level);
      break;
    case "match_end":
      if (d.ranking) m.ranking = d.ranking;
      if (bot && (d.score || d.replay_check)) m.serverScores[bot] = { score: d.score, check: d.replay_check };
      note(m, ev, "hết trận");
      break;
    case "run_end":
      m.runEnds.push({ bot, ...d });
      note(m, ev, `tiến trình ${bot} thoát, mã ${d.exit_code}`, d.exit_code === 0 ? "info" : "warn");
      break;
  }
}

export function standings(m) {
  const cmp = (a, b) => b.total[0] - a.total[0] || b.total[1] - a.total[1] || b.total[2] - a.total[2];
  const rows = [...m.bots].sort(cmp);
  rows.forEach((r, i) => { r.rank = i > 0 && cmp(rows[i - 1], r) === 0 ? rows[i - 1].rank : i + 1; });
  return rows;
}

// Vị trí mọi xe ở (ngày, bước): theo vết của simulator nếu ngày đã xong, không thì vị trí đầu ngày.
export function positionsAt(m, d, step) {
  const day = m.days[d];
  if (!day) return [];
  const ours = m.bots.map(b => {
    const res = day.results[b.name];
    const start = day.teams[b.name] || [];
    let agents = start.map(a => ({ ...a }));
    if (res?.trace?.pos?.length) {
      const i = Math.min(step, res.trace.pos.length - 1);
      const kinds = m.roles[b.name]?.chosen || [];
      agents = res.trace.pos[i].map((pos, k) => ({ pos, fuel: res.trace.fuel[i][k], kind: start[k]?.kind ?? kinds[k] ?? 0 }));
    }
    return { bot: b.name, color: b.color, agents, errorAgent: res?.error ? res.error.agent : -1 };
  });
  const others = (day.others || []).map((team, j) => ({ bot: `đội khác ${j + 1}`, color: OTHER_COLOR, agents: team, errorAgent: -1 }));
  return ours.concat(others);
}

// Dự báo giao thông (bot làm ở ngày trước) so với thực tế đầu ngày d, chỉ trên ô đông/tắc.
export function forecastAccuracy(m, d, bot) {
  const fc = m.days[d]?.think[bot]?.forecast;
  const actual = m.days[d]?.status;
  if (!fc || !actual) return null;
  const cells = new Set([...Object.keys(fc), ...Object.keys(actual)]);
  let hit = 0;
  cells.forEach(c => { if ((fc[c] || 0) === (actual[c] || 0)) hit++; });
  return { hit, total: cells.size };
}
