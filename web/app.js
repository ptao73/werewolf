// 狼人杀网页版（多人）前端：每 1.5 秒问一次服务器"有什么新消息、是否轮到我"
// 所有文字都用 textContent 显示（不当作 HTML），防止发言里混进网页代码
const log = document.getElementById("log");
const panel = document.getElementById("panel");
const statusEl = document.getElementById("status");
let meSeat = null, meRole = null, loadedVersion = null;
// iPhone：键盘弹出/收起时，让页面高度跟着可见区域走，并把被推走的页面拉回顶部
function fitViewport() {
  const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  document.body.style.height = h + "px";
  window.scrollTo(0, 0);
}
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", fitViewport);
  window.visualViewport.addEventListener("scroll", fitViewport);
}
window.addEventListener("resize", fitViewport);
window.addEventListener("orientationchange", fitViewport);
document.addEventListener("focusout", () => setTimeout(fitViewport, 50));   // 键盘收起
fitViewport();      // 我的座位和身份（用来在座位表里标蓝）
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

function addSeats(ev) {
  // 座位表：自己标蓝（带身份），出局的标红并划线
  const box = el("div", { class: "ev seats-ev" });
  box.appendChild(el("div", {}, ev.data.title));
  const row_ = el("div", { class: "seats" });
  for (const x of ev.data.seats) {
    let cls = "seat", txt = x.seat + "号";
    if (x.seat === meSeat) { cls += " me"; txt += "（你" + (meRole ? "·" + meRole : "") + "）"; }
    if (!x.alive) { cls += " dead"; txt += " 出局"; }
    row_.appendChild(el("span", { class: cls }, txt));
  }
  box.appendChild(row_);
  log.appendChild(box);
}

function addEvent(ev) {
  if (ev.kind === "seats" && ev.data) return addSeats(ev);
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
  const task = el("div", { class: "task" }, "👉 " + p.task);
  countdownEl = el("span", { class: "countdown" });
  deadline = Date.now() + p.seconds_left * 1000;
  task.appendChild(countdownEl);
  panel.appendChild(task);
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
  const nick = nickInput(), size = sizeSelect(s.room.sizes, 6), err = errLine();
  const btn = action(el("button", { class: "primary" }, "创建房间"), err,
    () => post("/api/room/create", { nick: nick.value, size: Number(size.value) }));
  panel.append(row(nick, size, btn), err);
}

// ---------- 赛后讨论 ----------
function renderDiscuss(s) {
  const d = s.discuss, err = errLine();
  const sel = el("select");
  sel.appendChild(el("option", { value: "0" }, "所有 AI"));
  for (const x of d.seats) sel.appendChild(el("option", { value: String(x.seat) }, x.label));
  sel.value = draftSeat;
  sel.onchange = () => (draftSeat = sel.value);
  const q = el("input", { type: "text", maxlength: "200", placeholder: "💬 赛后问 AI：例 你为什么毒3号？" });
  q.value = draftQ;                      // 页面刷新时保留你打了一半的问题
  q.oninput = () => (draftQ = q.value);
  const btn = el("button", { class: "primary" }, d.busy ? "回答中…" : "问");
  btn.disabled = d.busy;
  action(btn, err, async () => {
    await post("/api/discuss", { seat: Number(sel.value), question: q.value });
    draftQ = "";
  });
  panel.append(row(sel, q, btn), err);
}

function renderLobby(s) {
  const r = s.room, me = s.me, err = errLine();
  if (s.discuss.available) renderDiscuss(s);
  const ai = r.ai > 0 ? ` · 🤖${r.ai} ${r.ai_ready ? "✅" : "准备中…"}` : "";
  panel.appendChild(el("div", { class: "line" }, `${r.size}人局 · 房主 ${r.host} · 👥 ${r.members.join("、")}${ai}`));
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
    const start = action(el("button", { class: "primary" }, s.discuss.available ? "🔁 下一局" : "开始游戏"), err,
      () => post("/api/room/start"));
    const leave = action(el("button", { class: "danger" }, "解散"), err, () => post("/api/room/leave"));
    panel.append(row(size, start, leave), err);
  } else {
    panel.append(row(el("span", { class: "info grow" }, "等待房主开始…"),
      action(el("button", { class: "danger" }, "离开"), err, () => post("/api/room/leave"))), err);
  }
}

function renderWaiting(s) {
  const line = el("div", { class: "line" });
  line.appendChild(el("span", { class: "grow" }, "⏳ 等待其他玩家…"));
  const err = errLine();
  if (s.me.host) {
    const stop = el("button", { class: "danger" }, "结束本局");
    stop.onclick = async () => {
      if (!confirm("确定要结束这一局吗？")) return;
      try { await post("/api/stop"); } catch (e) { err.textContent = e.message; }
    };
    line.appendChild(stop);
  }
  panel.append(line, err);
}

// ---------- 主循环 ----------
// 同一时间只允许一个 tick 在跑：以前两个 tick 同时拿到同一批消息，就会显示两遍
let ticking = false, tickAgain = false;
// 自动跟随最新消息：只有你自己往上翻时才停止跟随，翻回底部又恢复
let follow = true;
log.addEventListener("scroll", () => { follow = log.scrollTop + log.clientHeight >= log.scrollHeight - 60; });
const toBottom = () => { if (follow) log.scrollTop = log.scrollHeight; };
async function tick() {
  if (ticking) { tickAgain = true; return; }
  ticking = true;
  try { await tickOnce(); } finally { ticking = false; }
  if (tickAgain) { tickAgain = false; await tick(); }
}

async function tickOnce() {
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
    game = s.game; log.textContent = ""; shown = 0; panelKey = ""; follow = true;
    if (!firstLoad) { tickAgain = true; return; }
  }
  // 服务器更新了网页：不在你的回合时自动刷新，换上新版本
  if (loadedVersion === null) loadedVersion = s.v;
  else if (s.v !== loadedVersion && !s.pending) { location.reload(); return; }
  meSeat = s.me.seat; meRole = s.me.role;
  for (const ev of s.events) {
    if (ev.i < shown) continue;          // 已经显示过的消息不再重复显示
    addEvent(ev); shown = ev.i + 1;
  }
  if (s.events.length === 0 && s.total > shown && !s.me.in_game) shown = s.total;  // 看不到的消息直接跳过
  if (s.events.length) toBottom();

  const st = s.room.state;
  statusEl.textContent = s.pending ? "轮到你了" : st === "running" ? "游戏中" : st === "lobby" ? "大厅" : "没有房间";

  // 面板只在状态变化时重画，避免你打字到一半被清空
  const key = JSON.stringify([st, s.room.members, s.room.size, s.room.ai_ready, s.me, s.pending && s.pending.id, game,
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
    if (s.pending) follow = true;        // 轮到你时总是跳到最新
    toBottom();                          // 操作区高度变了，消息区也要重新贴底
  }
}

// 倒计时每秒更新一次（不重画面板）
setInterval(() => {
  if (countdownEl) {
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    countdownEl.textContent = left > 0 ? `⏱${left}s` : "⏱ 已超时";
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
