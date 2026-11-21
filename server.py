#!/usr/bin/env python3
"""狼人杀网页版服务器。

在 VM 上运行：python3 server.py
- 只监听 127.0.0.1:8010，外网只能通过 Cloudflare 隧道进来
- 从隧道进来的请求必须带有效的 Cloudflare Access 登录凭证（JWT），否则一律 403
- 同一时间只跑一局（只有你一个真人玩家）
"""
import base64, json, random, sys, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import werewolf as w

HOST, PORT = "127.0.0.1", 8010
PUBLIC_HOST = "werewolf.potato73.win"
ACCESS_TEAM = "ptao"
# Access 应用的 AUD 标签（在 Cloudflare 后台 Access 应用的 Overview 里能看到）。放在 access_aud.txt 里，可以多行
AUD_FILE = Path(__file__).with_name("access_aud.txt")
HERE = Path(__file__).parent
FAKE = "--fake" in sys.argv   # 测试用：AI 随机行动，不花额度


# ---------- Cloudflare Access 登录凭证检查 ----------
def b64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class AccessCheck:
    def __init__(self, team):
        self.team = team
        self.keys, self.keys_time = {}, 0
        self.lock = threading.Lock()

    def audiences(self):
        if not AUD_FILE.exists():
            return set()
        return {l.strip() for l in AUD_FILE.read_text().splitlines() if l.strip()}

    def get_key(self, kid):
        with self.lock:
            # 公钥缓存 1 小时；遇到不认识的 kid 时（Cloudflare 换钥匙了）最多每分钟重新拉一次
            if time.time() - self.keys_time > 3600 or (kid not in self.keys and time.time() - self.keys_time > 60):
                url = f"https://{self.team}.cloudflareaccess.com/cdn-cgi/access/certs"
                data = json.load(urlopen(url, timeout=10))
                self.keys = {k["kid"]: k for k in data.get("keys", []) if k.get("kty") == "RSA"}
                self.keys_time = time.time()
            return self.keys.get(kid)

    def verify(self, token):
        """凭证有效就返回登录邮箱，否则抛出异常"""
        h, p, sig = token.split(".")
        header, claims = json.loads(b64(h)), json.loads(b64(p))
        if header.get("alg") != "RS256":
            raise ValueError("签名算法不对")
        k = self.get_key(header.get("kid"))
        if not k:
            raise ValueError("找不到对应公钥")
        pub = rsa.RSAPublicNumbers(int.from_bytes(b64(k["e"]), "big"), int.from_bytes(b64(k["n"]), "big")).public_key()
        pub.verify(b64(sig), f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())  # 签名不对会抛异常
        now = time.time()
        if claims.get("iss") != f"https://{self.team}.cloudflareaccess.com":
            raise ValueError("签发方不对")
        aud = claims.get("aud", [])
        aud = [aud] if isinstance(aud, str) else aud
        if not set(aud) & self.audiences():
            raise ValueError("AUD 不匹配（access_aud.txt 里没有这个应用）")
        if not (claims.get("exp", 0) > now and claims.get("nbf", 0) <= now + 60):
            raise ValueError("凭证过期")
        return claims.get("email", "?")


ACCESS = AccessCheck(ACCESS_TEAM)


# ---------- 游戏控制：网页和游戏线程之间传话 ----------
TURN_SECONDS = 120     # 真人每次行动的时间限制
SIZES = sorted(w.ROLESETS)


class WebGame(w.Game):
    """规则和终端版完全一样；多人网页版：匿名、同时投票、AI 出错自动处理"""
    anonymous = True
    parallel_votes = True
    auto_recover = True

    def __init__(self, ctl, *args, **kw):
        self.ctl = ctl
        self.quit_raised = False
        super().__init__(*args, **kw)

    def check_stop(self):
        """房主点了"结束本局"：抛出一次 Quit（只抛一次，避免退出过程中再次触发）"""
        with self.ctl.cond:
            if self.ctl.stop_flag and not self.quit_raised:
                self.quit_raised = True
                raise w.Quit()

    def emit(self, text, kind="info", to=None):
        if kind == "logpath":          # 日志路径只给管理员看，不发到网页
            return
        self.ctl.add(text, kind, to["uid"] if to else None)
        self.check_stop()

    def call_ai(self, p, prompt):
        self.check_stop()              # 夜里没有输出时也能尽快停下
        return super().call_ai(p, prompt)

    def ask_human(self, p, task, choices, allow_none, speech):
        self.check_stop()
        spec = {"type": "turn", "task": task, "speech": speech, "allow_none": allow_none,
                "choices": [{"seat": c["seat"], "label": self.label(c)} for c in (choices or [])]}
        ans = self.ctl.wait_answer(p["uid"], spec, TURN_SECONDS)
        if ans == "STOP":
            self.check_stop()
            ans = None                 # 退出信号已由别的线程抛出：这里随便给个默认值让线程结束
        if ans is None:                # 超时：自动处理，只私下告诉本人
            valid = [c["seat"] for c in (choices or [])]
            seat = 0 if (allow_none or not valid) else random.choice(valid)
            if not self.quit_raised:
                what = "发言记为“过”" if speech else ("不选/弃票" if seat == 0 else f"随机选了 {seat}号")
                self.emit(f"⏰ 你超时了，系统已自动{what}。", "private", to=p)
            return ("过。" if speech else ""), seat
        text = w.clean_input(str(ans.get("text", ""))).strip()[:300]
        if speech and not text:
            text = "过。"
        return text, int(ans.get("seat") or 0)


class Controller:
    """一个房间：大厅（等人）→ 游戏中 → 回到大厅"""

    def __init__(self):
        self.cond = threading.Condition()
        self.state = "idle"            # idle 没房间 / lobby 等人 / running 游戏中
        self.host, self.size = None, 6
        self.members = {}              # uid → 昵称（按加入顺序）
        self.players = set()           # 本局参加的 uid（游戏中才有）
        self.events, self.game_no = [], 0
        self.pending = {}              # uid → 待回答的问题
        self.answers = {}              # uid → 回答
        self.next_id = 0
        self.stop_flag = False
        self.last_game = None          # 刚结束的那一局（赛后讨论用）
        self.model_ok, self.model_time = [], 0   # 后台检查过的可用模型、检查时间
        self.checking = False
        self.asking = False            # 赛后讨论：同一时间只问一个问题

    # ----- 游戏线程用 -----
    def add(self, text, kind, to=None):
        with self.cond:
            self.events.append({"i": len(self.events), "kind": kind, "text": text, "to": to})
            self.cond.notify_all()

    def wait_answer(self, uid, spec, timeout):
        """等某位玩家回答。返回回答；超时返回 None；本局被结束返回 "STOP" """
        with self.cond:
            self.next_id += 1
            self.pending[uid] = dict(spec, id=self.next_id, deadline=time.time() + timeout)
            self.answers.pop(uid, None)
            end = time.time() + timeout
            try:
                while uid not in self.answers:
                    if self.stop_flag:
                        return "STOP"
                    left = end - time.time()
                    if left <= 0:
                        return None
                    self.cond.wait(min(left, 1))
                return self.answers.pop(uid)
            finally:
                self.pending.pop(uid, None)

    # ----- 后台检查 AI 模型（开房间时就开始，开局时直接用结果，不用再等）-----
    CACHE_SECONDS = 15 * 60

    def prewarm(self):
        """如果结果已过期且没在检查，就在后台检查一遍全部模型"""
        with self.cond:
            if self.checking or (time.time() - self.model_time < self.CACHE_SECONDS and self.model_ok):
                return
            self.checking = True
        threading.Thread(target=self.check_models, daemon=True).start()

    def check_models(self):
        try:
            if FAKE:
                ok = list(w.MODEL_POOL)
            else:
                import tempfile
                from concurrent.futures import ThreadPoolExecutor
                d = tempfile.mkdtemp(prefix="werewolf-check-")
                with ThreadPoolExecutor(len(w.MODEL_POOL)) as ex:
                    res = list(ex.map(lambda m: w.run_cli(m, w.PING, w.PING_TIMEOUT, d), w.MODEL_POOL))
                ok = [m for m, (good, _) in zip(w.MODEL_POOL, res) if good]
                bad = [f"{m['name']}: {err.splitlines()[-1][-120:] if err else '?'}"
                       for m, (good, err) in zip(w.MODEL_POOL, res) if not good]
                print(f"[模型检查] 可用 {len(ok)}/{len(w.MODEL_POOL)}" + (f"；不可用：{bad}" if bad else ""),
                      file=sys.stderr, flush=True)
            with self.cond:
                self.model_ok, self.model_time = ok, time.time()
        finally:
            with self.cond:
                self.checking = False
                self.cond.notify_all()

    def ready_models(self, need):
        """开局时取 AI：优先用后台检查的结果；正在检查就等它；都没有才现场检查"""
        with self.cond:
            end = time.time() + 60
            while self.checking and time.time() < end:
                self.cond.wait(1)
            fresh = time.time() - self.model_time < self.CACHE_SECONDS and self.model_ok
            ok = list(self.model_ok) if fresh else None
        if not ok:
            return None
        random.shuffle(ok)
        chosen = ok[:need]
        while len(chosen) < need:              # 可用模型不够坐满：同一模型坐多个座位
            chosen.append(dict(random.choice(ok)))
        spare = [dict(m) for m in ok]          # 中途有 AI 出错时的替补
        return [dict(m) for m in chosen], spare

    # ----- 网页请求用（都要先拿到锁）-----
    def give_answer(self, uid, data):
        with self.cond:
            p = self.pending.get(uid)
            if not p or data.get("id") != p["id"]:
                return "这个问题已经过期，请刷新页面"
            seat = data.get("seat") or 0
            valid = [c["seat"] for c in p["choices"]] + ([0] if p["allow_none"] or not p["choices"] else [])
            if not isinstance(seat, int) or seat not in valid:
                return "无效座位号"
            self.answers[uid] = data
            self.cond.notify_all()
            return None

    @staticmethod
    def clean_nick(nick):
        nick = w.clean_input(str(nick or "")).strip()
        return nick[:12]

    def create(self, uid, nick, size):
        nick = self.clean_nick(nick)
        with self.cond:
            if self.state != "idle":
                return "已经有房间了，请直接加入"
            if not nick:
                return "请输入昵称"
            if size not in SIZES:
                return "人数只能是 " + "/".join(map(str, SIZES))
            self.state, self.host, self.size = "lobby", uid, size
            self.members = {uid: nick}
            self.events, self.players = [], set()
        self.prewarm()                         # 大家进房间的这段时间里，后台先把 AI 检查好
        return None

    def join(self, uid, nick):
        nick = self.clean_nick(nick)
        with self.cond:
            if self.state != "lobby":
                return "现在不能加入（没有房间或游戏进行中）"
            if not nick:
                return "请输入昵称"
            if nick in [n for u, n in self.members.items() if u != uid]:
                return "这个昵称已经有人用了"
            if uid not in self.members and len(self.members) >= self.size:
                return "房间已满"
            self.members[uid] = nick
            return None

    def leave(self, uid):
        with self.cond:
            if self.state != "lobby" or uid not in self.members:
                return "现在不能离开"
            del self.members[uid]
            if uid == self.host:       # 房主走了：房间解散
                self.state, self.host, self.members = "idle", None, {}
            return None

    def set_size(self, uid, size):
        with self.cond:
            if uid != self.host or self.state != "lobby":
                return "只有房主可以改人数"
            if size not in SIZES:
                return "人数只能是 " + "/".join(map(str, SIZES))
            if len(self.members) > size:
                return f"已经有 {len(self.members)} 人加入，不能少于这个数"
            self.size = size
            return None

    def start(self, uid):
        with self.cond:
            if uid != self.host or self.state != "lobby":
                return "只有房主可以开始游戏"
            humans = [{"name": n, "kind": "human", "uid": u} for u, n in self.members.items()]
            self.state, self.players = "running", set(self.members)
            self.events, self.game_no = [], self.game_no + 1
            self.pending, self.answers, self.stop_flag = {}, {}, False
            self.last_game, self.asking = None, False
            size = self.size
        threading.Thread(target=self.run_game, args=(humans, size), daemon=True).start()
        return None

    def run_game(self, humans, size):
        need = size - len(humans)
        try:
            game_log = []
            ready = self.ready_models(need) if need else ([], [])
            if ready:
                chosen, spare = ready
                game_log.append("用后台检查结果：" + "、".join(m["name"] for m in chosen))
            else:
                self.add(f"🤖 正在准备 {need} 个 AI 玩家…（约 15 秒）", "info")
                # 检查过程只写日志：不让大家知道有哪些模型
                chosen, spare = w.pick_players([], need, FAKE, out=game_log.append, allow_repeat=True)
            g = WebGame(self, chosen + humans, spare, fake=FAKE)
            for line in game_log:
                g.log("[模型检查] " + line)
            g.run()
            with self.cond:
                self.last_game = g
        except w.NotEnoughModels as e:
            self.add(f"⚠️ {e}", "warn")
        except Exception:
            self.add("⚠️ 程序出错，本局结束。", "warn")
            print(traceback.format_exc(), file=sys.stderr, flush=True)
        finally:
            with self.cond:
                self.state = "lobby" if self.host in self.members else "idle"
                self.pending, self.stop_flag = {}, False
                self.cond.notify_all()
            self.prewarm()                     # 为下一局提前检查

    def discuss(self, uid, seat, question):
        """赛后讨论：房间里的人问某位 AI（seat=0 表示问所有 AI）"""
        question = w.clean_input(str(question or "")).strip()[:200]
        with self.cond:
            g = self.last_game
            if self.state != "lobby" or not g or uid not in self.members:
                return "现在不能提问（只能在一局结束后、下一局开始前）"
            if self.asking:
                return "上一个问题还在回答中，请稍等"
            if not question:
                return "请输入问题"
            ais = g.ai_seats()
            targets = ais if seat == 0 else [p for p in ais if p["seat"] == seat]
            if not targets:
                return "这个座位不是 AI"
            self.asking = True
            nick = self.members[uid]
        threading.Thread(target=self.run_discuss, args=(g, nick, targets, question, seat), daemon=True).start()
        return None

    def run_discuss(self, g, nick, targets, question, seat):
        try:
            who = "所有 AI" if seat == 0 else g.real_label(targets[0])
            self.add(f"❓ {nick} 问 {who}：{question}", "public")
            for t in targets:
                self.add(f"   …{g.real_label(t)} 思考中", "thinking")
                try:
                    ans = g.explain(t, question, nick)
                except w.Quit:
                    self.add(f"⚠️ {g.real_label(t)} 无法回答（AI 调用失败）", "warn")
                    continue
                self.add(f"🤖 {g.real_label(t)}：{ans}", "public")
        except Exception:
            self.add("⚠️ 回答时程序出错。", "warn")
            print(traceback.format_exc(), file=sys.stderr, flush=True)
        finally:
            with self.cond:
                self.asking = False
                self.cond.notify_all()

    def stop(self, uid):
        with self.cond:
            if uid != self.host or self.state != "running":
                return "只有房主可以结束本局"
            self.stop_flag = True
            self.cond.notify_all()
            return None

    def view(self, uid, since):
        """某位玩家能看到的状态：公开消息 + 只发给他的消息"""
        with self.cond:
            member = uid in self.members
            in_game = uid in self.players
            show = in_game or (self.state != "running" and member)
            evs = [{"i": e["i"], "kind": e["kind"], "text": e["text"]}
                   for e in self.events[since:] if show and (e["to"] is None or e["to"] == uid)]
            p = self.pending.get(uid)
            if p:
                p = dict(p, seconds_left=max(0, int(p["deadline"] - time.time())))
                p.pop("deadline", None)
            g = self.last_game
            discuss = {"available": bool(g and self.state == "lobby" and member), "busy": self.asking,
                       "seats": [{"seat": p["seat"], "label": g.real_label(p)} for p in g.ai_seats()] if g else []}
            return {
                "discuss": discuss,
                "me": {"nick": self.members.get(uid), "member": member, "host": uid == self.host, "in_game": in_game},
                "room": {"state": self.state, "size": self.size, "sizes": SIZES,
                         "host": self.members.get(self.host), "members": list(self.members.values()),
                         "ai": max(0, self.size - len(self.members)),
                         "ai_ready": (not self.checking) and bool(self.model_ok)
                                     and time.time() - self.model_time < self.CACHE_SECONDS},
                "game": self.game_no, "pending": p, "events": evs, "total": len(self.events),
            }


CTL = Controller()


# ---------- 网页请求处理 ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 不往终端刷访问日志
        pass

    def identify(self):
        """返回玩家身份（登录邮箱）；不合格就回 403 并返回 None"""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host in ("127.0.0.1", "localhost") and self.client_address[0] == "127.0.0.1" \
                and not self.headers.get("Cf-Connecting-Ip"):
            return "local:" + (self.headers.get("X-Test-User") or "me")   # 本机测试用
        if host != PUBLIC_HOST:
            self.reply(403, {"error": "forbidden"})
            return None
        token = self.headers.get("Cf-Access-Jwt-Assertion", "")
        if not token:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "CF_Authorization":
                    token = v
        try:
            return ACCESS.verify(token).lower()
        except Exception as e:
            self.reply(403, {"error": f"未通过 Cloudflare Access 登录验证：{e}"})
            return None

    def reply(self, code, obj=None, body=None, ctype="application/json; charset=utf-8"):
        data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        uid = self.identify()
        if not uid:
            return
        u = urlsplit(self.path)
        if u.path == "/":
            return self.reply(200, body=(HERE / "web" / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
        if u.path == "/app.js":
            return self.reply(200, body=(HERE / "web" / "app.js").read_bytes(), ctype="text/javascript; charset=utf-8")
        if u.path == "/api/leaderboard":       # 积分榜（真人只显示昵称，不显示邮箱）
            import stats
            b = stats.leaderboard()
            return self.reply(200, {"games": b["games"], "points": stats.POINTS, "labels": stats.LABELS,
                                    "rows": [{k: r[k] for k in ("name", "human", "games", "wins", "win_rate",
                                                                "judge", "total", "avg", "detail")} for r in b["rows"]]})
        if u.path == "/api/state":
            q = parse_qs(u.query)
            try:
                since = max(0, int(q.get("since", ["0"])[0] or 0))
            except ValueError:
                since = 0
            return self.reply(200, CTL.view(uid, since))
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        uid = self.identify()
        if not uid:
            return
        # 防止别的网站借你的登录状态偷偷发请求：必须带这个自定义头（跨站请求带不上）
        if self.headers.get("X-Werewolf") != "1":
            return self.reply(403, {"error": "missing header"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
            if not isinstance(data, dict):
                raise ValueError
        except Exception:
            return self.reply(400, {"error": "bad json"})
        size = data.get("size")
        actions = {
            "/api/room/create": lambda: CTL.create(uid, data.get("nick"), size),
            "/api/room/join": lambda: CTL.join(uid, data.get("nick")),
            "/api/room/leave": lambda: CTL.leave(uid),
            "/api/room/size": lambda: CTL.set_size(uid, size),
            "/api/room/start": lambda: CTL.start(uid),
            "/api/stop": lambda: CTL.stop(uid),
            "/api/answer": lambda: CTL.give_answer(uid, data),
            "/api/discuss": lambda: CTL.discuss(uid, data.get("seat") if isinstance(data.get("seat"), int) else -1,
                                                data.get("question")),
        }
        act = actions.get(urlsplit(self.path).path)
        if not act:
            return self.reply(404, {"error": "not found"})
        err = act()
        self.reply(400 if err else 200, {"error": err} if err else {"ok": True})


if __name__ == "__main__":
    if "--port" in sys.argv:        # 测试用：换端口
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    if "--turn" in sys.argv:        # 测试用：缩短真人时间限制
        TURN_SECONDS = int(sys.argv[sys.argv.index("--turn") + 1])
    print(f"狼人杀网页版：http://{HOST}:{PORT}  （公网：https://{PUBLIC_HOST}）", flush=True)
    if not ACCESS.audiences():
        print("提示：还没有 access_aud.txt，公网请求会全部被拒绝（403）。", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
