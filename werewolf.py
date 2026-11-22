#!/usr/bin/env python3
"""狼人杀（终端版）：你 + 5 个 AI 模型，共 6 人。AI 从模型池里随机抽取，也可以指定。

运行：      python3 werewolf.py
指定模型：  python3 werewolf.py --players Claude-Opus,Gemini-Argon   （不够 5 个的随机补齐）
看模型池：  python3 werewolf.py --list
测试：      python3 werewolf.py --fake --auto   （AI 随机行动、你的座位也自动，不花额度）
"""
import json, random, re, subprocess, sys, tempfile, threading, time, argparse, termios, unicodedata
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

# ---------- 各人数的角色配置 ----------
ROLESETS = {
    6:  ["狼人"] * 2 + ["预言家", "女巫"] + ["村民"] * 2,
    8:  ["狼人"] * 3 + ["预言家", "女巫", "猎人"] + ["村民"] * 2,
    9:  ["狼人"] * 3 + ["预言家", "女巫", "猎人"] + ["村民"] * 3,
    12: ["狼人"] * 4 + ["预言家", "女巫", "猎人", "守卫"] + ["村民"] * 4,
}


def rules_text(n):
    """根据人数生成给 AI 看的规则说明"""
    roles = ROLESETS[n]
    summary = "、".join(f"{roles.count(r)} {r}" for r in dict.fromkeys(roles))
    lines = [f"你正在参加一局 {n} 人狼人杀（中文进行）。", f"角色：{summary}。",
             "- 夜晚：狼人共同杀一人；女巫有一瓶解药（救当晚被杀者，整局一次）和一瓶毒药（毒一人，整局一次），"
             "同一晚不能两瓶都用；预言家查验一人是好人还是狼人。"]
    if "守卫" in roles:
        lines.append("- 守卫：每晚守护一人（不能连续两晚守同一人），被守护的人不会被狼人杀死；"
                     "但如果同一人同时被守护又被女巫救，仍然死亡。守卫挡不住毒药。")
    if "猎人" in roles:
        lines.append("- 猎人：出局时可以开枪带走一人；被女巫毒死则不能开枪。")
    lines += ["- 白天：公布夜里死亡的人，存活者依次发言，然后同时投票放逐一人（平票则无人出局）。",
              "- 胜负：狼人全部出局 → 好人赢；狼人数 ≥ 好人数 → 狼人赢。",
              "这是游戏，狼人可以撒谎伪装。发言要简短自然（不超过 80 字），像真人玩家一样推理。"]
    return "\n".join(lines)


AI_TIMEOUT = 120  # 每次调用 AI 的最长等待秒数（正常 7-15 秒；超时就按失败处理：重试/换模型）
PING_TIMEOUT = 45 # 开局前检查模型是否可用的最长等待秒数


# ---------- AI 人格：每局给上场的 AI 随机生成三个属性（0-100%）----------
TRAITS = [
    # (名字, 低端描述, 中间描述, 高端描述)
    ("理性", "感性：凭直觉和第一印象判断，容易被发言的气势和情绪带动，说话带感情色彩",
             "理性和感性兼顾：既看逻辑也凭感觉",
             "理性：重逻辑，盯着票型、发言矛盾和身份信息推理，不轻易被情绪带走"),
    ("好胜", "无所谓：佛系随和，不爱争论，常常跟着大家走，输赢看得很淡",
             "好胜心中等：想赢但不会太激进",
             "极其好胜：非常想赢，发言强势，敢冒险跳身份、带节奏，咬住怀疑对象不松口"),
    ("诚实", "狡猾：善于伪装和误导，敢悍跳、编造身份和查验结果，好人也可能为了战术说假话",
             "诚实度中等：必要时会隐瞒或误导，但不轻易编造",
             "诚实：尽量不说假话；即使是狼人也倾向少说、模糊带过，而不是编造事实"),
]


PERSONA_SHAPE = 0.4   # U 形分布的强度：越小越容易出现极端值（0.4 ≈ 八成落在两端、近一半 ≤10% 或 ≥90%）


def random_persona():
    """每个属性从 U 形分布（Beta 分布）里抽：两头多、中间少，性格更鲜明"""
    return {name: round(random.betavariate(PERSONA_SHAPE, PERSONA_SHAPE) * 100) for name, *_ in TRAITS}


def persona_text(persona):
    """把百分比变成给 AI 看的性格描述"""
    lines = []
    for name, low, mid, high in TRAITS:
        v = persona[name]
        desc = low if v <= 33 else high if v >= 67 else mid
        lines.append(f"- {name} {v}%：{desc}")
    return "\n".join(lines)


def persona_rules_text():
    """规则页里"AI 性格"一节（直接用游戏实际使用的设定生成，改设定规则页会跟着变）"""
    lines = ["每局开始时，每个上场的 AI 随机得到三个性格属性（0-100%）。两头的值更常见（约八成落在偏低或偏高的一端），性格更鲜明。"]
    for name, low, _mid, high in TRAITS:
        lines.append(f"· {name}：低 = {low.split('：')[0]}，高 = {high.split('：')[0]}")
    lines += [
        "性格不只影响说话风格，也影响判断：每次要选人（投票、夜间行动、猎人开枪）时，AI 给每个座位打三个分——"
        "证据推理分、直觉印象分、跟随大家分；程序再按它的性格合成最终选择：",
        "· 理性越高越看证据，越低越凭直觉；",
        "· 好胜越低越容易跟着大家走，而且可以不选的事（投票、毒药、开枪）没有足够把握就不出手；越好胜越敢在证据不足时出手；",
        "· 诚实决定它会不会编造身份和查验结果。",
        "性格在游戏中保密（否则大家就知道哪些座位是 AI），结束后和身份一起公布。真人玩家没有性格设定。",
    ]
    return "\n".join(lines)


def persona_short(persona):
    return "·".join(f"{k} {v}%" for k, v in persona.items())


class Quit(Exception):
    """玩家选择退出游戏"""


class NotEnoughModels(Exception):
    """可用模型不够开一局"""


class Game:
    # 以下开关由网页多人版打开；终端版保持原样
    anonymous = False      # True：只显示座位号，不显示名字/模型（AI 也只知道座位号）
    parallel_votes = False # True：所有人同时投票
    auto_recover = False   # True：AI 出错时自动重试/换模型，不停下来问

    def __init__(self, entries, spare, fake=False, auto=False):
        self.fake, self.auto = fake, auto
        self.spare = spare             # 没被抽中的模型，中途有人掉线时用来替补
        self.n = len(entries)
        if self.n not in ROLESETS:
            raise ValueError(f"不支持 {self.n} 人局（只支持 {sorted(ROLESETS)} 人）")
        seats = entries[:]
        random.shuffle(seats)          # 随机座位
        roles = ROLESETS[self.n][:]
        random.shuffle(roles)          # 随机发身份
        self.players = []
        for i, (p, r) in enumerate(zip(seats, roles), start=1):
            self.players.append({**p, "seat": i, "role": r, "alive": True, "notes": [], "reasons": []})
            if p.get("kind") != "human":                 # 只给上场的 AI 生成人格；替补接手时沿用这个座位的人格
                self.players[-1]["persona"] = random_persona()
        self.rules = rules_text(self.n)
        self.public = []               # 所有人都能看到的公开记录
        self.antidote = self.poison = True
        self.last_guard = 0            # 守卫上一晚守的人
        self.day = 0
        self.night_now = False
        self.deferred = []             # 夜里发生、要等天亮才公布的消息
        self.discussion = []           # 赛后讨论记录
        self.result = ""               # 游戏结果（赛后讨论用）
        self.lock = threading.Lock()   # 同时投票时保护共享数据
        Path(__file__).with_name("logs").mkdir(exist_ok=True)
        self.logfile = Path(__file__).with_name("logs") / f"game-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
        self.notedir = self.logfile.with_suffix("")   # 每局一个文件夹，放各 AI 的思考笔记本
        self.workdir = tempfile.mkdtemp(prefix="werewolf-")  # AI 在空目录里运行，碰不到你的文件

    # ---------- 小工具 ----------
    def label(self, p):
        """游戏里显示的名字：匿名模式只有座位号"""
        return f"{p['seat']}号" if self.anonymous else f"{p['seat']}号 {p['name']}"

    def real_label(self, p):
        """真实身份（写日志、赛后揭晓用）"""
        return f"{p['seat']}号 {p['name']}"

    def alive(self):
        return [p for p in self.players if p["alive"]]

    def humans(self):
        return [p for p in self.players if p["kind"] == "human"]

    def human(self):
        h = self.humans()
        return h[0] if h else None

    def is_human(self, p):
        return p["kind"] == "human"

    def log(self, text):
        with self.lock, open(self.logfile, "a", errors="replace") as f:   # 万一有怪字符也不崩溃
            f.write(text + "\n")

    def emit(self, text, kind="info", to=None, data=None):
        """所有给玩家看的文字都从这里出去。kind: public / private / info / thinking / warn / reveal
        to=某个玩家 表示只给这个人看（终端版只有你一个人，所以直接打印）"""
        print(text, flush=True)

    def show_seats(self, title):
        """显示座位表：出局的标 ❌（网页版会把你自己标蓝、出局的标红）"""
        mark = lambda p: ("❌" if not p["alive"] else "") + self.label(p) + \
            ("（你）" if self.is_human(p) and not self.anonymous else "")
        self.emit(title + "\n   " + "  ".join(mark(p) for p in self.players), "seats",
                  data={"title": title, "seats": [{"seat": p["seat"], "alive": p["alive"]} for p in self.players]})

    def intro(self, h):
        """告诉真人玩家自己的座位和身份"""
        self.tell(h, f"你是 {h['seat']}号，身份：{h['role']}")

    def say(self, text):
        """公开信息：所有人可见 + 写进公开记录"""
        self.emit(text, "public")
        with self.lock:
            self.public.append(text)
        self.log(text)

    def tell(self, p, text):
        """私密信息：只有该玩家知道"""
        p["notes"].append(text)
        self.log(f"[私密→{self.real_label(p)}] {text}")
        if self.is_human(p):
            self.emit(f"  🔒 {text}", "private", to=p)

    # ---------- 询问玩家 ----------
    def ask(self, p, task, choices=None, allow_none=False, speech=False):
        """让玩家做决定。返回 (发言文字, 选择的座位号或 0)"""
        valid = [c["seat"] for c in (choices or [])]
        if self.is_human(p):
            return self.ask_human(p, task, choices, allow_none, speech)
        if self.fake:  # 测试模式：随机行动
            t = random.choice(valid + ([0] if allow_none else [])) if valid else 0
            return (f"（测试发言）我是{p['seat']}号。" if speech else ""), t
        return self.ask_ai(p, task, valid, allow_none, speech)

    def ask_human(self, p, task, choices, allow_none, speech):
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
        with self.lock:
            public = "\n".join(self.public)
        prompt = (
            f"{self.rules}\n\n你是 {self.label(p)}，身份：{p['role']}。\n"
            + (f"【你的性格】（请按这个性格来玩：它既决定你的发言风格，也决定你判断时看重什么——"
               f"理性高就重证据、感性高就重直觉；好胜高就敢冒险出手、好胜低就随大流、没把握就不出手；"
               f"诚实决定你会不会编造。胜负依然重要，但用你自己的方式去争取）\n"
               f"{persona_text(p['persona'])}\n" if p.get("persona") else "") +
            f"存活玩家：{'、'.join(self.label(a) for a in self.alive())}\n\n"
            "【公开记录】\n" + (public or "（无）") + "\n\n"
            "【只有你知道的信息】\n" + ("\n".join(p["notes"]) or "（无）") + "\n\n"
            "【你的思考笔记本（只有你自己能看到，是你之前每一步记下的想法，请保持思路连贯）】\n"
            + ("\n".join(p["reasons"]) or "（还没有）") + "\n\n"
            f"【现在轮到你】{task}\n"
        )
        fmt = {"speech": "你的发言" if speech else "", "target": "座位号数字" if valid else 0,
               "thinking": "你的思考笔记（只有你自己看得到，不公开）：你目前对其他玩家的判断、这样做的理由、接下来的打算，不超过150字"}
        weighted = bool(valid and p.get("persona"))
        if weighted:
            fmt["scores"] = {str(valid[0]): ["logic", "gut", "crowd"], "…": "每个可选座位一项"}
            prompt += ("给每个可选座位打三个分（0-10 的数字）：logic=按证据和推理你有多想选他；gut=凭直觉和印象有多想选他；"
                       "crowd=场上其他人有多倾向他。系统会按你的性格把这些分数合成最终决定"
                       + ("；如果这件事可以不选，分数都不够高时就会不选" if allow_none else "") + "。target 写你自己最想选的。\n")
        if valid:
            prompt += f"target 只能是 {valid}" + ("，或 0 表示不选" if allow_none else "") + "。\n"
        prompt += f"不要运行任何命令或工具，只用一行 JSON 回复，格式：{json.dumps(fmt, ensure_ascii=False)}"

        for attempt in range(2):
            raw = self.call_ai(p, prompt)
            data = self.parse_json(raw)
            ok_t = (not valid) or (data and str(data.get("target")).isdigit()
                                   and (int(data["target"]) in valid or (allow_none and int(data["target"]) == 0)))
            picked = self.personality_pick(p, data.get("scores"), valid, allow_none) \
                if (weighted and isinstance(data, dict)) else None
            if data is not None and (ok_t or picked):
                own = int(data["target"]) if ok_t and valid else 0      # AI 自己想选的
                t = picked[0] if picked else own                       # 按性格权重算出的最终选择
                thinking = str(data.get("thinking") or data.get("reason") or "").strip()[:400]
                when = f"第{self.day}{'夜' if self.night_now else '天'}"
                act = f"选了 {t}号" if t else ("不选" if valid else "发言")
                note = f"{when}｜{task}｜{act}\n  思考：{thinking or '（没写）'}"
                if picked:
                    finals, thr = picked[1], picked[2]
                    top = "、".join(f"{s_}号 {v:.1f}" for s_, v in sorted(finals.items(), key=lambda x: -x[1])[:3])
                    note += f"\n  性格加权：得分前三 {top}" + (f"；门槛 {thr:.1f}" if allow_none else "")
                    if t != own:
                        note += f"；你原本想选 {own or '不选'}{'号' if own else ''}，按性格最终 {act}"
                self.write_note(p, note)
                return str(data.get("speech", "")).strip(), t
            self.log(f"[无效回复 {self.real_label(p)}] {raw[:300]}")
            prompt += "\n（上次回复格式无效，请严格按 JSON 格式重新回复。）"
        # 两次都无效。不显示回复内容：里面可能有身份或夜间行动（如女巫说要救谁），只写进日志，赛后可查
        return self.on_failure(p, "两次回复格式都无效（内容为保密不显示，已写入日志）",
                               lambda: self.ask_ai(p, task, valid, allow_none, speech), tries=2)

    def call_ai(self, p, prompt):
        """调用对应的命令行工具，返回它的文字回复；失败就按设置处理"""
        tries = 0
        while True:
            ok, out = run_cli(p, prompt, AI_TIMEOUT, self.workdir)
            if ok:
                return out
            tries += 1
            self.on_failure(p, out, None, tries)   # 重试 / 换模型后再循环一次

    def on_failure(self, p, err, retry, tries=1):
        self.log(f"[失败 {self.real_label(p)}] {err}")
        if self.auto_recover:
            # 多人网页版：不停下来问。第一次失败先重试，再失败就换一个模型接替；都不行才结束本局
            if tries >= 2 and not self.substitute(p):
                self.say("⚠️ 有 AI 玩家出了故障且没有可用的替补模型，本局只能结束。")
                raise Quit()
            return retry() if retry else None
        who = "夜间某位玩家（保密）" if self.night_now else self.label(p)
        self.emit(f"\n⚠️  {who} 调用失败：{err}", "warn")
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
        while True:
            with self.lock:
                if not self.spare:
                    break
                random.shuffle(self.spare)
                cand = self.spare.pop()
            if not self.anonymous:
                self.emit(f"   检查 {cand['name']} 是否可用…", "info")
            ok, err = run_cli(cand, PING, PING_TIMEOUT, self.workdir)
            if not ok:
                self.log(f"[替补不可用 {cand['name']}] {err[-150:]}")
                if not self.anonymous:
                    self.emit(f"   ⚠️  {cand['name']} 不可用：{err[-150:]}", "warn")
                continue
            old = self.real_label(p)
            p.update(name=cand["name"], kind=cand["kind"], model=cand["model"])
            self.log(f"[替补] {old} → {self.real_label(p)}")
            if self.anonymous:   # 匿名模式不说是哪个座位、哪个模型
                msg = "📢 有一位 AI 玩家出了故障，已换一个模型接替（身份不变）"
            else:
                msg = f"📢 {old} 掉线，由 {self.label(p)} 接替（身份不变）"
            if self.night_now:
                self.deferred.append(msg)   # 夜里换人先不公布，天亮再说，避免暴露谁在夜里行动
                if not self.anonymous:
                    self.emit("   已换人接替，天亮后公布。", "info")
            else:
                self.say(msg)
            return True
        if not self.anonymous:
            self.emit("   没有可用的备用模型了。", "warn")
        return False

    def explain(self, p, question, asker="玩家"):
        """赛后讨论：让 AI 根据完整记录和它自己的思考笔记本，诚实解释它的想法"""
        reveal = "、".join(f"{self.real_label(x)}={x['role']}" for x in self.players)
        with self.lock:
            public = "\n".join(self.public)
        prompt = (
            f"{self.rules}\n\n游戏已经结束：{self.result}\n身份揭晓：{reveal}\n"
            f"你是 {self.real_label(p)}，身份：{p['role']}。\n"
            + (f"你这局的性格：\n{persona_text(p['persona'])}\n" if p.get("persona") else "") + "\n"
            "【公开记录】\n" + (public or "（无）") + "\n\n"
            "【你在游戏中的私密信息】\n" + ("\n".join(p["notes"]) or "（无）") + "\n\n"
            "【你的思考笔记本（游戏中每一步当时记下的想法）】\n" + ("\n".join(p["reasons"]) or "（没有记录）") + "\n\n"
            "【赛后讨论记录】\n" + ("\n".join(self.discussion) or "（无）") + "\n\n"
            f"现在是赛后讨论，{asker}问你：{question}\n"
            "游戏已结束，不需要再伪装。请根据上面的记录，尤其是你笔记本里当时的想法，诚实解释；"
            "如果记录里没有写到，就说明是现在回想推测的。用中文，不超过150字，直接回答，不要 JSON，不要运行任何命令。"
        )
        if self.fake:
            ans = f"（测试回答）我是{p['seat']}号，我的笔记本有 {len(p['reasons'])} 条。"
        else:
            ans = self.call_ai(p, prompt).strip()
        self.discussion.append(f"{asker} 问 {self.real_label(p)}：{question}\n{self.real_label(p)} 回答：{ans}")
        self.write_note(p, f"赛后讨论｜{asker} 问：{question}\n  回答：{ans}")
        self.log(f"[赛后讨论] {asker} 问 {self.real_label(p)}：{question}\n  回答：{ans}")
        return ans

    def personality_pick(self, p, scores, valid, allow_none):
        """按性格把 AI 的打分合成最终选择。返回 (座位号或0, 各座位得分, 门槛)；分数不完整返回 None
        - 理性：logic 和 gut 的比例（理性 90% = 90% 看证据、10% 看直觉）
        - 好胜：越不好胜越跟随大家（crowd 加分），而且可以不选的事需要更强的把握才出手"""
        if not isinstance(scores, dict):
            return None
        r, h = p["persona"]["理性"] / 100, p["persona"]["好胜"] / 100
        finals = {}
        for seat in valid:
            v = scores.get(str(seat), scores.get(seat))
            try:                      # 有的模型把分数写成文字 "7"，也接受
                nums = [float(x) for x in v] if isinstance(v, list) and len(v) == 3 else None
            except (TypeError, ValueError):
                nums = None
            if nums:
                logic, gut, crowd = (max(0.0, min(10.0, x)) for x in nums)
                finals[seat] = r * logic + (1 - r) * gut + (1 - h) * 0.5 * crowd
        if not finals or len(finals) < (len(valid) + 1) // 2:     # 打分太不完整，就不用
            return None
        best = max(finals.values())
        pick = random.choice([s_ for s_, v in finals.items() if abs(v - best) < 1e-9])
        thr = 7.5 - 5 * h          # 好胜 100% → 2.5 分就出手；0% → 要 7.5 分
        if allow_none and best < thr:
            return 0, finals, thr
        return pick, finals, thr

    def write_note(self, p, entry):
        """把一条思考记进该座位的笔记本（内存里一份，文件里一份）"""
        p["reasons"].append(entry)
        self.log(f"[笔记 {self.real_label(p)}] {entry}")
        with self.lock:
            self.notedir.mkdir(parents=True, exist_ok=True)
            f = self.notedir / f"{p['seat']:02d}号.md"
            if not f.exists():
                f.write_text(f"# {p['seat']}号 的思考笔记本（{p['role']}）\n\n")
            with open(f, "a", errors="replace") as fh:
                fh.write(f"- [{self.real_label(p)}] {entry}\n")

    def ai_seats(self):
        return [p for p in self.players if not self.is_human(p)]

    def discuss_terminal(self):
        """终端版赛后讨论：输入 "座位号 问题" 或 "all 问题"，直接回车结束"""
        print("\n💬 赛后讨论：输入「座位号 问题」问某位 AI（例：5 你为什么毒3号？），"
              "「all 问题」问所有 AI，直接回车结束讨论。", flush=True)
        while True:
            restore_tty()
            try:
                line = clean_input(input("提问：")).strip()
            except (KeyboardInterrupt, EOFError):
                return
            if not line:
                return
            line = unicodedata.normalize("NFKC", line)
            m = re.match(r"^(all|全部|所有)\s*(.+)$", line, re.I) or re.match(r"^(\d+)\s*号?\s*(.+)$", line)
            if not m:
                print("   格式：座位号 问题（例：5 你为什么毒3号？）", flush=True)
                continue
            who, q = m.group(1), m.group(2).strip()
            targets = self.ai_seats() if not who.isdigit() else [x for x in self.ai_seats() if x["seat"] == int(who)]
            if not targets:
                print("   这个座位不是 AI（或不存在）。", flush=True)
                continue
            for t in targets:
                print(f"   …{self.real_label(t)} 思考中", flush=True)
                try:
                    ans = self.explain(t, q, "你")
                except Quit:
                    print("   已取消。", flush=True)
                    break
                print(f"🤖 {self.real_label(t)}：{ans}", flush=True)

    @staticmethod
    def parse_json(raw):
        """从回复里找出最后一个 {...} 并解析"""
        raw = raw or ""
        dec = json.JSONDecoder()
        found = None
        for i, ch in enumerate(raw):            # 从每个 "{" 尝试解析出完整的 JSON（支持嵌套）
            if ch != "{":
                continue
            try:
                obj, _ = dec.raw_decode(raw[i:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and (found is None or not (set(found) & {"speech", "target", "scores", "ok"})):
                found = obj
                if set(obj) & {"speech", "target", "scores", "ok"}:
                    break
        return found

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
        if self.night_now:
            return
        # 匿名模式下真人和 AI 显示一样的"思考中"，大家分不出谁是真人
        if self.anonymous or not self.is_human(p):
            self.emit(f"   …{self.label(p)} 思考中", "thinking")

    def by_seat(self, s):
        return next(p for p in self.players if p["seat"] == s)

    def role_of(self, role):
        return next((p for p in self.alive() if p["role"] == role), None)

    def night(self):
        self.day += 1
        self.say(f"\n🌙 ===== 第 {self.day} 夜 =====  天黑请闭眼")
        self.night_now = True
        self.emit("   🌙 夜间行动中…（不显示是谁，以免暴露身份）", "thinking")
        alive = self.alive()

        # 守卫、狼人、预言家互不依赖 → 同时行动（省时间）；女巫要知道刀口，所以最后单独行动
        # 终端版里如果你自己有夜间行动，就按顺序来（在后台线程里等你输入时按 Ctrl+C 会卡住）
        human_acts = any(self.is_human(p) and p["role"] in ("守卫", "狼人", "预言家") for p in alive)
        if self.parallel_votes or not human_acts:
            with ThreadPoolExecutor(3) as ex:
                f_guard = ex.submit(self.night_guard, alive)
                f_wolves = ex.submit(self.night_wolves, alive)
                f_seer = ex.submit(self.night_seer, alive)
                guarded, victim = f_guard.result(), f_wolves.result()
                f_seer.result()
        else:
            guarded, victim = self.night_guard(alive), self.night_wolves(alive)
            self.night_seer(alive)

        # 2) 女巫
        saved, poisoned = False, None
        witch = self.role_of("女巫")
        if witch:
            if self.antidote:
                self.tell(witch, f"第{self.day}夜：今晚被狼人杀的是 {self.label(victim)}")
                # 救就填被杀者的座位号，不救填 0（以前用 1=救 容易被 AI 误解成座位号）
                _, s = self.ask(witch, f"你是女巫。要用解药救 {self.label(victim)} 吗？救就填 {victim['seat']}，不救填 0。",
                                [victim], allow_none=True)
                if s == victim["seat"]:
                    self.antidote, saved = False, True
                    self.tell(witch, f"第{self.day}夜：你用解药救了 {self.label(victim)}")
            if self.poison and not saved:
                _, t = self.ask(witch, "你是女巫。要用毒药毒谁？不用就填 0。",
                                [p for p in alive if p is not witch], allow_none=True)
                if t:
                    self.poison = False
                    poisoned = self.by_seat(t)
                    self.tell(witch, f"第{self.day}夜：你毒了 {t}号")

        # 结算：被守护或被救（只有其一）就活下来；同守同救仍然死亡；毒药挡不住
        dead = {}   # 座位号 → (玩家, 死因)
        if saved == (guarded == victim["seat"]):      # 既没守也没救，或者同守同救 → 死
            dead[victim["seat"]] = (victim, "kill")
        if poisoned:                                   # 被毒的一定死；又被杀又被毒按"毒死"算（猎人不能开枪）
            dead[poisoned["seat"]] = (poisoned, "poison")
        dead = list(dead.values())
        for p, _ in dead:
            p["alive"] = False

        self.night_now = False
        self.say(f"\n☀️  ===== 第 {self.day} 天 =====  天亮了")
        for msg in self.deferred:
            self.say(msg)
        self.deferred = []
        self.say("昨晚死亡：" + ("、".join(self.label(p) for p, _ in dead) if dead else "平安夜，没有人死亡"))
        return dead

    def night_guard(self, alive):
        """守卫：守护一人，不能连续两晚守同一人。返回守护的座位号（0=不守）"""
        guard = self.role_of("守卫")
        if not guard:
            return 0
        opts = [p for p in alive if p["seat"] != self.last_guard]
        _, guarded = self.ask(guard, "你是守卫。选择今晚要守护的人（可以守自己；不能和上一晚相同；0=不守）。",
                              opts, allow_none=True)
        self.last_guard = guarded
        self.tell(guard, f"第{self.day}夜：你守护了 {guarded}号" if guarded else f"第{self.day}夜：你没有守护任何人")
        return guarded

    def night_wolves(self, alive):
        """狼人：AI 狼同时提议；有真人狼的话，真人看到 AI 队友的提议后拍板；否则按多数（平票随机）。返回被杀的玩家"""
        wolves = [p for p in alive if p["role"] == "狼人"]
        ai = [p for p in wolves if not self.is_human(p)]
        humans = [p for p in wolves if self.is_human(p)]
        task = "你是狼人。选择今晚要杀的人（队友也在同时选，最后按多数决定）。"
        proposals = {}
        if ai:
            with ThreadPoolExecutor(len(ai)) as ex:
                for p, (_, k) in zip(ai, ex.map(lambda p: self.ask(p, task, alive), ai)):
                    proposals[p["seat"]] = k
        for p in wolves:
            for s_, k in proposals.items():
                if s_ != p["seat"]:
                    self.tell(p, f"第{self.day}夜：狼队友 {s_}号 提议杀 {k}号")
        kill = 0
        for h in humans:   # 真人狼看到 AI 队友的提议后决定（多个真人狼时，最后一个说了算）
            _, kill = self.ask(h, "你是狼人。参考队友的提议，决定今晚要杀的人（你说了算）。", alive)
            proposals[h["seat"]] = kill
            for p in wolves:
                if p is not h:
                    self.tell(p, f"第{self.day}夜：狼队友 {self.label(h)} 提议杀 {kill}号")
        if not humans:
            votes = list(proposals.values())
            top = max(votes.count(v) for v in votes)
            kill = random.choice(sorted({v for v in votes if votes.count(v) == top}))
        victim = self.by_seat(kill)
        for p in wolves:
            self.tell(p, f"第{self.day}夜：狼队最终决定杀 {self.label(victim)}")
        return victim

    def night_seer(self, alive):
        seer = self.role_of("预言家")
        if not seer:
            return
        _, t = self.ask(seer, "你是预言家。选择今晚要查验的人。", [p for p in alive if p is not seer])
        q = self.by_seat(t)
        self.tell(seer, f"第{self.day}夜查验：{self.label(q)} 是{'狼人' if q['role'] == '狼人' else '好人'}")

    def last_words(self, p):
        self.thinking(p)
        text, _ = self.ask(p, "你出局了，请说遗言。", speech=True)
        self.say(f"💬 {self.label(p)}（遗言）：{text}")

    def hunter_shot(self, p, cause):
        """猎人出局时开枪（被毒死不能开枪）"""
        if p["role"] != "猎人" or cause == "poison":
            return
        self.thinking(p)
        _, t = self.ask(p, "你是猎人，你出局了。可以开枪带走一人（0=不开枪）。",
                        [a for a in self.alive() if a is not p], allow_none=True)
        if t:
            target = self.by_seat(t)
            target["alive"] = False
            self.say(f"💥 {self.label(p)} 是猎人，开枪带走了 {self.label(target)}")

    def vote(self, alive):
        """投票：大家都看不到别人的票，最后一起公布"""
        def one(p):
            return p["seat"], self.ask(p, "投票放逐一人（看不到别人的票）。0=弃票。",
                                       [a for a in alive if a is not p], allow_none=True)[1]
        if self.parallel_votes:
            self.emit("   🗳 投票中…（所有人同时投票）", "thinking")
            with ThreadPoolExecutor(len(alive)) as ex:
                return dict(ex.map(one, alive))
        votes = {}
        for p in alive:
            self.thinking(p)
            s, t = one(p)
            votes[s] = t
        return votes

    def day_phase(self, night_dead):
        if self.day == 1:
            for p, _ in night_dead:
                self.last_words(p)
        for p, cause in night_dead:
            self.hunter_shot(p, cause)
        if self.winner():
            return
        # 发言：从随机的存活玩家开始，按座位顺序
        self.show_seats("📋 发言前座位：")
        alive = self.alive()
        k = random.randrange(len(alive))
        for p in alive[k:] + alive[:k]:
            self.thinking(p)
            text, _ = self.ask(p, "白天发言阶段，请发言。", speech=True)
            self.say(f"💬 {self.label(p)}：{text}")
        votes = self.vote(alive)
        self.say("🗳  投票：" + "  ".join(f"{s}→{t or '弃'}" for s, t in sorted(votes.items())))
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
            self.say(f"平票（{'、'.join(map(str, sorted(leaders)))}号），无人出局。")
            return
        out = self.by_seat(leaders[0])
        out["alive"] = False
        self.say(f"被放逐：{self.label(out)}")
        self.last_words(out)
        self.hunter_shot(out, "vote")

    def run(self):
        self.show_seats(f"🐺 {self.n} 人狼人杀开始！座位：")
        with self.lock:
            self.public.append("座位：" + "、".join(self.label(p) for p in self.players))
        self.log("身份（秘密）：" + "、".join(f"{self.real_label(p)}={p['role']}" for p in self.players))
        # 积分统计用：每个座位是谁（AI 的模型 / 真人的登录身份）、是否测试局
        for p in self.players:
            self.log(f"[玩家] {p['seat']}|{p['name']}|{p['kind']}|{p.get('model', '')}|{p.get('uid', '')}")
        for p in self.players:
            if p.get("persona"):
                self.log(f"[人格] {p['seat']}|" + "|".join(f"{k}={v}" for k, v in p["persona"].items()))
        if self.fake:
            self.log("[模式] fake")
        for h in self.humans():
            self.intro(h)
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
            self.result = f"{self.winner()}阵营获胜"
            self.say(f"\n🏁 游戏结束：{self.result}！")
        except Quit:
            self.result = "游戏中途退出"
            self.say("\n游戏已退出。")
        except (KeyboardInterrupt, EOFError):
            self.result = "游戏中途被中断"
            self.say("\n游戏被中断。")
        self.emit("身份揭晓：\n" + "\n".join(
            f"   {self.real_label(p)}：{p['role']}" + (f"（{persona_short(p['persona'])}）" if p.get("persona") else "")
            for p in self.players), "reveal",
            data={"rows": [{"label": self.real_label(p), "role": p["role"], "persona": p.get("persona")}
                           for p in self.players]})          # 网页版用这份数据给三个性格上不同颜色
        self.emit(f"完整记录（含所有私密信息）：{self.logfile}", "logpath")


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


def pick_players(wanted, need, fake, out=print, allow_repeat=False):
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
            results = list(ex.map(lambda m: run_cli(m, PING, PING_TIMEOUT, workdir), batch))
        for m, (ok, err) in zip(batch, results):
            if ok:
                ok_list.append(m)
            else:
                first = next((l for l in err.splitlines() if "ERROR" in l or "error" in l), err)
                out(f"⚠️  {m['name']} 不可用，本局不参加：{first[-200:]}")
    if len(ok_list) < need and allow_repeat and ok_list:
        # 可用模型不够坐满：同一个模型可以坐多个座位（各自独立思考，匿名模式下看不出来）
        base = ok_list[:]
        while len(ok_list) < need:
            ok_list.append(dict(random.choice(base)))
        out(f"可用模型不够 {need} 个，部分模型会占用多个座位。")
    if len(ok_list) < need:
        raise NotEnoughModels(f"可用模型只有 {len(ok_list)} 个，不够 {need} 个，游戏无法开始。")
    spare = queue + ([dict(m) for m in ok_list] if allow_repeat else [])
    return ok_list, spare                               # 备用模型：没测过的 +（允许重复时）已确认可用的


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--players", default="", help="指定参加的模型，逗号分隔，不够的随机补齐")
    ap.add_argument("--list", action="store_true", help="列出模型池")
    ap.add_argument("--fake", action="store_true", help="AI 随机行动（不花额度，测试用）")
    ap.add_argument("--auto", action="store_true", help="你的座位也由 AI 代打（测试用）")
    ap.add_argument("--stats", action="store_true", help="显示积分排行榜")
    ap.add_argument("--size", type=int, default=6, choices=sorted(ROLESETS), help="总人数（默认 6）")
    a = ap.parse_args()
    if a.stats:
        import stats
        stats.print_board()
        sys.exit()
    if a.list:
        for m in MODEL_POOL:
            print(f"  {m['name']:<16} ({m['kind']}: {m['model']})")
        sys.exit()
    need = a.size if a.auto else a.size - 1
    wanted = find_models([n.strip() for n in a.players.split(",") if n.strip()])
    if len(wanted) > need:
        sys.exit(f"最多指定 {need} 个模型。")
    while True:
        # 每局都重新抽取、重新检查模型（额度可能刚恢复或刚用完）
        try:
            chosen, spare = pick_players(wanted, need, a.fake, allow_repeat=a.size > 6)
        except NotEnoughModels as e:
            sys.exit(str(e))
        entries = chosen if a.auto else chosen + [HUMAN]
        g = Game(entries, spare, fake=a.fake, auto=a.auto)
        g.run()
        if a.auto:          # 全自动测试模式：只玩一局
            break
        g.discuss_terminal()
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
