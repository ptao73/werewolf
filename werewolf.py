#!/usr/bin/env python3
"""狼人杀（终端版）：你 + 5 个 AI 模型，共 6 人。AI 从模型池里随机抽取，也可以指定。

运行：      python3 werewolf.py
指定模型：  python3 werewolf.py --players Claude-Opus,Gemini-Argon   （不够 5 个的随机补齐）
看模型池：  python3 werewolf.py --list
测试：      python3 werewolf.py --fake --auto   （AI 随机行动、你的座位也自动，不花额度）
"""
import json, random, re, subprocess, sys, tempfile, time, argparse, termios, unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

# ---------- 模型池 ----------
# name 是游戏里显示的玩家名；kind 决定用哪个命令行工具；model 是传给该工具的模型名
# 想加减模型，直接改这个列表即可（模型名可用 `agy models`、`claude --help` 查）
MODEL_POOL = [
    {"name": "Claude-Opus",     "kind": "claude", "model": "opus"},
    {"name": "Claude-Sonnet",   "kind": "claude", "model": "sonnet"},
    {"name": "Claude-Haiku",    "kind": "claude", "model": "haiku"},
    {"name": "GPT-6.2-Sol",     "kind": "codex",  "model": "gpt-6.2-sol"},
    {"name": "GPT-6.1-Terra",   "kind": "codex",  "model": "gpt-6.1-terra"},
    {"name": "GPT-5.6-Luna",    "kind": "codex",  "model": "gpt-5.6-luna"},
    {"name": "Gemini-Argon",    "kind": "agy",    "model": "Gemini 4 Argon (High)"},
    {"name": "Gemini-Neon",     "kind": "agy",    "model": "Gemini 4 Neon (Medium)"},
    {"name": "Gemini-Hydrogen", "kind": "agy",    "model": "Gemini 4 Hydrogen (Medium)"},
    {"name": "Gemini-Flash",    "kind": "agy",    "model": "Gemini 3.8 Flash (Medium)"},
    {"name": "GPT-OSS",         "kind": "agy",    "model": "GPT-OSS 120B (Medium)"},
]
HUMAN = {"name": "你", "kind": "human"}
ROLES = ["狼人", "狼人", "预言家", "女巫", "村民", "村民"]

RULES = """你正在参加一局 6 人狼人杀（中文进行）。
角色：2 狼人、1 预言家、1 女巫、2 村民。
- 夜晚：狼人共同杀一人；女巫有一瓶解药（救当晚被杀者，整局一次）和一瓶毒药（毒一人，整局一次），同一晚不能两瓶都用；预言家查验一人是好人还是狼人。
- 白天：公布夜里死亡的人，存活者依次发言，然后同时投票放逐一人（平票则无人出局）。
- 胜负：狼人全部出局 → 好人赢；狼人数 ≥ 好人数 → 狼人赢。
这是游戏，狼人可以撒谎伪装。发言要简短自然（不超过 80 字），像真人玩家一样推理。"""

AI_TIMEOUT = 240  # 每次调用 AI 的最长等待秒数


class Quit(Exception):
    """玩家选择退出游戏"""


class NotEnoughModels(Exception):
    """可用模型不够开一局"""


class Game:
    def __init__(self, entries, spare, fake=False, auto=False):
        self.fake, self.auto = fake, auto
        self.spare = spare             # 没被抽中的模型，中途有人掉线时用来替补
        seats = entries[:]
        random.shuffle(seats)          # 随机座位
        roles = ROLES[:]
        random.shuffle(roles)          # 随机发身份
        self.players = []
        for i, (p, r) in enumerate(zip(seats, roles), start=1):
            self.players.append({**p, "seat": i, "role": r, "alive": True, "notes": []})
        self.public = []               # 所有人都能看到的公开记录
        self.antidote = self.poison = True
        self.day = 0
        self.night_now = False
        self.deferred = []          # 夜里发生、要等天亮才公布的消息
        Path(__file__).with_name("logs").mkdir(exist_ok=True)
        self.logfile = Path(__file__).with_name("logs") / f"game-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.workdir = tempfile.mkdtemp(prefix="werewolf-")  # AI 在空目录里运行，碰不到你的文件

    # ---------- 小工具 ----------
    def label(self, p):
        return f"{p['seat']}号 {p['name']}"

    def alive(self):
        return [p for p in self.players if p["alive"]]

    def human(self):
        return next((p for p in self.players if p["kind"] == "human"), None)

    def is_human(self, p):
        return p["kind"] == "human"

    def log(self, text):
        with open(self.logfile, "a", errors="replace") as f:   # 万一有怪字符也不崩溃
            f.write(text + "\n")

    def emit(self, text, kind="info"):
        """所有给你看的文字都从这里出去。kind: public 公开 / private 私密 / info 提示 / thinking 思考中"""
        print(text, flush=True)

    def say(self, text):
        """公开信息：打印到屏幕 + 写进公开记录"""
        self.emit(text, "public")
        self.public.append(text)
        self.log(text)

    def tell(self, p, text):
        """私密信息：只有该玩家知道（如果是你，就打印出来）"""
        p["notes"].append(text)
        self.log(f"[私密→{self.label(p)}] {text}")
        if self.is_human(p):
            self.emit(f"  🔒 {text}", "private")

    # ---------- 询问玩家 ----------
    def ask(self, p, task, choices=None, allow_none=False, speech=False):
        """让玩家做决定。返回 (发言文字, 选择的座位号或 0)"""
        valid = [c["seat"] for c in (choices or [])]
        if self.is_human(p):
            return self.ask_human(task, choices, allow_none, speech)
        if self.fake:  # 测试模式：随机行动
            t = random.choice(valid + ([0] if allow_none else [])) if valid else 0
            return (f"（测试发言）我是{p['seat']}号。" if speech else ""), t
        return self.ask_ai(p, task, valid, allow_none, speech)

    def ask_human(self, task, choices, allow_none, speech):
        restore_tty()
        print(f"\n👉 {task}")
        text = ""
        if speech:
            text = clean_input(input("你的发言（直接回车=过）：")).strip() or "过。"
        if not choices:
            return text, 0
        print("   可选：" + "  ".join(self.label(c) for c in choices) + ("  0=不选" if allow_none else ""))
        valid = [c["seat"] for c in choices] + ([0] if allow_none else [])
        while True:
            # 接受 "3"、"3号"、全角 "３" 等写法：只取里面的数字
            s = unicodedata.normalize("NFKC", clean_input(input("输入座位号：")))
            digits = re.findall(r"\d+", s)
            if len(digits) == 1 and int(digits[0]) in valid:
                return text, int(digits[0])
            print("   无效，请重新输入。")

    def ask_ai(self, p, task, valid, allow_none, speech):
        prompt = (
            f"{RULES}\n\n你是 {self.label(p)}，身份：{p['role']}。\n"
            f"存活玩家：{'、'.join(self.label(a) for a in self.alive())}\n\n"
            "【公开记录】\n" + ("\n".join(self.public) or "（无）") + "\n\n"
            "【只有你知道的信息】\n" + ("\n".join(p["notes"]) or "（无）") + "\n\n"
            f"【现在轮到你】{task}\n"
        )
        fmt = {"speech": "你的发言" if speech else "", "target": "座位号数字" if valid else 0}
        if valid:
            prompt += f"target 只能是 {valid}" + ("，或 0 表示不选" if allow_none else "") + "。\n"
        prompt += f"不要运行任何命令或工具，只用一行 JSON 回复，格式：{json.dumps(fmt, ensure_ascii=False)}"

        for attempt in range(2):
            raw = self.call_ai(p, prompt)
            data = self.parse_json(raw)
            ok_t = (not valid) or (data and str(data.get("target")).isdigit()
                                   and (int(data["target"]) in valid or (allow_none and int(data["target"]) == 0)))
            if data is not None and ok_t:
                t = int(data.get("target") or 0) if valid else 0
                return str(data.get("speech", "")).strip(), t
            self.log(f"[无效回复 {self.label(p)}] {raw[:300]}")
            prompt += "\n（上次回复格式无效，请严格按 JSON 格式重新回复。）"
        # 两次都无效：不偷偷替它选，停下来问你
        # 不显示回复内容：里面可能有身份或夜间行动（如女巫说要救谁），只写进日志，赛后可查
        return self.on_failure(p, "两次回复格式都无效（内容为保密不显示，已写入日志）",
                               lambda: self.ask_ai(p, task, valid, allow_none, speech))

    def call_ai(self, p, prompt):
        """调用对应的命令行工具，返回它的文字回复；失败就停下来问你"""
        while True:
            ok, out = run_cli(p, prompt, AI_TIMEOUT, self.workdir)
            if ok:
                return out
            self.on_failure(p, out, None)   # 你选 r 会重试；选 s 后 p 已换成新模型，再循环一次

    def on_failure(self, p, err, retry):
        who = "夜间某位玩家（保密）" if self.night_now else self.label(p)
        self.emit(f"\n⚠️  {who} 调用失败：{err}", "warn")
        self.log(f"[失败 {self.label(p)}] {err}")
        while True:
            c = self.ask_failure()
            if c == "q":
                raise Quit()
            if c == "s" and not self.substitute(p):
                continue
            if c in ("r", "s"):
                return retry() if retry else None

    def ask_failure(self):
        """AI 调用失败时问你怎么办，返回 r / s / q"""
        restore_tty()
        return input("   r=重试  s=换一个模型接替  q=退出游戏：").strip().lower()

    def substitute(self, p):
        """从备用模型里找一个可用的，接替 p 的座位（身份和记忆都保留）"""
        random.shuffle(self.spare)
        while self.spare:
            cand = self.spare.pop()
            self.emit(f"   检查 {cand['name']} 是否可用…", "info")
            ok, err = run_cli(cand, PING, 90, self.workdir)
            if not ok:
                self.emit(f"   ⚠️  {cand['name']} 不可用：{err[-150:]}", "warn")
                continue
            old = self.label(p)
            p.update(name=cand["name"], kind=cand["kind"], model=cand["model"])
            msg = f"📢 {old} 掉线，由 {self.label(p)} 接替（身份不变）"
            if self.night_now:
                self.deferred.append(msg)   # 夜里换人先不公布，天亮再说，避免暴露谁在夜里行动
                self.emit("   已换人接替，天亮后公布。", "info")
            else:
                self.say(msg)
            return True
        self.emit("   没有可用的备用模型了。", "warn")
        return False

    @staticmethod
    def parse_json(raw):
        """从回复里找出最后一个 {...} 并解析"""
        for m in reversed(re.findall(r"\{.*?\}", raw or "", re.S)):
            try:
                return json.loads(m)
            except json.JSONDecodeError:
                continue
        return None

    # ---------- 游戏流程 ----------
    def winner(self):
        wolves = sum(1 for p in self.alive() if p["role"] == "狼人")
        good = len(self.alive()) - wolves
        if wolves == 0:
            return "好人"
        if wolves >= good:
            return "狼人"
        return None

    def thinking(self, p):
        # 夜里不显示谁在思考：否则大家就知道谁是狼人/预言家/女巫了
        if not self.is_human(p) and not self.night_now:
            self.emit(f"   …{self.label(p)} 思考中", "thinking")

    def night(self):
        self.day += 1
        self.say(f"\n🌙 ===== 第 {self.day} 夜 =====  天黑请闭眼")
        self.night_now = True
        self.emit("   🌙 夜间行动中…（不显示是谁，以免暴露身份）", "thinking")
        alive = self.alive()

        # 1) 狼人：按座位依次提议，后面的狼看得到前面的提议，最后一只狼拍板（你是狼的话你最后决定）
        wolves = [p for p in alive if p["role"] == "狼人"]
        wolves.sort(key=lambda p: self.is_human(p))
        kill = 0
        for w in wolves:
            self.thinking(w)
            _, kill = self.ask(w, "你是狼人。选择今晚要杀的人（可以和队友商量的结果一致）。", alive)
            for mate in wolves:
                if mate is not w:
                    self.tell(mate, f"第{self.day}夜：狼队友 {self.label(w)} 提议杀 {kill}号")
        victim = self.by_seat(kill)
        for w in wolves:
            self.tell(w, f"第{self.day}夜：狼队最终决定杀 {self.label(victim)}")

        # 2) 女巫
        dead = [victim]
        witch = next((p for p in alive if p["role"] == "女巫"), None)
        if witch:
            saved = False
            if self.antidote:
                self.tell(witch, f"第{self.day}夜：今晚被狼人杀的是 {self.label(victim)}")
                self.thinking(witch)
                # 救就填被杀者的座位号，不救填 0（以前用 1=救 容易被 AI 误解成座位号）
                _, s = self.ask(witch, f"你是女巫。要用解药救 {self.label(victim)} 吗？救就填 {victim['seat']}，不救填 0。",
                                [victim], allow_none=True)
                if s == victim["seat"]:
                    self.antidote, saved = False, True
                    dead = []
                    self.tell(witch, f"第{self.day}夜：你用解药救了 {self.label(victim)}")
            if self.poison and not saved:
                self.thinking(witch)
                _, t = self.ask(witch, "你是女巫。要用毒药毒谁？不用就填 0。",
                                [p for p in alive if p is not witch], allow_none=True)
                if t:
                    self.poison = False
                    dead.append(self.by_seat(t))
                    self.tell(witch, f"第{self.day}夜：你毒了 {t}号")

        # 3) 预言家
        seer = next((p for p in alive if p["role"] == "预言家"), None)
        if seer:
            self.thinking(seer)
            _, t = self.ask(seer, "你是预言家。选择今晚要查验的人。", [p for p in alive if p is not seer])
            q = self.by_seat(t)
            self.tell(seer, f"第{self.day}夜查验：{self.label(q)} 是{'狼人' if q['role'] == '狼人' else '好人'}")

        # 天亮公布
        dead = list({p["seat"]: p for p in dead}.values())
        for p in dead:
            p["alive"] = False
        self.night_now = False
        self.say(f"\n☀️  ===== 第 {self.day} 天 =====  天亮了")
        for msg in self.deferred:
            self.say(msg)
        self.deferred = []
        self.say("昨晚死亡：" + ("、".join(self.label(p) for p in dead) if dead else "平安夜，没有人死亡"))
        return dead

    def by_seat(self, s):
        return next(p for p in self.players if p["seat"] == s)

    def last_words(self, p):
        self.thinking(p)
        text, _ = self.ask(p, "你出局了，请说遗言。", speech=True)
        self.say(f"💬 {self.label(p)}（遗言）：{text}")

    def day_phase(self, night_dead):
        if self.day == 1:
            for p in night_dead:
                self.last_words(p)
        # 发言：从随机的存活玩家开始，按座位顺序
        alive = self.alive()
        k = random.randrange(len(alive))
        for p in alive[k:] + alive[:k]:
            self.thinking(p)
            text, _ = self.ask(p, "白天发言阶段，请发言。", speech=True)
            self.say(f"💬 {self.label(p)}：{text}")
        # 投票：大家都看不到别人的票，最后一起公布
        votes = {}
        for p in alive:
            self.thinking(p)
            _, t = self.ask(p, "投票放逐一人（看不到别人的票）。0=弃票。",
                            [a for a in alive if a is not p], allow_none=True)
            votes[p["seat"]] = t
        self.say("🗳  投票：" + "  ".join(f"{s}→{t or '弃'}" for s, t in votes.items()))
        count = {}
        for t in votes.values():
            if t:
                count[t] = count.get(t, 0) + 1
        if not count:
            self.say("全部弃票，无人出局。")
            return
        top = max(count.values())
        leaders = [s for s, c in count.items() if c == top]
        if len(leaders) > 1:
            self.say(f"平票（{'、'.join(map(str, leaders))}号），无人出局。")
            return
        out = self.by_seat(leaders[0])
        out["alive"] = False
        self.say(f"被放逐：{self.label(out)}")
        self.last_words(out)

    def run(self):
        self.emit("🐺 狼人杀开始！座位：\n" + "\n".join(f"   {self.label(p)}" for p in self.players), "public")
        self.public.append("座位：" + "、".join(self.label(p) for p in self.players))
        self.log("身份（秘密）：" + "、".join(f"{self.label(p)}={p['role']}" for p in self.players))
        me = self.human()
        if me:
            self.tell(me, f"你的身份是：{me['role']}")
        for w in (p for p in self.players if p["role"] == "狼人"):
            mates = [self.label(m) for m in self.players if m["role"] == "狼人" and m is not w]
            self.tell(w, f"你的狼队友是：{'、'.join(mates)}")
        try:
            while True:
                dead = self.night()
                if self.winner():
                    break
                self.day_phase(dead)
                if self.winner():
                    break
            self.say(f"\n🏁 游戏结束：{self.winner()}阵营获胜！")
        except Quit:
            self.say("\n游戏已退出。")
        except (KeyboardInterrupt, EOFError):
            self.say("\n游戏被中断。")
        self.emit("身份揭晓：" + "、".join(f"{self.label(p)}={p['role']}" for p in self.players), "reveal")
        self.emit(f"完整记录（含所有私密信息）：{self.logfile}", "info")


# 开局时的终端设置（防止 AI 工具把终端改成"按回车没反应"的状态）
TTY_SAVED = termios.tcgetattr(0) if sys.stdin.isatty() else None


IUTF8 = getattr(termios, "IUTF8", 0o040000)  # 部分 Python 版本没有这个常量，Linux 上的值固定是 0o040000


def restore_tty():
    if TTY_SAVED is not None:
        attrs = [list(a) if isinstance(a, list) else a for a in TTY_SAVED]
        attrs[0] |= IUTF8   # 打开 UTF-8 编辑：退格一次删掉整个汉字，而不是半个
        termios.tcsetattr(0, termios.TCSADRAIN, attrs)


def clean_input(text):
    """去掉输入里残缺的字节（例如退格只删了半个汉字），避免程序崩溃"""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "ignore")


PING = '只回复 JSON：{"ok": 1}。不要运行任何命令。'


def run_cli(p, prompt, timeout, workdir):
    """真正调用命令行工具。返回 (是否成功, 回复文字或错误信息)"""
    try:
        if p["kind"] == "claude":
            cmd = ["claude", "-p", "--model", p["model"], "--tools", ""]
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=workdir,
                               start_new_session=True)
            out = r.stdout
        elif p["kind"] == "codex":
            # 每次用不同的输出文件，避免同时检查多个模型时互相覆盖
            outf = Path(tempfile.mkstemp(dir=workdir, suffix=".txt")[1])
            cmd = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                   "-m", p["model"], "-C", workdir, "-o", str(outf), "-"]
            r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                               start_new_session=True)
            out = outf.read_text()
            outf.unlink(missing_ok=True)
        else:  # agy
            cmd = ["agy", "-p", prompt, "--model", p["model"], "--print-timeout", "0"]
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout,
                               cwd=workdir, start_new_session=True)
            out = r.stdout
        if r.returncode == 0 and out.strip():
            return True, out
        err = (r.stderr or "").strip() or (r.stdout or "").strip()
        return False, f"退出码 {r.returncode}；错误输出：{err[-300:]}"
    except subprocess.TimeoutExpired:
        return False, f"超过 {timeout} 秒没回复"


def find_models(names):
    """把 --players 里写的名字（不分大小写）对应到模型池"""
    by_name = {m["name"].lower(): m for m in MODEL_POOL}
    chosen = []
    for n in names:
        if n.lower() not in by_name:
            sys.exit(f"模型池里没有 {n}。用 --list 查看可用名字。")
        chosen.append(by_name[n.lower()])
    return chosen


def pick_players(wanted, need, fake, out=print):
    """先用你指定的，再随机补齐；每个都先测一下能不能用，不能用的提示并跳过"""
    workdir = tempfile.mkdtemp(prefix="werewolf-check-")
    queue = wanted[:]                                   # 指定的排在前面
    rest = [m for m in MODEL_POOL if m not in wanted]
    random.shuffle(rest)
    queue += rest
    ok_list = []
    while len(ok_list) < need and queue:
        batch, queue = queue[:need - len(ok_list)], queue[need - len(ok_list):]
        if fake:                                        # 测试模式不真的调用
            ok_list += batch
            continue
        out("检查模型是否可用：" + "、".join(m["name"] for m in batch))
        with ThreadPoolExecutor(len(batch)) as ex:      # 同时检查，节省时间
            results = list(ex.map(lambda m: run_cli(m, PING, 90, workdir), batch))
        for m, (ok, err) in zip(batch, results):
            if ok:
                ok_list.append(m)
            else:
                first = next((l for l in err.splitlines() if "ERROR" in l or "error" in l), err)
                out(f"⚠️  {m['name']} 不可用，本局不参加：{first[-200:]}")
    if len(ok_list) < need:
        raise NotEnoughModels(f"可用模型只有 {len(ok_list)} 个，不够 {need} 个，游戏无法开始。")
    return ok_list, queue                               # queue 里剩下的是没测过的备用模型


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", default="", help="指定参加的模型，逗号分隔，不够的随机补齐")
    ap.add_argument("--list", action="store_true", help="列出模型池")
    ap.add_argument("--fake", action="store_true", help="AI 随机行动（不花额度，测试用）")
    ap.add_argument("--auto", action="store_true", help="你的座位也由 AI 代打（测试用）")
    a = ap.parse_args()
    if a.list:
        for m in MODEL_POOL:
            print(f"  {m['name']:<16} ({m['kind']}: {m['model']})")
        sys.exit()
    need = 6 if a.auto else 5
    wanted = find_models([n.strip() for n in a.players.split(",") if n.strip()])
    if len(wanted) > need:
        sys.exit(f"最多指定 {need} 个模型。")
    while True:
        # 每局都重新抽取、重新检查模型（额度可能刚恢复或刚用完）
        try:
            chosen, spare = pick_players(wanted, need, a.fake)
        except NotEnoughModels as e:
            sys.exit(str(e))
        entries = chosen if a.auto else chosen + [HUMAN]
        Game(entries, spare, fake=a.fake, auto=a.auto).run()
        if a.auto:          # 全自动测试模式：只玩一局
            break
        restore_tty()
        try:
            while (c := clean_input(input("\n🔁 n=下一局  q=结束：")).strip().lower()) not in ("n", "q"):
                print("   请输入 n 或 q。")
        except (KeyboardInterrupt, EOFError):
            c = "q"
        if c == "q":
            print("👋 再见！")
            break
        print("\n" + "=" * 40 + "\n")
