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

## Thinking notebooks & leaderboard
- Every AI decision carries a private thinking note; the AI sees its own notebook each turn.
  Notebooks are saved per seat in `logs/<game>/NN号.md` and used in the post-game discussion.
- `stats.py` scores all finished real games in `logs/` (random-move test games and unfinished games excluded).
  AIs are ranked by model, humans by login (shown by nickname). `owner_email.txt` (not in git) maps your
  web login to your terminal games. View: `python3 werewolf.py --stats` or the 🏆 button on the web page.
- Points: win +10; good-side vote on a wolf +2 / on a good player -1; wolf vote that exiles a good player +1;
  seer finds a wolf +2; witch poisons a wolf +3 / a good player -3, saves a good player +2;
  hunter shoots a wolf +3 / a good player -3; guard blocks the kill +3.

## AI personalities
Each AI that plays gets three random traits per game (0-100%, U-shaped: extremes are common): 理性 (emotional ↔ rational),
好胜 (easy-going ↔ extremely competitive), 诚实 (cunning ↔ honest). They shape how the AI talks and decides.
Traits are hidden during the game (showing them would reveal which seats are AIs) and revealed with the roles at the end.
