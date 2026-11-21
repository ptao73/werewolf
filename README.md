# 狼人杀 · Werewolf (terminal)

6-player Werewolf in the terminal: you + Claude + Codex + agy (Gemini) + 2 Claude Haiku bots.
6 人终端狼人杀：你 + Claude + Codex + agy + 2 个 Haiku 机器人。

Roles 角色: 2 狼人, 预言家, 女巫, 2 村民.

## Requirements
- Python 3 (standard library only)
- Logged-in CLIs: `claude`, `codex`, `agy`

## Play
```bash
python3 werewolf.py               # play
python3 werewolf.py --fake --auto # rule test: random moves, no AI quota
python3 werewolf.py --auto        # all-AI game (Haiku plays your seat)
```
Lines marked 🔒 are private to you. Full game records (incl. secrets) go to `logs/`.
If an AI call fails, the game shows the error and asks to retry or quit.
