# 规则单项测试：用"剧本"控制每个角色的选择，检查结算结果
import sys, random; from pathlib import Path; sys.path.insert(0, str(Path(__file__).parent))
import werewolf as w

def make(n, roles):
    g = w.Game([{"name": f"AI{i}", "kind": "claude", "model": "x"} for i in range(n)], [], fake=True)
    for p, r in zip(g.players, roles): p["role"] = r
    g.emit = lambda *a, **k: None
    return g

def scripted(g, script):
    """script: 函数(玩家, 任务) -> 座位号"""
    g.ask = lambda p, task, choices=None, allow_none=False, speech=False: ("", script(p, task, choices))

R12 = ["狼人"]*4 + ["预言家", "女巫", "猎人", "守卫"] + ["村民"]*4   # 座位 1-4 狼，5 预，6 女巫，7 猎人，8 守卫，9-12 民
def night(guard_t, kill_t, save, poison_t):
    g = make(12, R12)
    def sc(p, task, choices):
        if p["role"] == "守卫": return guard_t
        if p["role"] == "狼人": return kill_t
        if "解药" in task: return kill_t if save else 0
        if "毒药" in task: return poison_t
        return choices[0]["seat"]
    scripted(g, sc)
    return g, {p["seat"]: c for p, c in g.night()}

checks = []
def check(name, got, want): checks.append((name, got == want, got, want))
_, d = night(0, 9, False, 0);  check("not guarded, not saved -> dies", d, {9: "kill"})
_, d = night(9, 10, False, 0); check("guard other -> dies", d, {10: "kill"})
_, d = night(10, 10, False, 0); check("guarded only -> survives", d, {})
_, d = night(9, 10, True, 0);  check("saved only -> survives", d, {})
_, d = night(10, 10, True, 0); check("guarded AND saved -> dies", d, {10: "kill"})
_, d = night(0, 10, False, 11); check("kill + poison other -> both die", d, {10: "kill", 11: "poison"})
_, d = night(11, 10, False, 11); check("guard does not stop poison", d, {10: "kill", 11: "poison"})
_, d = night(0, 7, False, 7);  check("killed+poisoned same -> poison", d, {7: "poison"})

# 守卫不能连续两晚守同一人
g = make(12, R12); opts_seen = []
def sc(p, task, choices):
    if p["role"] == "守卫": opts_seen.append([c["seat"] for c in choices]); return 9
    if p["role"] == "狼人": return 10
    return 0 if choices and "解药" in task or "毒药" in task else choices[0]["seat"]
scripted(g, sc); g.night(); g.night()
check("guard: night-2 options exclude last target 9", 9 in opts_seen[1], False)

# 猎人：被杀能开枪，被毒不能，被放逐能开枪
for cause, want_shot in [("kill", True), ("poison", False), ("vote", True)]:
    g = make(12, R12); hunter = g.by_seat(7); hunter["alive"] = False
    asked = []
    scripted(g, lambda p, task, choices: (asked.append(task), 1)[1])
    g.hunter_shot(hunter, cause)
    check(f"hunter {cause}: shoots={want_shot}", (len(asked) == 1, g.by_seat(1)["alive"] is False), (want_shot, want_shot))

# 胜负
g = make(6, ["狼人"]*2 + ["预言家", "女巫"] + ["村民"]*2)
for s in (1, 2): g.by_seat(s)["alive"] = False
check("all wolves dead -> good wins", g.winner(), "好人")
g = make(6, ["狼人"]*2 + ["预言家", "女巫"] + ["村民"]*2)
for s in (3, 4): g.by_seat(s)["alive"] = False
check("wolves == good -> wolves win", g.winner(), "狼人")

# 规则文字：12 人有守卫/猎人说明，6 人没有
check("12p rules mention guard+hunter", ("守卫" in w.rules_text(12), "猎人" in w.rules_text(12)), (True, True))
check("6p rules no guard/hunter", ("守卫" in w.rules_text(6), "猎人" in w.rules_text(6)), (False, False))
for name, ok, got, want in checks: print("PASS" if ok else "FAIL", name, "" if ok else f"got={got} want={want}")
print(sum(c[1] for c in checks), "/", len(checks), "passed")
