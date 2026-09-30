#!/usr/bin/env python3
"""
committee.py - run a committee of local agent CLIs on one problem.

Protocol:
  Round 1  every member answers blind, in parallel, in a fixed format
  Round 2+ every member sees the others' answers (anonymized as Member A, B, ...)
           and must attack, revise or hold its position, then vote
  Chair    one member writes the decision memo from the record only

Everything is plain files in runs/<timestamp>-<slug>/, so any step can be
inspected or re-run. Standard library only, Python 3.9+.

Usage:
  python3 committee.py --check
  python3 committee.py "Should we build or buy X?" --options "Build,Buy,Wait"
  python3 committee.py -f problem.md --context notes.md --cwd ~/code/repo
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import shutil
import signal
import string
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
VOTE_RE = re.compile(r"^[\s*_>#-]*VOTE[\s*_]*:\s*(.+?)[\s*_]*$", re.I | re.M)
CONF_RE = re.compile(r"^[\s*_>#-]*CONFIDENCE[\s*_]*:\s*(\d{1,3})", re.I | re.M)
VETO_RE = re.compile(r"^[\s*_>#-]*VETO[\s*_]*:\s*(yes|no)\b[\s,.:;–-]*(.*)$", re.I | re.M)

PING = "Reply with the single word PONG and nothing else."

# Known failure signatures (all substrings must match) -> what to do (shown by --check).
HINTS = [
    (("endpoint is a symlink",),
     "Grok's read-only/strict profiles can't mask the Docker socket symlink. "
     "Set GROK_SANDBOX to 'workspace' and \"own_folder\": true for grok in council.json (README: Grok sandbox)."),
    (("custom sandbox profile",),
     "Grok ignores project .grok/sandbox.toml in untrusted folders. Use the built-in profile: "
     "GROK_SANDBOX 'workspace' and \"own_folder\": true in council.json (README: Grok sandbox)."),
    (("trust",), "Grok wants folder trust. Check that grok has \"own_folder\": true and uses a built-in "
     "sandbox profile (README: Grok sandbox)."),
    (("not found on path",), "Install the CLI or add its folder to PATH in this shell."),
    (("login",), "Run the CLI once interactively and log in."),
    (("argument list too long",), "Brief too big for argv: use --cwd for large material, or \"stdin\": true."),
]


def hint_for(error: str) -> str:
    low = error.lower()
    for needles, hint in HINTS:
        if all(n in low for n in needles):
            return hint
    return ""


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    member: str
    ok: bool
    text: str = ""
    error: str = ""
    seconds: float = 0.0
    exit_code: int | None = None
    vote: str = ""
    confidence: int | None = None
    veto: str = ""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    for m in cfg["members"]:
        m.setdefault("enabled", True)
        m.setdefault("output", {"type": "text"})
        role = m.get("role")
        m["_role_text"] = (base / role).read_text(encoding="utf-8").strip() if role else ""
        m["_role_title"] = role_title(m["_role_text"]) or m["name"]
        # Files copied into the member's own working folder before each run,
        # e.g. {".grok/sandbox.toml": ".grok/sandbox.toml"}. Paths are relative to this config.
        m["_workspace_files"] = {dst: base / src for dst, src in m.get("workspace_files", {}).items()}
        for dst, src in m["_workspace_files"].items():
            if not src.is_file():
                sys.exit(f"{m['name']}: workspace file {src} not found (needed for {dst}).")
    chair_role = cfg.get("chair_role", "roles/chair.md")
    cfg["_chair_text"] = (base / chair_role).read_text(encoding="utf-8").strip()
    cfg.setdefault("timeout_seconds", 900)
    cfg.setdefault("max_parallel", 4)
    return cfg


def role_title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return re.sub(r"^#+\s*(Role:\s*)?", "", line).strip()
    return ""


# --------------------------------------------------------------------------- #
# Running one CLI
# --------------------------------------------------------------------------- #
def build_argv(member: dict, prompt: str, outfile: Path, promptfile: Path) -> list[str]:
    argv = []
    for a in member["cmd"]:
        if a == "{prompt}":
            argv.append(prompt)
        else:
            argv.append(a.replace("{outfile}", str(outfile)).replace("{promptfile}", str(promptfile)))
    return argv


def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def parse_json_blob(s: str):
    s = s.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Last JSON object line (stream-style output), then widest {...} span.
    for line in reversed(s.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    a, b = s.find("{"), s.rfind("}")
    if 0 <= a < b:
        try:
            return json.loads(s[a : b + 1])
        except json.JSONDecodeError:
            return None
    return None


def extract(spec: dict, stdout: str, outfile: Path) -> tuple[str, str | None]:
    kind = spec.get("type", "text")
    if kind == "file":
        return (outfile.read_text(encoding="utf-8", errors="replace") if outfile.exists() else ""), None
    if kind == "json":
        data = parse_json_blob(stdout)
        if not isinstance(data, dict):
            return "", "could not parse JSON output"
        sf = spec.get("status_field")
        if sf and str(data.get(sf)) != str(spec.get("status_ok")):
            return "", f"{sf}={data.get(sf)!r}"
        ef = spec.get("error_field")
        if ef and data.get(ef):
            return "", "agent reported error: " + str(data.get(spec.get("field", ""), ""))[:300]
        return str(data.get(spec.get("field", "result"), "") or ""), None
    return ANSI.sub("", stdout), None


def tail(s: str, n: int = 400) -> str:
    s = ANSI.sub("", s or "").strip()
    return s[-n:] if len(s) > n else s


def run_cli(member: dict, prompt: str, cwd: Path, logdir: Path, tag: str, timeout: int) -> Result:
    name = member["name"]
    outfile = logdir / f"{tag}.{name}.lastmsg.txt"
    promptfile = (logdir / f"{tag}.{name}.prompt.md").resolve()
    promptfile.write_text(prompt, encoding="utf-8")
    argv = build_argv(member, prompt, outfile, promptfile)
    exe = shutil.which(argv[0])
    if exe is None:
        return Result(name, False, error=f"'{argv[0]}' not found on PATH")
    argv[0] = exe  # full path, so Windows .cmd shims resolve too

    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env.update({k: str(v) for k, v in member.get("env", {}).items()})
    kw = {"start_new_session": True} if os.name == "posix" else {}
    use_stdin = bool(member.get("stdin"))  # send the prompt on stdin instead of argv

    t0 = time.time()
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", env=env, **kw,
        )
    except OSError as e:
        hint = " (prompt too long for argv – set \"stdin\": true or use {promptfile})" \
            if getattr(e, "errno", None) == 7 else ""
        return Result(name, False, error=f"{e}{hint}")

    timed_out = False
    try:
        out, err = proc.communicate(input=prompt if use_stdin else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
    secs = round(time.time() - t0, 1)
    (logdir / f"{tag}.{name}.stdout.log").write_text(out or "", encoding="utf-8")
    (logdir / f"{tag}.{name}.stderr.log").write_text(err or "", encoding="utf-8")

    if timed_out:
        return Result(name, False, error=f"timed out after {timeout}s", seconds=secs)
    if proc.returncode != 0:
        return Result(name, False, error=f"exit {proc.returncode}: {tail(err or out)}",
                      seconds=secs, exit_code=proc.returncode)
    text, perr = extract(member["output"], out or "", outfile)
    if perr:
        return Result(name, False, error=perr, seconds=secs, exit_code=0)
    if not text.strip():
        return Result(name, False, error="empty response (see stdout/stderr logs)",
                      seconds=secs, exit_code=0)
    return Result(name, True, text=text.strip(), seconds=secs, exit_code=0)


def prepare_cwds(members: list[dict], material: Path, run_dir: Path) -> dict[str, Path]:
    """Members with own_folder (or workspace_files) work in the run folder and read the
    material folder by absolute path, so a sandbox that allows writes to the working
    folder can only write into the throwaway run folder. Everyone else works in the
    material folder."""
    cwds = {}
    for m in members:
        if m.get("own_folder") or m["_workspace_files"]:
            for dst, src in m["_workspace_files"].items():
                target = run_dir / dst
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
            cwds[m["name"]] = run_dir
        else:
            cwds[m["name"]] = material
    return cwds


def run_many(jobs: list[tuple[dict, str]], cwds: dict[str, Path], logdir: Path, tag: str,
             timeout: int, parallel: int) -> dict[str, Result]:
    results: dict[str, Result] = {}
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        futs = {ex.submit(run_cli, m, p, cwds[m["name"]], logdir, tag, timeout): m["name"]
                for m, p in jobs}
        for fut in as_completed(futs):
            r = fut.result()
            results[futs[fut]] = r
            status = "ok" if r.ok else f"FAILED – {r.error}"
            log(f"  [{tag}] {r.member:<8} {r.seconds:>6.1f}s  {status}")
    return results


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
RULES = """Session rules:
- You are advising, not executing. Do not create, edit or delete files. Reading files and searching is fine.
- Be concrete: numbers, names, mechanisms. Label estimates, assumptions and unknowns.
- Keep the whole answer under {words} words.
- End with the machine-readable lines exactly as shown (VOTE, CONFIDENCE, and any line your role requires)."""

ROUND1 = """{role}

# Problem
{problem}
{options}
# Your task – round 1 of {total}: independent position
You have not seen the other members' views. Give your own position.

Answer in this format:
## Position
## Reasoning
(max 5 bullets)
## Assumptions
## Risks and failure modes
## What would change my mind
VOTE: <{vote_hint}>
CONFIDENCE: <0-100>%

{rules}"""

CROSS = """{role}

# Problem
{problem}
{options}
# Your position from round {prev}
{own}

# The other members' positions from round {prev} (anonymized)
{others}

# Your task – round {n} of {total}: cross-examination
Test your position against the others. Change your mind where they are right, hold where they are not.
Do not average positions to be agreeable, and do not concede to a majority without a reason.

Answer in this format:
## Strongest argument against my position
## Where other members are wrong
(cite them as Member X)
## What I changed and why
(or "No change" and why)
## Final position
VOTE: <{vote_hint}>
CONFIDENCE: <0-100>%

{rules}"""

CHAIR = """{chair_role}

# Problem
{problem}
{options}
# Committee record – final round, anonymized
{statements}

# Vote tally (parsed by the orchestrator)
{tally}

# Seats with no statement
{absent}

Write the committee's decision memo in this format:
## Decision
## Vote
## Where the committee agrees
## Where it disagrees
## Minority report
## Conditions and risks
## Next steps
## Open questions

{rules}"""


def options_block(options: list[str]) -> str:
    if not options:
        return ""
    return "\n# Options on the table\n" + "\n".join(f"- {o}" for o in options) + "\n"


def vote_hint(options: list[str]) -> str:
    return "one of: " + " | ".join(options) if options else "your position in one line"


def parse_markers(r: Result) -> Result:
    if not r.ok:
        return r
    votes = VOTE_RE.findall(r.text)
    confs = CONF_RE.findall(r.text)
    vetos = VETO_RE.findall(r.text)
    r.vote = votes[-1].strip().strip("<>").strip() if votes else ""
    r.confidence = min(100, int(confs[-1])) if confs else None
    if vetos:
        flag, why = vetos[-1]
        r.veto = f"{flag.lower()} {why.strip()}".strip()
    return r


def match_option(vote: str, options: list[str]) -> str:
    v = vote.lower().strip(" .*_")
    for o in options:
        if v == o.lower() or v.startswith(o.lower()):
            return o
    hits = [o for o in options if re.search(rf"\b{re.escape(o.lower())}\b", v)]
    return hits[0] if len(hits) == 1 else f"other: {vote}"


def tally(results: dict[str, Result], seats: dict[str, str], options: list[str]) -> str:
    member_lines, counts = [], {}
    for name, r in results.items():
        if not r.ok:
            continue
        v = match_option(r.vote, options) if options else (r.vote or "(no vote line)")
        counts[v] = counts.get(v, 0) + 1
        conf = f"{r.confidence}%" if r.confidence is not None else "n/a"
        veto = f"; VETO {r.veto}" if r.veto else ""
        member_lines.append(f"- Member {seats[name]}: {v} (confidence {conf}{veto})")
    if not member_lines:
        return "(no votes)"
    out = sorted(member_lines)
    if options:
        out += ["", "Count:"] + [f"- {k}: {c}" for k, c in sorted(counts.items(), key=lambda x: -x[1])]
    return "\n".join(out)


def consensus(results: dict[str, Result], options: list[str]) -> bool:
    if not options:
        return False
    votes = {match_option(r.vote, options) for r in results.values() if r.ok}
    oks = [r for r in results.values() if r.ok]
    return len(oks) >= 2 and len(votes) == 1 and not next(iter(votes)).startswith("other:") \
        and not any(r.veto.startswith("yes") for r in oks)


def labelled(r: Result, seats: dict[str, str], titles: dict[str, str]) -> str:
    return f"## Member {seats[r.member]} ({titles[r.member]})\n\n{r.text}\n"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_check(cfg: dict, members: list[dict]) -> int:
    tmp = HERE / "runs" / "_check"
    shutil.rmtree(tmp, ignore_errors=True)  # fresh each time, so stale files can't mask a problem
    tmp.mkdir(parents=True, exist_ok=True)
    timeout = min(int(cfg["timeout_seconds"]), 240)
    log(f"Pinging {len(members)} member(s), timeout {timeout}s each...")
    cwds = prepare_cwds(members, tmp, tmp)
    res = run_many([(m, PING) for m in members], cwds, tmp, "check", timeout, len(members))
    print("\nmember    status  seconds  detail")
    ok = 0
    for m in members:
        r = res[m["name"]]
        good = r.ok and "PONG" in r.text.upper()
        ok += good
        detail = r.text[:60].replace("\n", " ") if r.ok else r.error[:200].replace("\n", " ")
        print(f"{m['name']:<9} {'OK' if good else 'FAIL':<7} {r.seconds:>7.1f}  {detail}")
        hint = "" if good else hint_for(r.error or r.text)
        if hint:
            print(f"{'':<25}fix: {hint}")
    print(f"\n{ok}/{len(members)} ready. Logs: {tmp}")
    return 0 if ok >= 2 else 1


def cmd_run(cfg: dict, members: list[dict], args) -> int:
    problem = read_problem(args)
    for c in args.context:
        p = Path(c).expanduser()
        problem += f"\n\n## Context: {p.name}\n\n{p.read_text(encoding='utf-8', errors='replace')}"
    options = [o.strip() for o in (args.options or "").split(",") if o.strip()]
    total = max(1, args.rounds)
    timeout = int(args.timeout or cfg["timeout_seconds"])
    parallel = int(cfg["max_parallel"])

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", problem.lower())[:40].strip("-") or "run"
    run_dir = Path(args.out).expanduser() / f"{stamp}-{slug}"
    logdir = run_dir / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    run_dir = run_dir.resolve()
    logdir = run_dir / "logs"
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else run_dir
    if args.cwd:
        if not cwd.is_dir():
            sys.exit(f"--cwd {cwd} is not a folder.")
        problem += (f"\n\n## Material\nFiles for this problem are in {cwd}. Read what you need "
                    f"there using that absolute path. Do not modify anything in it.")
    (run_dir / "problem.md").write_text(problem, encoding="utf-8")
    cwds = prepare_cwds(members, cwd, run_dir)

    letters = random.sample(string.ascii_uppercase[: max(len(members), 2)], len(members))
    seats = {m["name"]: letters[i] for i, m in enumerate(members)}
    titles = {m["name"]: m["_role_title"] for m in members}
    rules = RULES.format(words=args.words)
    ob, vh = options_block(options), vote_hint(options)
    meta = {"started": stamp, "problem_file": "problem.md", "cwd": str(cwd), "options": options,
            "seats": seats, "rounds": []}

    roster = ", ".join("{}=Member {}".format(m["name"], seats[m["name"]]) for m in members)
    log(f"Committee: {roster}")
    log(f"Run folder: {run_dir}")

    # Round 1 – blind
    jobs = [(m, ROUND1.format(role=m["_role_text"], problem=problem, options=ob, total=total,
                              vote_hint=vh, rules=rules)) for m in members]
    current = {k: parse_markers(v) for k, v in run_many(jobs, cwds, logdir, "r1", timeout, parallel).items()}
    save_round(run_dir, 1, current, seats, titles, meta)
    live = [m for m in members if current[m["name"]].ok]
    if len(live) < 2:
        log("Quorum not met: fewer than 2 members answered in round 1. See logs/.")
        write_meta(run_dir, meta)
        return 2

    # Rounds 2..N – cross-examination
    for n in range(2, total + 1):
        if consensus(current, options):
            log(f"  Unanimous vote after round {n - 1}; skipping further rounds.")
            break
        jobs = []
        for m in live:
            own = current[m["name"]].text
            others = "\n".join(labelled(current[x["name"]], seats, titles) for x in live if x is not m)
            jobs.append((m, CROSS.format(role=m["_role_text"], problem=problem, options=ob, prev=n - 1,
                                         own=own, others=others, n=n, total=total, vote_hint=vh,
                                         rules=rules)))
        new = {k: parse_markers(v) for k, v in run_many(jobs, cwds, logdir, f"r{n}", timeout, parallel).items()}
        # A member that drops out mid-debate keeps its last statement on the record.
        for name, r in new.items():
            if r.ok:
                current[name] = r
            else:
                log(f"  {name} failed in round {n}; keeping its round {n - 1} statement.")
        save_round(run_dir, n, new, seats, titles, meta)

    # Chair
    finals = [current[m["name"]] for m in live]
    absent = [f"Member {seats[m['name']]} ({titles[m['name']]}): {current[m['name']].error}"
              for m in members if not current[m["name"]].ok] or ["none"]
    t = tally({r.member: r for r in finals}, seats, options)
    chair_prompt = CHAIR.format(chair_role=cfg["_chair_text"], problem=problem, options=ob,
                                statements="\n".join(labelled(r, seats, titles) for r in finals),
                                tally=t, absent="\n".join(absent), rules=rules)
    order = [args.chair or cfg.get("chair", members[0]["name"])] + [m["name"] for m in live]
    by_name = {m["name"]: m for m in members}
    memo = None
    for name in dict.fromkeys(order):
        if name not in by_name:
            continue
        log(f"Chair: {name}")
        r = run_cli(by_name[name], chair_prompt, cwds[name], logdir, "chair", timeout)
        if r.ok:
            memo, meta["chair"] = r.text, name
            break
        log(f"  chair {name} failed – {r.error}; trying next")
    if memo is None:
        memo = "Chair failed. Raw vote tally below; read rounds/ for the statements.\n\n" + t

    seat_map = "\n".join(f"- Member {seats[m['name']]}: {m['name']} – {titles[m['name']]}"
                         + ("" if current[m["name"]].ok else " (no statement)")
                         for m in sorted(members, key=lambda m: seats[m["name"]]))
    decision = (f"# Committee decision\n\n{memo}\n\n---\n\n## Vote tally (orchestrator)\n\n{t}\n\n"
                f"## Seat map\n\n{seat_map}\n- Chair: {meta.get('chair', 'none')}\n\n"
                f"Run folder: {run_dir}\n")
    (run_dir / "decision.md").write_text(decision, encoding="utf-8")
    write_meta(run_dir, meta)
    print(decision)
    return 0


def read_problem(args) -> str:
    if args.problem_file:
        return Path(args.problem_file).expanduser().read_text(encoding="utf-8").strip()
    if args.problem:
        if args.problem.startswith("@"):
            return Path(args.problem[1:]).expanduser().read_text(encoding="utf-8").strip()
        return args.problem.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    sys.exit("Give a problem: committee.py \"...\" or -f problem.md or pipe it on stdin.")


def save_round(run_dir: Path, n: int, results: dict[str, Result], seats, titles, meta) -> None:
    d = run_dir / "rounds"
    d.mkdir(exist_ok=True)
    body = [f"# Round {n}\n"]
    for name, r in sorted(results.items(), key=lambda kv: seats[kv[0]]):
        head = f"## Member {seats[name]} – {name} ({titles[name]})"
        body.append(f"{head}\n\n{r.text if r.ok else '_No statement: ' + r.error + '_'}\n")
    (d / f"round{n}.md").write_text("\n".join(body), encoding="utf-8")
    meta["rounds"].append({"round": n, "results": {
        k: {x: y for x, y in asdict(v).items() if x != "text"} for k, v in results.items()}})


def write_meta(run_dir: Path, meta: dict) -> None:
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a committee of local agent CLIs on one problem.")
    ap.add_argument("problem", nargs="?", help='problem text, or @path/to/problem.md')
    ap.add_argument("-f", "--problem-file", help="read the problem from a file")
    ap.add_argument("--context", action="append", default=[], help="file to append to the brief (repeatable)")
    ap.add_argument("--cwd", help="folder members may read, e.g. a repo (default: the run folder)")
    ap.add_argument("--options", help='comma-separated options to vote on, e.g. "Build,Buy,Wait"')
    ap.add_argument("--rounds", type=int, default=2, help="1 = blind answers only; 2+ adds cross-examination")
    ap.add_argument("--members", help="comma-separated subset, e.g. claude,codex,grok")
    ap.add_argument("--chair", help="member that writes the decision memo (default from config)")
    ap.add_argument("--timeout", type=int, help="seconds per agent call (default from config)")
    ap.add_argument("--words", type=int, default=600, help="word cap per statement")
    ap.add_argument("--config", default=str(HERE / "council.json"))
    ap.add_argument("--out", default=str(HERE / "runs"))
    ap.add_argument("--check", action="store_true", help="ping each CLI headless and exit")
    args = ap.parse_args()

    cfg = load_config(Path(args.config).expanduser())
    members = [m for m in cfg["members"] if m["enabled"]]
    if args.members:
        want = [x.strip() for x in args.members.split(",")]
        members = [m for m in cfg["members"] if m["name"] in want]
    if not members:
        sys.exit("No members selected.")
    if args.check:
        return cmd_check(cfg, members)
    if len(members) < 2:
        sys.exit("A committee needs at least 2 members.")
    return cmd_run(cfg, members, args)


if __name__ == "__main__":
    sys.exit(main())
