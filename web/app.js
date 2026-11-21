// 狼人杀网页版（多人）前端：每 1.5 秒问一次服务器"有什么新消息、是否轮到我"
// 所有文字都用 textContent 显示（不当作 HTML），防止发言里混进网页代码
const log = document.getElementById("log");
const panel = document.getElementById("panel");
const statusEl = document.getElementById("status");
let game = -1, shown = 0, panelKey = "", countdownEl = null, deadline = 0, draftQ = "", draftSeat = "0";

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

// 点按钮 → 发请求；失败时把原因显示在 errEl
function action(button, errEl, fn) {
  button.onclick = async () => {
    button.disabled = true;
    errEl.textContent = "";
    try { await fn(); panelKey = ""; await tick(); }
    catch (e) { errEl.textContent = e.message; button.disabled = false; }
  };
  return button;
}

function addEvent(ev) {
  let cls = ev.kind;
  if (ev.text.includes("===== 第") && ev.text.includes("夜")) cls += " night";
  if (ev.text.includes("===== 第") && ev.text.includes("天 =====")) cls += " day";
  log.appendChild(el("div", { class: "ev " + cls }, ev.text.replace(/^\n+/, "")));
}

const errLine = () => el("div", { class: "err" });
const row = (...kids) => { const d = el("div", { class: "btns" }); d.append(...kids); return d; };

function nickInput() {
  const i = el("input", { type: "text", maxlength: "12", placeholder: "你的昵称" });
  i.value = localStorage.getItem("nick") || "";
  i.oninput = () => localStorage.setItem("nick", i.value);
  return i;
}

function sizeSelect(sizes, current) {
  const s = el("select");
  for (const n of sizes) {
    const o = el("option", { value: String(n) }, n + " 人局");
    if (n === current) o.selected = true;
    s.appendChild(o);
  }
  return s;
}

// ---------- 轮到你 ----------
function renderTurn(p) {
  panel.appendChild(el("div", { class: "task" }, "👉 " + p.task));
  countdownEl = el("div", { class: "countdown" });
  deadline = Date.now() + p.seconds_left * 1000;
  panel.appendChild(countdownEl);
  const err = errLine();
  let ta = null;
  if (p.speech) {
    ta = el("textarea", { placeholder: "输入你的发言（留空 = 过）", maxlength: "300" });
    panel.appendChild(ta);
  }
  const buttons = [];
  const text = () => (ta ? ta.value : "");
  const send = async (seat) => {
    buttons.forEach(b => (b.disabled = true));
    try { await post("/api/answer", { id: p.id, text: text(), seat }); panelKey = ""; await tick(); }
    catch (e) { err.textContent = "提交失败：" + e.message; buttons.forEach(b => (b.disabled = false)); }
  };
  const box = el("div", { class: "btns" });
  if (p.choices.length) {
    for (const c of p.choices) buttons.push(el("button", { onclick: () => send(c.seat) }, c.label));
    if (p.allow_none) buttons.push(el("button", { onclick: () => send(0) }, "不选 / 弃票"));
  } else {
    buttons.push(el("button", { class: "primary", onclick: () => send(0) }, "发言"));
    buttons.push(el("button", { onclick: () => { if (ta) ta.value = ""; send(0); } }, "过"));
  }
  box.append(...buttons);
  panel.append(box, err);
  if (ta) ta.focus();
}

// ---------- 大厅 ----------
function renderIdle(s) {
  panel.appendChild(el("div", { class: "task" }, "还没有房间，创建一个吧"));
  const nick = nickInput(), size = sizeSelect(s.room.sizes, 6), err = errLine();
  const btn = action(el("button", { class: "primary" }, "创建房间"), err,
    () => post("/api/room/create", { nick: nick.value, size: Number(size.value) }));
  panel.append(row(nick, size, btn), err);
}

// ---------- 赛后讨论 ----------
function renderDiscuss(s) {
  const d = s.discuss, err = errLine();
  panel.appendChild(el("div", { class: "task" }, "💬 赛后讨论：问问 AI 当时是怎么想的"));
  const sel = el("select");
  sel.appendChild(el("option", { value: "0" }, "所有 AI"));
  for (const x of d.seats) sel.appendChild(el("option", { value: String(x.seat) }, x.label));
  sel.value = draftSeat;
  sel.onchange = () => (draftSeat = sel.value);
  const q = el("input", { type: "text", maxlength: "200", placeholder: "例：你为什么毒 3 号？" });
  q.value = draftQ;                      // 页面刷新时保留你打了一半的问题
  q.oninput = () => (draftQ = q.value);
  const btn = el("button", { class: "primary" }, d.busy ? "回答中…" : "提问");
  btn.disabled = d.busy;
  action(btn, err, async () => {
    await post("/api/discuss", { seat: Number(sel.value), question: q.value });
    draftQ = "";
  });
  panel.append(row(sel, q, btn), err, el("hr"));
}

function renderLobby(s) {
  const r = s.room, me = s.me, err = errLine();
  if (s.discuss.available) renderDiscuss(s);
  panel.appendChild(el("div", { class: "task" },
    `房间：${r.size} 人局 · 房主 ${r.host} · 已加入 ${r.members.length} 人，开始后由 ${r.ai} 个 AI 补位`));
  panel.appendChild(el("div", { class: "members" }, "👥 " + r.members.join("、")));
  if (!me.member) {
    const nick = nickInput();
    panel.append(row(nick, action(el("button", { class: "primary" }, "加入"), err,
      () => post("/api/room/join", { nick: nick.value }))), err);
    return;
  }
  if (me.host) {
    const size = sizeSelect(r.sizes, r.size);
    size.onchange = async () => {
      try { await post("/api/room/size", { size: Number(size.value) }); panelKey = ""; await tick(); }
      catch (e) { err.textContent = e.message; size.value = String(r.size); }
    };
    const start = action(el("button", { class: "primary" }, game > 0 ? "🔁 开始下一局" : "开始游戏"), err,
      () => post("/api/room/start"));
    const leave = action(el("button", { class: "danger" }, "解散房间"), err, () => post("/api/room/leave"));
    panel.append(row(size, start, leave), err);
  } else {
    panel.appendChild(el("div", { class: "info" }, "等待房主开始…"));
    panel.append(row(action(el("button", { class: "danger" }, "离开房间"), err, () => post("/api/room/leave"))), err);
  }
}

function renderWaiting(s) {
  panel.appendChild(el("div", { class: "task" }, "⏳ 等待其他玩家…"));
  if (s.me.host) {
    const err = errLine();
    const stop = el("button", { class: "danger" }, "结束本局");
    stop.onclick = async () => {
      if (!confirm("确定要结束这一局吗？")) return;
      try { await post("/api/stop"); } catch (e) { err.textContent = e.message; }
    };
    panel.append(row(stop), err);
  }
}

// ---------- 主循环 ----------
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
    if (!firstLoad) return tick();
  }
  const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
  for (const ev of s.events) { addEvent(ev); shown = ev.i + 1; }
  if (s.events.length === 0 && s.total > shown && !s.me.in_game) shown = s.total;  // 看不到的消息直接跳过
  if (atBottom && s.events.length) window.scrollTo(0, document.body.scrollHeight);

  const st = s.room.state;
  statusEl.textContent = s.pending ? "轮到你了" : st === "running" ? "游戏中" : st === "lobby" ? "大厅" : "没有房间";

  // 面板只在状态变化时重画，避免你打字到一半被清空
  const key = JSON.stringify([st, s.room.members, s.room.size, s.me, s.pending && s.pending.id, game,
                             s.discuss.available, s.discuss.busy]);
  if (key !== panelKey) {
    panelKey = key;
    panel.textContent = "";
    countdownEl = null;
    if (s.pending) renderTurn(s.pending);
    else if (st === "idle") renderIdle(s);
    else if (st === "lobby") renderLobby(s);
    else if (s.me.in_game) renderWaiting(s);
    else panel.appendChild(el("div", { class: "task" }, "游戏进行中，你没有参加这一局，请等待下一局。"));
    if (s.pending) window.scrollTo(0, document.body.scrollHeight);
  }
}

// 倒计时每秒更新一次（不重画面板）
setInterval(() => {
  if (countdownEl) {
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    countdownEl.textContent = `⏱ 剩余 ${left} 秒` + (left === 0 ? "（已超时，系统自动处理）" : "");
  }
}, 1000);

tick();
setInterval(tick, 1500);

// ---------- 排行榜 ----------
const board = document.getElementById("board");
document.getElementById("boardBtn").onclick = async () => {
  board.textContent = "";
  board.hidden = false;
  const close = el("button", { class: "primary", onclick: () => (board.hidden = true) }, "关闭");
  board.appendChild(row(close));
  let b;
  try { b = await (await fetch("/api/leaderboard")).json(); }
  catch (e) { board.appendChild(el("div", { class: "err" }, "读取失败：" + e.message)); return; }
  board.appendChild(el("h2", {}, `🏆 积分榜（${b.games} 局已完成的真实对局）`));
  const t = el("table"), head = el("tr");
  for (const h of ["#", "玩家", "局数", "胜", "胜率", "判断分", "总分", "场均"]) head.appendChild(el("th", {}, h));
  t.appendChild(head);
  b.rows.forEach((r, i) => {
    const tr = el("tr");
    const cells = [i + 1, (r.human ? "👤 " : "🤖 ") + r.name, r.games, r.wins, r.win_rate + "%", r.judge, r.total, r.avg];
    for (const c of cells) tr.appendChild(el("td", {}, String(c)));
    t.appendChild(tr);
    const d = Object.entries(r.detail).map(([k, v]) => `${k}×${v}`).join("  ");
    if (d) { const tr2 = el("tr"); const td = el("td", { class: "detail", colspan: "8" }, d); tr2.appendChild(td); t.appendChild(tr2); }
  });
  board.appendChild(t);
  const rules = Object.entries(b.points).map(([k, v]) => `${b.labels[k]} ${v > 0 ? "+" : ""}${v}`).join("，");
  board.appendChild(el("div", { class: "rules" }, "计分：胜利 +10；" + rules + "。只统计打完的真实对局。"));
};
