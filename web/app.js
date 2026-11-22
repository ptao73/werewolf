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
let gameEvents = [], lastSeatsKey = "", lastNotice = null;
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

// 身份揭晓：三个性格用不同颜色（理性=蓝、好胜=橙、诚实=绿）
const TRAIT_CLASS = { "理性": "t-r", "好胜": "t-c", "诚实": "t-h" };
function addReveal(ev) {
  const box = el("div", { class: "ev reveal" });
  box.appendChild(el("div", {}, "身份揭晓："));
  for (const r of ev.data.rows) {
    const line = el("div", { class: "rv" });
    line.append(el("span", {}, r.label + "："), el("span", { class: "role" }, r.role));
    if (r.persona) {
      line.append("（");
      Object.entries(r.persona).forEach(([k, v], i) => {
        if (i) line.append(" · ");
        line.appendChild(el("span", { class: TRAIT_CLASS[k] || "" }, `${k} ${v}%`));
      });
      line.append("）");
    }
    box.appendChild(line);
  }
  log.appendChild(box);
}

function addEvent(ev) {
  if (ev.kind === "reveal" && ev.data && ev.data.rows) return addReveal(ev);
  if (ev.kind === "seats" && ev.data) {   // 座位改到顶部固定栏；对话里只留"开始"那一行
    if (ev.data.title.includes("开始")) log.appendChild(el("div", { class: "ev public" }, ev.data.title.replace("座位：", "")));
    return;
  }
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
  // 不在任何房间：上面是现有房间（可加入），下面一行是昵称 + 开新房间
  const nick = nickInput(), err = errLine();
  for (const r of s.rooms) {
    const open = r.state === "lobby" && r.members < r.size;
    const status = r.state === "running" ? "游戏中" : open ? "等人中" : "已满";
    const line = el("div", { class: "line" });
    line.appendChild(el("span", { class: "grow" }, `#${r.id} · ${r.size}人局 · 房主 ${r.host} · 👥${r.members}/${r.size} · ${status}`));
    const b = el("button", {}, "加入");
    b.disabled = !open;
    line.appendChild(action(b, err, () => post("/api/room/join", { room: r.id, nick: nick.value })));
    panel.appendChild(line);
  }
  if (!s.rooms.length) panel.appendChild(el("div", { class: "line" }, "还没有房间，开一个吧"));
  const size = sizeSelect(s.room.sizes, 6);
  const full = s.rooms.length >= s.max_rooms;
  const btn = el("button", { class: "primary" }, full ? `已有 ${s.max_rooms} 个房间` : "开新房间");
  btn.disabled = full;
  action(btn, err, () => post("/api/room/create", { nick: nick.value, size: Number(size.value) }));
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
  panel.appendChild(el("div", { class: "line" }, `#${r.id} · ${r.size}人局 · 房主 ${r.host} · 👥 ${r.members.join("、")}${ai}`));
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
    if (r.status === 403) {                // 还没输入邀请码
      const j = await r.json().catch(() => ({}));
      if (j.need_invite) { renderInvite(); return; }
    }
    if (!r.ok) { statusEl.textContent = "连接失败（" + r.status + "）"; return; }
    s = await r.json();
  } catch (e) {
    statusEl.textContent = "连接中断，重试中…";
    return;
  }
  if (s.game !== game) {                 // 新的一局（或刚打开页面）：清空后从头显示
    const firstLoad = game === -1;
    game = s.game; log.textContent = ""; shown = 0; panelKey = ""; follow = true; gameEvents = [];
    if (!firstLoad) { tickAgain = true; return; }
  }
  // 服务器更新了网页：不在你的回合时自动刷新，换上新版本
  if (loadedVersion === null) loadedVersion = s.v;
  else if (s.v !== loadedVersion && !s.pending) { location.reload(); return; }
  meSeat = s.me.seat; meRole = s.me.role;
  for (const ev of s.events) {
    if (ev.i < shown) continue;          // 已经显示过的消息不再重复显示
    gameEvents.push(ev);
    addEvent(ev); shown = ev.i + 1;
  }
  if (s.events.length === 0 && s.total > shown && !s.me.in_game) shown = s.total;  // 看不到的消息直接跳过
  if (s.events.length) toBottom();

  const st = s.room.state;
  statusEl.textContent = s.pending ? "轮到你了" : st === "running" ? "游戏中" : st === "lobby" ? "大厅" : "没有房间";
  updateStartBtn(s);
  renderSeatbar(s.seats || []);
  if (s.notice) lastNotice = s.notice;
  document.getElementById("adminBtn").hidden = !s.me.owner;

  // 面板只在状态变化时重画，避免你打字到一半被清空
  if (s.notice) panelKey = "";            // 有新提示时重画面板
  const key = JSON.stringify([st, lastNotice, st === "idle" ? s.rooms : 0, s.room.members, s.room.size, s.room.ai_ready, s.me, s.pending && s.pending.id, game,
                             s.discuss.available, s.discuss.busy]);
  if (key !== panelKey) {
    panelKey = key;
    panel.textContent = "";
    countdownEl = null;
    if (s.pending) renderTurn(s.pending);
    else if (st === "idle") { if (lastNotice) panel.appendChild(el("div", { class: "notice" }, "ℹ️ " + lastNotice)); renderIdle(s); }
    else if (st === "lobby") renderLobby(s);
    else if (s.me.in_game) { lastNotice = null; renderWaiting(s); }
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
  const close = el("button", { class: "primary", onclick: () => (board.hidden = true) }, "← 回到游戏");
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

// ---------- 顶部按钮：大厅（回到游戏）、开局、规则 ----------
const overlay = board;
statusEl.onclick = () => { overlay.hidden = true; };     // 不管在看排行榜还是规则，点它就回到游戏

const startBtn = document.getElementById("startBtn");
let startAction = null;
function updateStartBtn(s) {
  // 开局按钮：没房间→建房并开局；房主→开局；没加入→加入；其他情况变灰
  const st = s.room.state, me = s.me;
  startAction = null;
  let label = "▶ 开局", tip = "";
  if (st === "idle" && s.rooms.length < s.max_rooms) startAction = "create";
  else if (st === "idle") tip = "房间数已满，请加入已有房间";
  else if (st === "lobby" && me.host) startAction = "start";
  else if (st === "lobby") tip = "只有房主可以开局";
  else tip = "游戏进行中";
  startBtn.textContent = label;
  startBtn.disabled = !startAction;
  startBtn.title = tip;
}
startBtn.onclick = async () => {
  overlay.hidden = true;
  let nick = (localStorage.getItem("nick") || "").trim();
  if (startAction !== "start" && !nick) {
    nick = (prompt("你的昵称（最多 12 个字）") || "").trim().slice(0, 12);
    if (!nick) return;
    localStorage.setItem("nick", nick);
  }
  startBtn.disabled = true;
  try {
    if (startAction === "create") {
      await post("/api/room/create", { nick, size: 6 });
      await post("/api/room/start");
    } else if (startAction === "start") {
      await post("/api/room/start");
    } else if (startAction === "join") {
      await post("/api/room/join", { nick });
    }
  } catch (e) { alert("开局失败：" + e.message); }
  panelKey = ""; await tick();
};

document.getElementById("rulesBtn").onclick = async () => {
  overlay.textContent = "";
  overlay.hidden = false;
  overlay.appendChild(row(el("button", { class: "primary", onclick: () => (overlay.hidden = true) }, "← 回到游戏")));
  let r;
  try { r = await (await fetch("/api/rules")).json(); }
  catch (e) { overlay.appendChild(el("div", { class: "err" }, "读取失败：" + e.message)); return; }
  overlay.appendChild(el("h2", {}, "📖 规则说明"));
  overlay.appendChild(el("div", { class: "rtext" },
    "· 每局由房主选人数；空位由 AI 补上。游戏中大家都只显示座位号，结束后公布每个座位是谁、是什么身份。\n" +
    "· 夜里：守卫、狼人、预言家同时行动，然后女巫行动。白天：公布死讯 → 依次发言 → 所有人同时投票。\n" +
    `· 真人每次行动限时 ${r.turn_seconds} 秒，超时自动处理：发言记为“过”，投票记为弃票，必须选人的随机选。`));
  for (const [n, text] of Object.entries(r.sizes)) {
    overlay.appendChild(el("h3", {}, `${n} 人局`));
    overlay.appendChild(el("div", { class: "rtext" }, text.split("\n").slice(1, -1).join("\n")));
  }
  overlay.appendChild(el("h3", {}, "🤖 AI 玩家的性格"));
  overlay.appendChild(el("div", { class: "rtext" }, r.ai));
  overlay.appendChild(el("h3", {}, "🏠 房间与开局"));
  overlay.appendChild(el("div", { class: "rtext" }, r.rooms));
  overlay.appendChild(el("h3", {}, "🏆 积分"));
  const pts = Object.entries(r.points).map(([k, v]) => `${r.labels[k]} ${v > 0 ? "+" : ""}${v}`).join("，");
  overlay.appendChild(el("div", { class: "rtext" }, "胜利 +10；" + pts + "。只统计打完的真实对局。"));
};

// ---------- 邀请码（朋友第一次来时输入一次）----------
function renderInvite() {
  statusEl.textContent = "需要邀请码";
  startBtn.disabled = true;
  if (panelKey === "invite") return;
  panelKey = "invite";
  panel.textContent = "";
  log.textContent = "";
  log.appendChild(el("div", { class: "ev info" }, "👋 欢迎！第一次来请输入邀请码（向网站主人要），以后就不用再输了。"));
  const code = el("input", { type: "text", maxlength: "12", placeholder: "邀请码", autocapitalize: "characters" });
  const err = errLine();
  const btn = action(el("button", { class: "primary" }, "进入"), err, async () => {
    await post("/api/invite", { code: code.value });
    panelKey = "";
  });
  panel.append(row(code, btn), err);
}

// ---------- 网站主人的管理页：邀请码、已加入的人、今天朋友开了几局 ----------
document.getElementById("adminBtn").onclick = async () => {
  overlay.textContent = "";
  overlay.hidden = false;
  overlay.appendChild(row(el("button", { class: "primary", onclick: () => (overlay.hidden = true) }, "← 回到游戏")));
  let a;
  try { a = await (await fetch("/api/admin")).json(); }
  catch (e) { overlay.appendChild(el("div", { class: "err" }, "读取失败：" + e.message)); return; }
  overlay.appendChild(el("h2", {}, "⚙ 管理"));
  overlay.appendChild(el("div", { class: "rtext" }, `邀请码：${a.code}\n把它发给朋友。换新邀请码后，旧的就不能用了（已经进来的人不受影响）。`));
  const err = errLine();
  overlay.append(row(action(el("button", {}, "换新邀请码"), err, async () => {
    if (!confirm("确定换新邀请码？旧的会失效。")) throw new Error("已取消");
    await post("/api/admin/code"); document.getElementById("adminBtn").click();
  })), err);
  overlay.appendChild(el("h3", {}, `今天朋友开局：${a.used_today} / ${a.limit}（你自己开的不算）`));
  overlay.appendChild(el("h3", {}, `房间（${a.rooms.length}）`));
  if (!a.rooms.length) overlay.appendChild(el("div", { class: "rtext" }, "现在没有房间。"));
  for (const r of a.rooms) {
    const st = r.state === "running" ? "游戏中" : "等人中";
    const line = el("div", { class: "line" });
    line.appendChild(el("span", { class: "grow" }, `#${r.id} · ${r.size}人局 · ${st} · 👥 ${r.names.join("、")} · ${Math.floor(r.idle / 60)} 分钟前有活动`));
    line.appendChild(action(el("button", { class: "danger" }, "删除"), err, async () => {
      if (!confirm(`删除 #${r.id} 房间？` + (r.state === "running" ? "正在进行的这局会被结束。" : ""))) throw new Error("已取消");
      await post("/api/admin/close_room", { room: r.id }); document.getElementById("adminBtn").click();
    }));
    overlay.appendChild(line);
  }
  overlay.appendChild(el("h3", {}, `已加入的朋友（${a.allowed.length}）`));
  for (const p of a.allowed) {
    const line = el("div", { class: "line" });
    line.appendChild(el("span", { class: "grow" }, `${p.nick || "（还没起昵称）"} · ${p.email} · ${p.since}`));
    line.appendChild(action(el("button", { class: "danger" }, "移除"), err, async () => {
      if (!confirm(`移除 ${p.email}？他需要重新输入邀请码才能再进来。`)) throw new Error("已取消");
      await post("/api/admin/remove", { email: p.email }); document.getElementById("adminBtn").click();
    }));
    overlay.appendChild(line);
  }
};

// ---------- 顶部固定座位栏：点座位看他这局说过的话 ----------
const seatbar = document.getElementById("seatbar");
function renderSeatbar(seats) {
  const key = JSON.stringify([seats, meSeat, meRole]);
  if (key === lastSeatsKey) return;
  lastSeatsKey = key;
  seatbar.textContent = "";
  seatbar.hidden = !seats.length;
  for (const x of seats) {
    let cls = "", txt = x.seat + "号";
    if (x.seat === meSeat) { cls += " me"; txt += "（你" + (meRole ? "·" + meRole : "") + "）"; }
    if (!x.alive) cls += " dead";
    seatbar.appendChild(el("button", { class: cls.trim(), onclick: () => showSeat(x.seat) }, txt));
  }
}
function showSeat(n) {
  // 找出这个座位的发言、遗言、赛后回答（消息里以"💬 N号"或"🤖 N号"开头的行）
  const re = new RegExp("^(💬|🤖) " + n + "号[ （：]");
  const lines = gameEvents.map(e => e.text.replace(/^\n+/, "")).filter(t => re.test(t));
  overlay.textContent = "";
  overlay.hidden = false;
  overlay.appendChild(row(el("button", { class: "primary", onclick: () => (overlay.hidden = true) }, "← 回到游戏")));
  overlay.appendChild(el("h2", {}, `${n}号 这局说过的话（${lines.length}）`));
  if (!lines.length) overlay.appendChild(el("div", { class: "rtext" }, "还没有发言。"));
  for (const t of lines) overlay.appendChild(el("div", { class: "ev public" }, t));
}
