#!/usr/bin/env python3
"""狼人杀（终端版）：你 + Claude + Codex + agy + 2 个 Haiku 机器人，共 6 人。

运行：  python3 werewolf.py
测试：  python3 werewolf.py --fake --auto   （AI 随机行动、你的座位也自动，用来快速检查规则）
"""
import json, random, re, subprocess, sys, tempfile, time, argparse
from datetime import datetime
from pathlib import Path

# ---------- 玩家与角色配置 ----------
# kind 决定用哪个命令行工具来"思考"；model 是该工具使用的模型
PLAYERS = [
    {"name": "你",      "kind": "human"},
    {"name": "Claude",  "kind": "claude", "model": "sonnet"},
    {"name": "Codex",   "kind": "codex"},
    {"name": "agy",     "kind": "agy",    "model": "Gemini 4 Argon (High)"},
    {"name": "Haiku-1", "kind": "claude", "model": "haiku"},
    {"name": "Haiku-2", "kind": "claude", "model": "haiku"},
]
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


class Game:
    def __init__(self, fake=False, auto=False):
        self.fake, self.auto = fake, auto
        seats = PLAYERS[:]
        random.shuffle(seats)          # 随机座位
        roles = ROLES[:]
        random.shuffle(roles)          # 随机发身份
        self.players = []
        for i, (p, r) in enumerate(zip(seats, roles), start=1):
            self.players.append({**p, "seat": i, "role": r, "alive": True, "notes": []})
        if auto and not fake:  # 真实测试：你的座位由 Haiku 代打
            self.human().update(kind="claude", model="haiku")
        self.public = []               # 所有人都能看到的公开记录
        self.antidote = self.poison = True
        self.day = 0
        Path(__file__).with_name("logs").mkdir(exist_ok=True)
        self.logfile = Path(__file__).with_name("logs") / f"game-{datetime.now():%Y%m%d-%H%M%S}.log"
        self.workdir = tempfile.mkdtemp(prefix="werewolf-")  # AI 在空目录里运行，碰不到你的文件

    # ---------- 小工具 ----------
    def label(self, p):
        return f"{p['seat']}号 {p['name']}"

    def alive(self):
        return [p for p in self.players if p["alive"]]

    def human(self):
        return next(p for p in self.players if p["name"] == "你")

    def is_human(self, p):
        return p["kind"] == "human" and not self.auto

    def log(self, text):
        with open(self.logfile, "a") as f:
            f.write(text + "\n")

    def say(self, text):
        """公开信息：打印到屏幕 + 写进公开记录"""
        print(text)
        self.public.append(text)
        self.log(text)

    def tell(self, p, text):
        """私密信息：只有该玩家知道（如果是你，就打印出来）"""
        p["notes"].append(text)
        self.log(f"[私密→{self.label(p)}] {text}")
        if self.is_human(p):
            print(f"  🔒 {text}")

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
        print(f"\n👉 {task}")
        text = ""
        if speech:
            text = input("你的发言（直接回车=过）：").strip() or "过。"
        if not choices:
            return text, 0
        print("   可选：" + "  ".join(self.label(c) for c in choices) + ("  0=不选" if allow_none else ""))
        valid = [c["seat"] for c in choices] + ([0] if allow_none else [])
        while True:
            s = input("输入座位号：").strip()
            if s.isdigit() and int(s) in valid:
                return text, int(s)
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
        return self.on_failure(p, f"两次回复都无效：{raw[:200]}", lambda: self.ask_ai(p, task, valid, allow_none, speech))

    def call_ai(self, p, prompt):
        """调用对应的命令行工具，返回它的文字回复"""
        while True:
            try:
                if p["kind"] == "claude":
                    cmd = ["claude", "-p", "--model", p["model"], "--tools", ""]
                    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                       timeout=AI_TIMEOUT, cwd=self.workdir)
                    out = r.stdout
                elif p["kind"] == "codex":
                    outf = Path(self.workdir) / "codex-last.txt"
                    outf.unlink(missing_ok=True)
                    cmd = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
                           "-C", self.workdir, "-o", str(outf), "-"]
                    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=AI_TIMEOUT)
                    out = outf.read_text() if outf.exists() else ""
                else:  # agy
                    cmd = ["agy", "-p", prompt, "--model", p["model"], "--print-timeout", "0"]
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=AI_TIMEOUT, cwd=self.workdir)
                    out = r.stdout
                if r.returncode == 0 and out.strip():
                    return out
                err = f"退出码 {r.returncode}；错误输出：{(r.stderr or '').strip()[-300:]}"
            except subprocess.TimeoutExpired:
                err = f"超过 {AI_TIMEOUT} 秒没回复"
            # 调用失败：报告原始错误，让你决定重试还是退出
            self.on_failure(p, err, None)

    def on_failure(self, p, err, retry):
        print(f"\n⚠️  {self.label(p)} 调用失败：{err}")
        self.log(f"[失败 {self.label(p)}] {err}")
        while True:
            c = input("   r=重试  q=退出游戏：").strip().lower()
            if c == "q":
                raise Quit()
            if c == "r":
                return retry() if retry else None

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
        if not self.is_human(p):
            print(f"   …{self.label(p)} 思考中", flush=True)

    def night(self):
        self.day += 1
        self.say(f"\n🌙 ===== 第 {self.day} 夜 =====  天黑请闭眼")
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
                _, s = self.ask(witch, f"你是女巫。要用解药救 {self.label(victim)} 吗？target 填 1=救，0=不救。",
                                [{"seat": 1, "name": "救", "role": ""}], allow_none=True)
                if s == 1:
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
        self.say(f"\n☀️  ===== 第 {self.day} 天 =====  天亮了")
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
        print("🐺 狼人杀开始！座位：")
        for p in self.players:
            print(f"   {self.label(p)}")
        self.public.append("座位：" + "、".join(self.label(p) for p in self.players))
        self.log("身份（秘密）：" + "、".join(f"{self.label(p)}={p['role']}" for p in self.players))
        me = self.human()
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
        print("身份揭晓：" + "、".join(f"{self.label(p)}={p['role']}" for p in self.players))
        print(f"完整记录（含所有私密信息）：{self.logfile}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="AI 随机行动（不花额度，测试用）")
    ap.add_argument("--auto", action="store_true", help="你的座位也自动行动（测试用；不加 --fake 时由 Haiku 代打）")
    a = ap.parse_args()
    Game(fake=a.fake, auto=a.auto).run()
