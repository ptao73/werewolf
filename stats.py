#!/usr/bin/env python3
"""狼人杀积分统计：读 logs/ 里所有已完成的真实对局，算出每位玩家（AI 按模型、真人按登录身份）的积分。

计分规则：
  胜利 +10（所在阵营获胜，活着死了都算）
  好人投票：投中狼人 +2，投了好人 -1（弃票 0）
  狼人投票：投的人被放逐且是好人 +1
  预言家：查到狼人 +2
  女巫：毒中狼人 +3，毒了好人 -3；救了好人 +2
  猎人：开枪带走狼人 +3，带走好人 -3
  守卫：守住了狼人当晚要杀的人（且女巫没救，否则同守同救会死）+3
只统计：真实 AI 对局（不含 --fake 随机测试局）、并且打完的（中途退出的不算）。
"""
import glob, re
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
LOGS = HERE / "logs"
OWNER_FILE = HERE / "owner_email.txt"   # 房主（你）的登录邮箱：网页局和终端局算同一个人。不进 git
OWNER = "owner"

WIN = 10
POINTS = {
    "vote_wolf": 2, "vote_good": -1, "wolf_exile_good": 1, "seer_wolf": 2,
    "poison_wolf": 3, "poison_good": -3, "save_good": 2,
    "shoot_wolf": 3, "shoot_good": -3, "guard_block": 3,
}
LABELS = {  # 排行榜里显示的中文名
    "vote_wolf": "投中狼", "vote_good": "投错好人", "wolf_exile_good": "狼投出好人", "seer_wolf": "验出狼",
    "poison_wolf": "毒中狼", "poison_good": "毒错好人", "save_good": "救对人",
    "shoot_wolf": "枪中狼", "shoot_good": "枪错好人", "guard_block": "守住刀",
}


# 最早几局（还没按模型命名时）用的玩家名 → 对应的模型
ALIASES = {"Claude": "Claude-Sonnet", "Haiku-1": "Claude-Haiku", "Haiku-2": "Claude-Haiku",
           "agy": "Gemini-Argon", "Codex": "GPT-6.2-Sol"}


def owner_email():
    return OWNER_FILE.read_text().strip().lower() if OWNER_FILE.exists() else ""


def parse_game(chunk, owner):
    """解析一局的日志。返回 dict，或 None（测试局 / 没打完）"""
    if "[模式] fake" in chunk or "（测试发言）" in chunk or "游戏结束" not in chunk:
        return None
    lines = chunk.splitlines()
    roles, name = {}, {}
    for m in re.finditer(r"(\d+)号 ([^=、]+)=(\S+?)(?=、|$)", lines[0]):
        seat = int(m.group(1)); name[seat] = m.group(2).strip(); roles[seat] = m.group(3)
    if not roles:
        return None
    kind, uid, nick = {}, {}, {}
    for l in lines:
        if l.startswith("[玩家] "):
            seat, nm, k, model, u = (l[5:].split("|") + [""] * 5)[:5]
            kind[int(seat)], uid[int(seat)] = k, u.strip().lower()

    def ident(seat, nm):
        """当前坐在这个座位上的是谁：AI → 模型名；真人 → 登录身份"""
        if kind.get(seat) == "human" or (seat not in kind and nm == "你"):
            u = uid.get(seat, "")
            if not u or u == owner:          # 终端局（没有登录身份）= 你
                nick[OWNER] = nm if nm != "你" else nick.get(OWNER, "你")
                return OWNER
            nick[u] = nm
            return u
        return ALIASES.get(nm, nm)

    who = {s: ident(s, name[s]) for s in roles}           # 座位 → 当前玩家
    occupants = {s: {who[s]} for s in roles}              # 座位 → 坐过这个座位的所有玩家
    is_wolf = lambda s: roles.get(s) == "狼人"
    ev = []                                               # (玩家, 事件)
    kill, guard, saved = {}, {}, set()                    # 第几夜 → 座位

    for idx, l in enumerate(lines):
        if idx == 0:
            continue
        m = re.match(r"\[替补\] (\d+)号 .+ → \d+号 (.+)$", l) or re.match(r"📢 (\d+)号 .*掉线，由 \d+号 (\S+) 接替", l)
        if m:
            s = int(m.group(1)); who[s] = ident(s, m.group(2).strip()); occupants[s].add(who[s]); continue
        m = re.match(r"\[私密→(\d+)号 [^\]]*\] (.*)$", l)
        if m:
            s, t = int(m.group(1)), m.group(2)
            if (x := re.match(r"第(\d+)夜查验：(\d+)号", t)) and is_wolf(int(x.group(2))):
                ev.append((who[s], "seer_wolf"))
            elif x := re.match(r"第(\d+)夜：你毒了 (\d+)号", t):
                ev.append((who[s], "poison_wolf" if is_wolf(int(x.group(2))) else "poison_good"))
            elif x := re.match(r"第(\d+)夜：你用解药救了 (\d+)号", t):
                saved.add(int(x.group(1)))
                if not is_wolf(int(x.group(2))):
                    ev.append((who[s], "save_good"))
            elif x := re.match(r"第(\d+)夜：你守护了 (\d+)号", t):
                guard[int(x.group(1))] = (int(x.group(2)), who[s])
            elif x := re.match(r"第(\d+)夜：狼队最终决定杀 (\d+)号", t):
                kill[int(x.group(1))] = int(x.group(2))
            continue
        if l.startswith("🗳") and "→" in l:
            votes = [(int(a), None if b == "弃" else int(b)) for a, b in re.findall(r"(\d+)→(\d+|弃)", l)]
            exiled = None
            for nxt in lines[idx + 1: idx + 3]:
                if x := re.match(r"被放逐：(\d+)号", nxt):
                    exiled = int(x.group(1))
            for voter, target in votes:
                if target is None:
                    continue
                if is_wolf(voter):
                    if target == exiled and not is_wolf(target):
                        ev.append((who[voter], "wolf_exile_good"))
                else:
                    ev.append((who[voter], "vote_wolf" if is_wolf(target) else "vote_good"))
            continue
        if x := re.match(r"💥 (\d+)号.*开枪带走了 (\d+)号", l):
            ev.append((who[int(x.group(1))], "shoot_wolf" if is_wolf(int(x.group(2))) else "shoot_good"))

    for night, (t, g) in guard.items():
        if kill.get(night) == t and night not in saved:
            ev.append((g, "guard_block"))

    winner = re.search(r"游戏结束：(好人|狼人)阵营获胜", chunk).group(1)
    won = {s: (is_wolf(s) == (winner == "狼人")) for s in roles}
    return {"size": len(roles), "winner": winner, "roles": roles, "occupants": occupants,
            "won": won, "events": ev, "nick": nick}


def load_games():
    owner = owner_email()
    games = []
    for f in sorted(glob.glob(str(LOGS / "game-*.log"))):
        text = open(f, errors="replace").read()
        for chunk in re.split(r"(?=身份（秘密）)", text)[1:]:   # 早期一个文件里可能有多局
            g = parse_game(chunk, owner)
            if g:
                g["file"] = Path(f).name
                games.append(g)
    return games


def leaderboard(games=None):
    games = load_games() if games is None else games
    rows = defaultdict(lambda: {"games": 0, "wins": 0, "judge": 0, "counts": defaultdict(int), "human": False})
    nicks = {}
    for g in games:
        nicks.update(g["nick"])
        for s, players in g["occupants"].items():
            for pl in players:
                r = rows[pl]
                r["games"] += 1
                r["wins"] += g["won"][s]
                r["human"] = r["human"] or pl == OWNER or "@" in pl or pl.startswith("local:")
        for pl, e in g["events"]:
            rows[pl]["judge"] += POINTS[e]
            rows[pl]["counts"][e] += 1
    out = []
    for pl, r in rows.items():
        total = r["wins"] * WIN + r["judge"]
        out.append({
            "name": nicks.get(pl, "你" if pl == OWNER else pl) if r["human"] else pl,
            "human": r["human"], "games": r["games"], "wins": r["wins"],
            "win_rate": round(100 * r["wins"] / r["games"]), "judge": r["judge"], "total": total,
            "avg": round(total / r["games"], 1),
            "detail": {LABELS[k]: v for k, v in sorted(r["counts"].items())},
        })
    out.sort(key=lambda x: (-x["total"], -x["avg"]))
    return {"games": len(games), "rows": out}


def print_board():
    b = leaderboard()
    print(f"🏆 狼人杀积分榜（共 {b['games']} 局已完成的真实对局）")
    print(f"{'名次':<4}{'玩家':<18}{'局数':>4}{'胜':>4}{'胜率':>6}{'判断分':>7}{'总分':>6}{'场均':>6}")
    for i, r in enumerate(b["rows"], 1):
        who = ("👤 " if r["human"] else "🤖 ") + r["name"]
        print(f"{i:<4}{who:<18}{r['games']:>4}{r['wins']:>4}{r['win_rate']:>5}%{r['judge']:>7}{r['total']:>6}{r['avg']:>6}")
    print("\n计分：胜利 +10；好人投中狼 +2、投错 -1；狼投出好人 +1；验出狼 +2；毒中狼 +3、毒错 -3；"
          "救对人 +2；枪中狼 +3、枪错 -3；守住刀 +3。")


if __name__ == "__main__":
    print_board()
