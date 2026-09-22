// Điều khiển trang: form, gọi API, poll sự kiện, dòng thời gian, phát lại.
import { applyEvent, createModel } from "./store.js";
import { draw } from "./hexmap.js";
import { esc, renderLog, renderReasoning, renderScoreboard } from "./panels.js";

const $ = id => document.getElementById(id);
const state = {
  options: null, runId: null, since: 0, running: false, timer: null, polling: false, pollAgain: false,
  model: createModel(), view: { day: 0, step: 0, bot: null }, live: true, playing: null, botKey: "",
};

async function api(path, body) {
  const init = body === undefined ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, init);
  let data = {};
  try { data = await res.json(); } catch { /* thân rỗng */ }
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function banner(text, level = "warn") {
  const b = $("banner");
  b.hidden = !text;
  b.textContent = text || "";
  b.className = `banner ${level}`;
}

function addBotRow(type = "search", seconds = 1) {
  const row = document.createElement("div");
  row.className = "bot-row";
  row.innerHTML = `<select class="bot-type" aria-label="loại bot">${state.options.bot_types
      .map(t => `<option ${t === type ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>` +
    `<input class="bot-seconds" type="number" min="0.1" step="0.1" value="${seconds}" title="giây tìm kiếm mỗi ngày">` +
    `<button type="button" class="bot-del" title="bỏ bot" aria-label="bỏ bot">×</button>`;
  const sync = () => { row.querySelector(".bot-seconds").hidden = row.querySelector(".bot-type").value !== "search"; };
  row.querySelector(".bot-type").addEventListener("change", sync);
  row.querySelector(".bot-del").addEventListener("click", () => row.remove());
  sync();
  $("bot-list").appendChild(row);
}

function updateMode() {
  const mode = $("mode").value;
  const online = mode !== "offline";
  $("offline-form").hidden = online;
  $("online-form").hidden = !online;
  $("fake-box").hidden = mode !== "fake";
  $("questions").hidden = true;
  $("questions").innerHTML = "";
  if (online) {
    const vars = state.options.tokens[mode] || [];
    $("list-token").innerHTML = vars.length
      ? vars.map(v => `<option>${esc(v)}</option>`).join("")
      : "<option value=''>(không có TEST_* trong file token)</option>";
    const f = state.options.fake;
    $("fake-status").textContent = f.reachable ? `server giả đang chạy ở ${f.url}` : "chưa có server giả";
  }
}

async function loadQuestions() {
  const mode = $("mode").value;
  try {
    const data = await api(`/api/online/questions?target=${mode}&token=${encodeURIComponent($("list-token").value)}`);
    const vars = state.options.tokens[mode] || [];
    const rows = data.rows.map(r => `<tr class="${r.allowed ? "" : "blocked"}">` +
      `<td><input type="radio" name="game" value="${esc(r.game_id)}" ${r.allowed ? "" : "disabled"} aria-label="chọn câu"></td>` +
      `<td>${esc(r.match)}</td><td>${esc(r.question)}</td><td>${esc(r.mode)}</td><td>${esc(r.status)}</td>` +
      `<td class="muted">${esc(r.why)}</td></tr>`).join("");
    $("questions").innerHTML =
      `<p>Đội ${esc(data.team)}${data.warnings.length ? " — " + esc(data.warnings.join("; ")) : ""}</p>` +
      `<table><tr><th></th><th>Trận</th><th>Câu</th><th>Loại</th><th>Trạng thái</th><th></th></tr>${rows}</table>` +
      `<p>Token chơi: ${vars.map(v => `<label><input type="checkbox" class="tok" value="${esc(v)}" ` +
        `${v === $("list-token").value ? "checked" : ""}> ${esc(v)}</label>`).join(" ")} ` +
      `<label><input type="checkbox" id="keep"> giữ practice để xem trên website (--keep)</label></p>`;
    $("questions").hidden = false;
    banner("");
  } catch (e) { banner(e.message); }
}

async function run() {
  const mode = $("mode").value;
  let body;
  if (mode === "offline") {
    const bots = [...document.querySelectorAll(".bot-row")].map(r => ({
      type: r.querySelector(".bot-type").value,
      seconds: Number(r.querySelector(".bot-seconds").value) || 1,
    }));
    body = { mode, map: $("map").value, days: Number($("days").value) || 0, seed: Number($("seed").value) || 0, bots };
  } else {
    const game = document.querySelector("input[name=game]:checked");
    if (!game) { banner("Tải danh sách câu rồi chọn một câu trước."); return; }
    const tokens = [...document.querySelectorAll(".tok:checked")].map(x => x.value);
    body = { mode, game_id: game.value, tokens, keep: $("keep")?.checked || false };
  }
  try {
    const res = await api("/api/runs", body);
    banner(res.warnings?.join("; ") || "", "warn");
    await refreshRuns(res.id);
    await openRun(res.id);
  } catch (e) { banner(e.message); }
}

async function openRun(id) {
  clearTimeout(state.timer);
  stopPlayback();
  Object.assign(state, { runId: id, since: 0, running: false, model: createModel(), live: true, botKey: "" });
  state.view = { day: 0, step: 0, bot: null };
  $("runs").value = id;
  await poll();
}

async function poll() {
  if (state.polling) { state.pollAgain = true; return; }
  state.polling = true;
  const id = state.runId;
  try {
    let more = Boolean(id);
    while (more) {
      const data = await api(`/api/runs/${encodeURIComponent(id)}/events?since=${state.since}`);
      if (id !== state.runId) break;
      data.events.forEach(ev => applyEvent(state.model, ev));
      state.since = data.next;
      state.running = data.running;
      more = data.more;
      if (data.warnings?.length) banner(data.warnings.join("; "));
    }
    if (id && id === state.runId) render();
  } catch (e) {
    banner(`mất kết nối tới web server: ${e.message}`);
  } finally {
    state.polling = false;
  }
  clearTimeout(state.timer);
  if (state.pollAgain || id !== state.runId) {
    // có lượt hỏi bị gác lại, hoặc người dùng vừa mở trận khác trong lúc đang hỏi
    state.pollAgain = false;
    return poll();
  }
  if (state.running) state.timer = setTimeout(poll, 500);
  else if (id) refreshRuns(id);
}

function followLive() {
  const m = state.model;
  if (m.lastDay < 0) return;
  state.view.day = m.lastDay;
  const d = m.days[m.lastDay];
  state.view.step = d.results[state.view.bot] ? d.steps : 0;
}

function render() {
  const m = state.model;
  if (!state.view.bot || !(state.view.bot in m.botIndex)) state.view.bot = m.bots[0]?.name || null;
  if (state.live) followLive();
  const d = m.days[state.view.day];
  const steps = d?.steps || 0;
  $("step").max = steps;
  $("step").value = state.view.step;
  $("day-label").textContent = m.config ? `Ngày ${state.view.day + 1}/${m.config.n_days}` : "Ngày –";
  $("step-label").textContent = `Bước ${state.view.step}/${steps}`;
  $("reason-day").textContent = m.config ? `ngày ${state.view.day + 1}` : "";
  $("live").classList.toggle("on", state.live);
  const key = m.bots.map(b => b.name).join("|");
  if (key !== state.botKey) {
    state.botKey = key;
    $("bot-pick").innerHTML = m.bots.map(b => `<option>${esc(b.name)}</option>`).join("");
  }
  $("bot-pick").value = state.view.bot || "";
  draw($("map-canvas"), m, state.view);
  renderScoreboard($("scoreboard"), m);
  renderReasoning($("reasoning"), m, state.view);
  renderLog($("log"), m, name => m.bots[m.botIndex[name]]?.color || "inherit");
  const failed = m.runEnds.filter(r => r.exit_code !== 0 && !r.stopped);
  if (failed.length) {
    banner(failed.map(r => `${r.bot} thoát mã ${r.exit_code}: ${(r.stderr_tail || []).slice(-3).join(" | ")}`).join("\n"), "error");
  } else if (m.runEnds.length && m.runEnds.every(r => r.stopped)) {
    banner("đã dừng theo yêu cầu", "info");
  }
}

function goDay(delta) {
  const m = state.model;
  if (!m.config) return;
  state.live = false;
  stopPlayback();
  state.view.day = Math.min(Math.max(0, m.lastDay), Math.max(0, state.view.day + delta));
  state.view.step = 0;
  render();
}

function togglePlay() {
  if (state.playing) { stopPlayback(); return; }
  state.live = false;
  const steps = state.model.days[state.view.day]?.steps || 0;
  if (state.view.step >= steps) state.view.step = 0;
  $("play").textContent = "❚❚ dừng";
  state.playing = setInterval(() => {
    state.view.step = Math.min(steps, state.view.step + Number($("speed").value));
    render();
    if (state.view.step >= steps) stopPlayback();
  }, 120);
}

function stopPlayback() {
  clearInterval(state.playing);
  state.playing = null;
  $("play").textContent = "▶ phát";
}

async function stop() {
  if (!state.runId) return;
  try {
    const r = await api(`/api/runs/${encodeURIComponent(state.runId)}/stop`, {});
    banner(r.message, "info");
    poll();
  } catch (e) { banner(e.message); }
}

async function startFake() {
  try {
    await api("/api/fake-server", { day_seconds: Number($("fake-seconds").value) || 30 });
    state.options = await api("/api/options");
    updateMode();
    banner("đã mở server giả", "info");
  } catch (e) { banner(e.message); }
}

async function refreshRuns(selected) {
  const data = await api("/api/runs");
  $("runs").innerHTML = `<option value="">—</option>` + data.runs.map(r =>
    `<option value="${esc(r.id)}">${esc(r.id)} — ${esc(r.title || "")}${r.running ? " (đang chạy)" : ""}</option>`).join("");
  $("runs").value = selected || state.runId || "";
}

function bindControls() {
  $("mode").addEventListener("change", updateMode);
  $("add-bot").addEventListener("click", () => addBotRow("search", 1));
  $("load-questions").addEventListener("click", loadQuestions);
  $("start-fake").addEventListener("click", startFake);
  $("run").addEventListener("click", run);
  $("stop").addEventListener("click", stop);
  $("runs").addEventListener("change", e => { if (e.target.value) openRun(e.target.value); });
  $("bot-pick").addEventListener("change", e => { state.view.bot = e.target.value; render(); });
  $("day-prev").addEventListener("click", () => goDay(-1));
  $("day-next").addEventListener("click", () => goDay(1));
  $("step").addEventListener("input", e => {
    state.live = false;
    stopPlayback();
    state.view.step = Number(e.target.value);
    render();
  });
  $("play").addEventListener("click", togglePlay);
  $("live").addEventListener("click", () => { state.live = true; stopPlayback(); render(); });
  window.addEventListener("resize", render);
}

async function init() {
  state.options = await api("/api/options");
  $("map").innerHTML = state.options.maps.map(m => `<option>${esc(m)}</option>`).join("");
  addBotRow("search", 1);
  addBotRow("greedy");
  addBotRow("sample");
  bindControls();
  updateMode();
  await refreshRuns();
  render();
}

init().catch(e => banner(`không khởi động được: ${e.message}`));
