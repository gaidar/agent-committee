# Committee – a decision committee of local agent CLIs

Runs Claude Code, Codex, Grok and Antigravity (agy) as one committee on one problem. Each agent gets a written role, answers blind, then cross-examines the others' anonymized answers, then a chair writes the decision memo. One Python file, standard library only, no API keys – each CLI uses its own login.

```
problem.md ─┬─> claude (Lead analyst)    ─┐
            ├─> codex  (Operator)        ─┤  round 1: blind, parallel
            ├─> grok   (Devil's advocate)─┤
            └─> agy    (Risk officer)    ─┘
                         │ answers shuffled into Member A–D, vendor names hidden
                         v
            each member attacks / revises / holds, votes again   round 2..N
                         v
            chair (claude by default, falls back to next member) writes decision.md
```

## Setup (10 minutes)

1. Python 3.9+ (`python3 --version`). Nothing to pip install.
2. Log in to each CLI once, interactively, so headless runs can reuse the login:
   `claude` · `codex login` · `grok login` · `agy` (sign in on first launch).
3. Put this folder anywhere, e.g. `~/committee`, and run:

```
cd ~/committee
python3 committee.py --check
```

Nothing is installed outside this folder, and there's no Grok config file to set up – see "Grok sandbox".

`--check` pings each CLI headless and prints OK/FAIL with the reason. Fix any FAIL before a real run (see troubleshooting). Two working members is the minimum.

## Run it

```
# quick question, free-form votes
python3 committee.py "What should our Q1 partner recruitment motion be?"

# a decision with fixed options – votes are tallied against them
python3 committee.py -f problem.md --options "Build,Buy,Wait"

# give it material: short files are pasted into the brief, a folder is readable by the agents
python3 committee.py -f problem.md --context pricing.md --context notes.md --cwd ~/code/repo

# subset, more rounds, different chair, longer answers
python3 committee.py -f problem.md --members claude,codex,grok --rounds 3 --chair codex --words 900
```

Copy `problem-template.md` for anything non-trivial – the quality of the brief is most of the quality of the answer.

Each run writes a folder under `runs/`:

| File | What's in it |
|---|---|
| `decision.md` | Chair's memo, orchestrator vote tally, seat map (which vendor was which member) |
| `rounds/roundN.md` | Every member's full statement per round |
| `logs/` | Exact prompt, stdout and stderr for every call – for debugging and re-runs |
| `run.json` | Timings, parsed votes, confidence, vetoes, failures |

## How the protocol works

- **Round 1 is blind.** No member sees the others, so you get four independent positions instead of the first answer anchoring the rest.
- **Cross-examination is anonymized.** Members see "Member B (Operator)", never "Codex", so they argue with the content, not the brand. Seats are shuffled every run.
- **Fixed output format.** Every statement ends with `VOTE:` and `CONFIDENCE:` lines; the risk officer adds `VETO:`. The orchestrator parses these, so the tally doesn't depend on the chair's reading.
- **Early stop.** If round 1 is unanimous on a listed option with no veto, further rounds are skipped.
- **Failure-tolerant.** Each call has a hard timeout (default 15 min) and the whole process tree is killed on timeout. A member that fails in round 2 keeps its round 1 statement. A failed chair hands off to the next member. Fewer than 2 answers in round 1 stops the run (exit code 2).
- **Advisory, read-only by default.** Claude runs in `dontAsk` mode with read and search tools only, Codex in its `read-only` sandbox, Grok in its built-in `workspace` sandbox inside the run folder plus `--deny Edit`, agy with `--sandbox`. Members can read files in `--cwd` but are told not to change anything.

## Customize

Everything is in `council.json` and `roles/`.

- **Change a role:** edit the markdown in `roles/`. The first line is the role title shown to other members.
- **Pick a model:** add the CLI's model flag to `cmd`, e.g. `"--model", "opus"` for Claude, `"-m", "<model>"` for Codex, `"--model", "<id>"` for agy (`agy models` lists ids), `"-m", "<id>"` for Grok (`grok models`).
- **Same CLI twice:** copy a member block, give it a new `name` and a different `role` (and model). Useful when a CLI is down or you want two lenses from one vendor.
- **Add any other CLI:** anything that takes a prompt and prints an answer works – Ollama, Kimi, Cursor Agent. Placeholders: `{prompt}` (whole prompt as one argument), `{promptfile}` (path to a file holding it), `{outfile}` (per-call output path). `"stdin": true` pipes the prompt instead. `output.type` is `text`, `json` (with `field`) or `file`.
- **Isolate a member:** `"own_folder": true` runs it in the run folder instead of `--cwd`; it reads your material by absolute path. Use it for any CLI whose sandbox allows writes to the working folder.
- **Disable a member:** `"enabled": false`.

## Grok sandbox

Grok's built-in `read-only` profile blocks network access for child processes by masking container sockets such as `/var/run/docker.sock`. With Docker Desktop, OrbStack or Colima on macOS that path is a symlink, and Grok refuses to start rather than run with a gap:

```
socket deny resolution failed: could not resolve runtime-socket deny path /var/run/docker.sock: endpoint is a symlink
```

A custom profile doesn't help here without touching `~/.grok`: Grok ignores a project-level `.grok/sandbox.toml` unless the folder is marked trusted, and trust is recorded in `~/.grok/trusted_folders.toml` – for every new run folder.

So `council.json` uses Grok's built-in `workspace` profile (`GROK_SANDBOX=workspace`), which needs no file and doesn't mask sockets, and sets `"own_folder": true` for Grok. Together:

- Grok runs in the throwaway run folder (`runs/<run>/`), even when you pass `--cwd`.
- The sandbox lets it read anywhere but write only in that run folder, `~/.grok` and temp. Your `--cwd` material is outside all three, so the kernel blocks writes to it.
- Every member gets the absolute path of the `--cwd` folder in its prompt, so Grok still reads your material.
- `--deny Edit` blocks Grok's file-edit tool as a second guard.

What you give up compared with `read-only`: programs Grok starts can reach the network, and Grok could write into its own run folder. Grok's model and web search run in-process and were never covered by the network block.

If you tried a `committee` profile earlier, delete `.grok/sandbox.toml` from this folder and the `[profiles.committee]` block from `~/.grok/sandbox.toml` if you added one. Neither is used any more.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Grok: `endpoint is a symlink ... Refusing to start` | Grok is on `read-only` or `strict`. Set `GROK_SANDBOX` to `workspace` in `council.json`. See "Grok sandbox". |
| Grok: `Custom sandbox profile '...' not found` | An old `council.json` pointing at a custom profile. Set `GROK_SANDBOX` to `workspace` and `"own_folder": true`. |
| `not found on PATH` | CLI installed for another shell. Run `which agy` in the same terminal; add its folder to PATH (agy installs to `~/.local/bin`). |
| `exit 1: ... login` / auth errors | Headless runs can't open a browser. Run the CLI interactively once and log in. |
| agy `timed out` or `empty response` | Known agy print-mode issues with non-terminal output in some versions (GitHub issues #76, #318, #548). Run `agy update`, then `--check` again. Stdin is already closed, which fixes the most common hang. If it still fails, disable agy for now – three members work fine. |
| `could not parse JSON output` | CLI changed its JSON shape. Open `logs/*.stdout.log`, fix `field` in `council.json`, or switch that member to `"output": {"type": "text"}` and drop its `--output-format` flag. |
| `Argument list too long` | Brief too big for a command-line argument (about 128 KB on Linux; less on Windows). Put large material in a folder and pass `--cwd` so agents read it, or switch that member to `"stdin": true` / `{promptfile}`. |
| Windows | Works, but `cmd.exe` limits commands to about 8,000 characters, which round 2 can exceed. Use WSL, or switch members to stdin/prompt-file delivery (verify with `--check`). |
| Run takes long | Each call is a full agent session, typically 15 seconds to several minutes (estimate) depending on web search and file reading. Four members × two rounds + chair = 9 calls, 4 in parallel. |
