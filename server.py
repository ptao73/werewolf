#!/usr/bin/env python3
"""狼人杀网页版服务器。

在 VM 上运行：python3 server.py
- 只监听 127.0.0.1:8010，外网只能通过 Cloudflare 隧道进来
- 从隧道进来的请求必须带有效的 Cloudflare Access 登录凭证（JWT），否则一律 403
- 同一时间只跑一局（只有你一个真人玩家）
"""
import base64, json, sys, threading, time, traceback
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
class WebGame(w.Game):
    """和终端版规则完全一样，只是把"打印"和"输入"换成网页"""

    def __init__(self, ctl, *args, **kw):
        self.ctl = ctl
        super().__init__(*args, **kw)

    def emit(self, text, kind="info"):
        self.ctl.add(text, kind)
        # 你点了"结束本局"：下一次有输出时就停下（不用等到轮到你）
        if self.ctl.take_stop():
            raise w.Quit()

    def call_ai(self, p, prompt):
        # 每次调用 AI 之前也检查"结束本局"，夜里没有输出时也能尽快停下
        if self.ctl.take_stop():
            raise w.Quit()
        return super().call_ai(p, prompt)

    def ask_human(self, task, choices, allow_none, speech):
        spec = {"type": "turn", "task": task, "speech": speech, "allow_none": allow_none,
                "choices": [{"seat": c["seat"], "label": self.label(c)} for c in (choices or [])]}
        ans = self.ctl.wait_answer(spec)
        text = w.clean_input(str(ans.get("text", ""))).strip()[:300]
        if speech and not text:
            text = "过。"
        return text, int(ans.get("seat") or 0)

    def ask_failure(self):
        return self.ctl.wait_answer({"type": "failure"})["choice"]


class Controller:
    def __init__(self):
        self.cond = threading.Condition()
        self.events, self.game_no = [], 0
        self.pending, self.pending_id, self.answer = None, 0, None
        self.running, self.stop_flag = False, False

    def add(self, text, kind):
        with self.cond:
            self.events.append({"i": len(self.events), "kind": kind, "text": text})
            self.cond.notify_all()

    def wait_answer(self, spec):
        """游戏线程在这里等你在网页上回答"""
        with self.cond:
            self.pending_id += 1
            self.pending = dict(spec, id=self.pending_id)
            self.answer = None
            while self.answer is None:
                if self.stop_flag:
                    self.stop_flag, self.pending = False, None   # 退出信号只用一次
                    raise w.Quit()
                self.cond.wait(1)
            ans, self.answer, self.pending = self.answer, None, None
            return ans

    def give_answer(self, data):
        """网页提交回答；先检查是否合法"""
        with self.cond:
            p = self.pending
            if not p or data.get("id") != p["id"]:
                return "这个问题已经过期，请刷新页面"
            if p["type"] == "failure":
                if data.get("choice") not in ("r", "s", "q"):
                    return "无效选项"
            else:
                seat = data.get("seat") or 0
                valid = [c["seat"] for c in p["choices"]] + ([0] if p["allow_none"] or not p["choices"] else [])
                if not isinstance(seat, int) or seat not in valid:
                    return "无效座位号"
            self.answer = data
            self.cond.notify_all()
            return None

    def start(self, wanted_names):
        with self.cond:
            if self.running:
                return "已经有一局在进行"
            try:
                wanted = w.find_models(wanted_names) if wanted_names else []
            except SystemExit as e:
                return str(e)
            if len(wanted) > 5:
                return "最多指定 5 个模型"
            self.events, self.game_no = [], self.game_no + 1
            self.running, self.stop_flag, self.pending = True, False, None
        threading.Thread(target=self.run_game, args=(wanted,), daemon=True).start()
        return None

    def run_game(self, wanted):
        try:
            chosen, spare = w.pick_players(wanted, 5, FAKE, out=lambda t: self.add(t, "info"))
            WebGame(self, chosen + [w.HUMAN], spare, fake=FAKE).run()
        except w.NotEnoughModels as e:
            self.add(f"⚠️ {e}", "warn")
        except Exception:
            self.add("⚠️ 程序出错：\n" + traceback.format_exc()[-800:], "warn")
        finally:
            with self.cond:
                self.running, self.pending = False, None
                self.cond.notify_all()

    def take_stop(self):
        """取走"结束本局"信号（只取一次，避免退出过程中再次触发）"""
        with self.cond:
            flag, self.stop_flag = self.stop_flag, False
            return flag

    def stop(self):
        with self.cond:
            self.stop_flag = True
            self.cond.notify_all()

    def state(self, since):
        with self.cond:
            return {"game": self.game_no, "running": self.running, "pending": self.pending,
                    "events": self.events[since:], "total": len(self.events)}


CTL = Controller()


# ---------- 网页请求处理 ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 不往终端刷访问日志
        pass

    def check_access(self):
        """从隧道来的请求必须带有效的 Access 凭证；本机测试（localhost）放行"""
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host in ("127.0.0.1", "localhost") and self.client_address[0] == "127.0.0.1" \
                and not self.headers.get("Cf-Connecting-Ip"):
            return True
        if host != PUBLIC_HOST:
            self.reply(403, {"error": "forbidden"})
            return False
        token = self.headers.get("Cf-Access-Jwt-Assertion", "")
        if not token:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "CF_Authorization":
                    token = v
        try:
            ACCESS.verify(token)
            return True
        except Exception as e:
            self.reply(403, {"error": f"未通过 Cloudflare Access 登录验证：{e}"})
            return False

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
        if not self.check_access():
            return
        u = urlsplit(self.path)
        if u.path == "/":
            return self.reply(200, body=(HERE / "web" / "index.html").read_bytes(), ctype="text/html; charset=utf-8")
        if u.path == "/app.js":
            return self.reply(200, body=(HERE / "web" / "app.js").read_bytes(), ctype="text/javascript; charset=utf-8")
        if u.path == "/api/state":
            q = parse_qs(u.query)
            since = int(q.get("since", ["0"])[0] or 0)
            return self.reply(200, CTL.state(max(0, since)))
        if u.path == "/api/pool":
            return self.reply(200, [m["name"] for m in w.MODEL_POOL])
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if not self.check_access():
            return
        # 防止别的网站借你的登录状态偷偷发请求：必须带这个自定义头（跨站请求带不上）
        if self.headers.get("X-Werewolf") != "1":
            return self.reply(403, {"error": "missing header"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(n, 10000)) or b"{}")
        except Exception:
            return self.reply(400, {"error": "bad json"})
        path = urlsplit(self.path).path
        if path == "/api/new":
            names = [str(x) for x in data.get("players", [])][:5]
            err = CTL.start(names)
        elif path == "/api/answer":
            err = CTL.give_answer(data)
        elif path == "/api/stop":
            CTL.stop()
            err = None
        else:
            return self.reply(404, {"error": "not found"})
        self.reply(400 if err else 200, {"error": err} if err else {"ok": True})


if __name__ == "__main__":
    print(f"狼人杀网页版：http://{HOST}:{PORT}  （公网：https://{PUBLIC_HOST}）", flush=True)
    if not ACCESS.audiences():
        print("提示：还没有 access_aud.txt，公网请求会全部被拒绝（403）。", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
