// 狼人杀网页版前端：每 1.5 秒问一次服务器"有什么新消息、是否轮到我"
// 所有文字都用 textContent 显示（不当作 HTML），防止 AI 的发言里混进网页代码
const log = document.getElementById("log");
const panel = document.getElementById("panel");
const statusEl = document.getElementById("status");
let game = -1, shown = 0, panelKey = "", pool = [];

function el(tag, attrs = {}, text) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "onclick") e.onclick = v; else e.setAttribute(k, v);
  }
  if (text !== undefined) e.textContent = text;
  return e;
}

async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Werewolf": "1" },
    body: JSON.stringify(body || {}),
  });
  const j = await r.json().catch(() => ({ error: "服务器没有正常回复（" + r.status + "）" }));
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}

function addEvent(ev) {
  let cls = ev.kind;
  if (ev.text.includes("===== 第") && ev.text.includes("夜")) cls += " night";
  if (ev.text.includes("===== 第") && ev.text.includes("天 =====")) cls += " day";
  log.appendChild(el("div", { class: "ev " + cls }, ev.text.replace(/^\n+/, "")));
}

function errLine() { return el("div", { class: "err" }); }

// 提交回答：按钮先变灰，失败时显示原因
async function send(body, errEl, buttons) {
  buttons.forEach(b => (b.disabled = true));
  try {
    await post("/api/answer", body);
    panelKey = ""; // 让面板马上刷新
    await tick();
  } catch (e) {
    errEl.textContent = "提交失败：" + e.message;
    buttons.forEach(b => (b.disabled = false));
  }
}

function renderTurn(p) {
  panel.appendChild(el("div", { class: "task" }, "👉 " + p.task));
  const err = errLine();
  let ta = null;
  if (p.speech) {
    ta = el("textarea", { placeholder: "输入你的发言（留空 = 过）", maxlength: "300" });
    panel.appendChild(ta);
  }
  const box = el("div", { class: "btns" });
  const buttons = [];
  const text = () => (ta ? ta.value : "");
  if (p.choices.length) {
    for (const c of p.choices) {
      const b = el("button", { onclick: () => send({ id: p.id, text: text(), seat: c.seat }, err, buttons) }, c.label);
      buttons.push(b); box.appendChild(b);
    }
    if (p.allow_none) {
      const b = el("button", { onclick: () => send({ id: p.id, text: text(), seat: 0 }, err, buttons) }, "不选 / 弃票");
      buttons.push(b); box.appendChild(b);
    }
  } else {
    const b = el("button", { class: "primary", onclick: () => send({ id: p.id, text: text(), seat: 0 }, err, buttons) }, "发言");
    const b2 = el("button", { onclick: () => { if (ta) ta.value = ""; send({ id: p.id, text: "", seat: 0 }, err, buttons); } }, "过");
    buttons.push(b, b2); box.append(b, b2);
  }
  panel.append(box, err);
  if (ta) ta.focus();
}

function renderFailure(p) {
  panel.appendChild(el("div", { class: "task" }, "⚠️ 有 AI 调用失败，怎么办？"));
  const err = errLine(), box = el("div", { class: "btns" }), buttons = [];
  for (const [c, label] of [["r", "重试"], ["s", "换一个模型接替"], ["q", "退出本局"]]) {
    const b = el("button", { class: c === "q" ? "danger" : "", onclick: () => send({ id: p.id, choice: c }, err, buttons) }, label);
    buttons.push(b); box.appendChild(b);
  }
  panel.append(box, err);
}

function renderIdle(hasPlayed) {
  const err = errLine();
  const start = el("button", { class: "primary" }, hasPlayed ? "🔁 下一局" : "开始游戏");
  // 可选：指定参加的模型（不选 = 全部随机）
  const det = el("details");
  det.appendChild(el("summary", {}, "指定模型（可选，最多 5 个，不够的随机补齐）"));
  const box = el("div", { class: "pool" });
  const checks = pool.map(name => {
    const cb = el("input", { type: "checkbox", value: name });
    const lab = el("label"); lab.append(cb, " " + name); box.appendChild(lab);
    return cb;
  });
  det.appendChild(box);
  start.onclick = async () => {
    const players = checks.filter(c => c.checked).map(c => c.value);
    if (players.length > 5) { err.textContent = "最多选 5 个"; return; }
    start.disabled = true;
    try { await post("/api/new", { players }); panelKey = ""; await tick(); }
    catch (e) { err.textContent = "开始失败：" + e.message; start.disabled = false; }
  };
  panel.append(el("div", { class: "btns" }), det, err);
  panel.firstChild.appendChild(start);
}

function renderWaiting() {
  panel.appendChild(el("div", { class: "task" }, "⏳ 等待其他玩家…"));
  const err = errLine();
  const stop = el("button", { class: "danger" }, "结束本局");
  stop.onclick = async () => {
    if (!confirm("确定要结束这一局吗？")) return;
    try { await post("/api/stop"); } catch (e) { err.textContent = e.message; }
  };
  const box = el("div", { class: "btns" }); box.appendChild(stop);
  panel.append(box, err);
}

async function tick() {
  let s;
  try {
    const r = await fetch("/api/state?since=" + (game === -1 ? 0 : shown), { headers: { "X-Werewolf": "1" } });
    if (!r.ok) { statusEl.textContent = "连接失败（" + r.status + "）"; return; }
    s = await r.json();
  } catch (e) {
    statusEl.textContent = "连接中断，重试中…";
    return;
  }
  if (s.game !== game) {                 // 新的一局（或刚打开页面）：清空后从头显示
    const firstLoad = game === -1;
    game = s.game; log.textContent = ""; shown = 0; panelKey = "";
    if (!firstLoad) return tick();       // 换局了：重新从第 0 条拉取
  }
  const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
  for (const ev of s.events) if (ev.i === shown) { addEvent(ev); shown++; }
  if (atBottom && s.events.length) window.scrollTo(0, document.body.scrollHeight);
  statusEl.textContent = s.running ? (s.pending ? "轮到你了" : "进行中") : "未开始";

  // 面板只在状态变化时重画，避免你打字到一半被清空
  const key = JSON.stringify([s.running, s.pending && s.pending.id, game]);
  if (key !== panelKey) {
    panelKey = key;
    panel.textContent = "";
    if (s.pending && s.pending.type === "turn") renderTurn(s.pending);
    else if (s.pending && s.pending.type === "failure") renderFailure(s.pending);
    else if (s.running) renderWaiting();
    else renderIdle(game > 0);
    if (s.pending) window.scrollTo(0, document.body.scrollHeight);
  }
}

(async () => {
  try { pool = await (await fetch("/api/pool")).json(); } catch (e) { pool = []; }
  await tick();
  setInterval(tick, 1500);
})();
