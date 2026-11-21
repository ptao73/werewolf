# 狼人杀 · Werewolf (terminal)

6-player Werewolf in the terminal: **you + 5 AI models**, drawn at random from a pool of
Claude (via `claude`), GPT (via `codex`) and Gemini / GPT-OSS (via `agy`) models.
Players are named after their model, e.g. `Claude-Opus`, `Gemini-Argon`.

6 人终端狼人杀：你 + 5 个 AI 模型（从模型池随机抽取，也可指定），玩家以模型命名。

Roles 角色: 2 狼人, 预言家, 女巫, 2 村民.

## Requirements
- Python 3 (standard library only)
- Logged-in CLIs: `claude`, `codex`, `agy`

## Play
```bash
python3 werewolf.py                                    # random 5 models
python3 werewolf.py --players Claude-Opus,Gemini-Argon # pick some; rest random
python3 werewolf.py --list                             # show the model pool
python3 werewolf.py --fake --auto                      # rule test, no AI quota
python3 werewolf.py --auto                             # all-AI game
```
- Before each game, the drawn models are checked; any that are out of quota or failing
  are reported (`⚠️ … 不可用`) and replaced by another draw.
- After each game: `n` next game (models re-drawn and re-checked), `q` quit.
- If a model fails mid-game: `r` retry, `s` hand its seat (same role and memory) to a spare model, `q` quit.
- Lines marked 🔒 are private to you. Full game records (incl. secrets) go to `logs/`.
- Edit `MODEL_POOL` at the top of `werewolf.py` to add or remove models.

## Web version (multiplayer)
`server.py` serves a phone-friendly page on `127.0.0.1:8010` (same rules engine, imports `werewolf.py`).
Public at https://werewolf.potato73.win via a Cloudflare Tunnel, protected by Cloudflare Access.
The server also verifies the Access JWT itself (team + AUD list in `access_aud.txt`, not in git);
each player is identified by their Access login e-mail.

- One room: the host creates it (6/8/9/12 players), others join with a nickname, the host starts;
  empty seats are filled with randomly drawn AI models (a model may fill several seats).
- In game everyone is only a seat number (AIs too); nicknames and models are revealed at the end.
- Each human has 120 s per action; on timeout: speech = pass, vote = abstain, required pick = random.
- Votes are simultaneous. AI failures are retried, then the seat is handed to a spare model.
- Runs as the user service `werewolf-web.service`.
- Test: `python3 server.py --fake --port 8011 --turn 3`, `python3 test_rules.py`.

Roles: 6 = 2 wolves, seer, witch, 2 villagers; 8 = 3 wolves, seer, witch, hunter, 2 villagers;
9 = 3 wolves, seer, witch, hunter, 3 villagers; 12 = 4 wolves, seer, witch, hunter, guard, 4 villagers.
Terminal: `python3 werewolf.py --size 8`.
