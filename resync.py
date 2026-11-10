#!/usr/bin/env python3
"""Copy every Claude Desktop account's Code-session pointers into the signed-in account's folder.

The sidebar reads <app-data>/claude-code-sessions/<account>/<org>/local_*.json; transcripts
(~/.claude/projects/*/<cliSessionId>.jsonl) are shared. Run only while Claude Desktop is closed.
"""
import argparse, json, os, re, shutil, stat, subprocess, sys, time
from collections import namedtuple
from pathlib import Path

HOME = Path.home()
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LOCAL = re.compile(r"^local_([\w-]+)\.json$")
DELETED = re.compile(r"^deleted_([\w-]+)$")
MAIN, LEGACY = "claude-code-sessions", "local-agent-mode-sessions"
AUDIT = HOME / ".claude" / "session-resync.jsonl"
CLI_MARKS = (".local\\bin", "claude-cli-nodejs", "node_modules", "\\npm\\", "programs\\devin")
Folder = namedtuple("Folder", "parent kind acct org path n")  # bridge-state.json is never copied


def jload(p):
    """Parsed JSON object, or None if missing/unreadable/not an object."""
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def files(d):  # regular files only: never follow symlinks/reparse points out of the index
    return [f for f in d.iterdir() if stat.S_ISREG(f.lstat().st_mode)] if d.is_dir() else []


def uuid_dirs(d):
    if not d.is_dir():
        return []
    return [x for x in d.iterdir() if UUID.match(x.name) and x.is_dir() and not x.is_symlink()]


def newest_ptr(d):
    return max((f.stat().st_mtime for f in files(d) if LOCAL.match(f.name)), default=0)


def discover():
    local = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local")
    roam = Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming")
    cands = [roam / "Claude", *sorted(local.glob("Claude-Data/*")),
             *sorted(local.glob("Packages/*/LocalCache/Roaming/Claude"))]
    parents, seen = [], set()
    for p in cands:
        if p.resolve() not in seen and ((p / MAIN).is_dir() or (p / LEGACY).is_dir()):
            seen.add(p.resolve())
            parents.append(p)
    folders = [Folder(p, kind, a.name, o.name, o, sum(1 for f in files(o) if LOCAL.match(f.name)))
               for p in parents for kind in (MAIN, LEGACY)
               for a in uuid_dirs(p / kind) for o in uuid_dirs(a)]
    return parents, folders


def identity(parents):
    """(account, parent, how) for the signed-in account, or None."""
    def mt(p):
        try:
            return (p / "config.json").stat().st_mtime
        except OSError:
            return 0
    for p in sorted(parents, key=mt, reverse=True):
        a = (jload(p / "config.json") or {}).get("lastKnownAccountUuid")
        if isinstance(a, str) and UUID.match(a):
            return a, p, "config.json"
    a = ((jload(HOME / ".claude.json") or {}).get("oauthAccount") or {}).get("accountUuid")
    if isinstance(a, str) and UUID.match(a):
        home = next((p for p in parents if (p / MAIN / a).is_dir() or (p / LEGACY / a).is_dir()), parents[0])
        return a, home, "~/.claude.json"
    return None


def claude_running():
    """True/False, or None when the process list can't be read."""
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / r"System32\WindowsPowerShell\v1.0\powershell.exe"
    cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'claude.exe' } "
           "| ForEach-Object { '|' + $_.ExecutablePath }")
    try:
        r = subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    paths = [l[1:].strip().lower() for l in r.stdout.splitlines() if l.startswith("|")]
    return any(not p or not any(m in p for m in CLI_MARKS) for p in paths)  # unknown path = assume app


def fold_archived(base, data):
    mine = base.get("archived")
    if not isinstance(mine, list):
        mine = base["archived"] = []
    new = [s for s in dict.fromkeys(data.get("archived") or []) if isinstance(s, str) and s not in mine]
    if new:
        base.setdefault("v", 1)
        mine.extend(new)


def fold_tasks(base, data):
    mine = base.get("scheduledTasks")
    if not isinstance(mine, list):
        mine = base["scheduledTasks"] = []
    seen = {json.dumps(t, sort_keys=True) for t in mine}
    for t in data.get("scheduledTasks") or []:
        if json.dumps(t, sort_keys=True) not in seen:  # dedupe by content; target's own wins
            seen.add(json.dumps(t, sort_keys=True))
            mine.append(t)
    for k, v in data.items():
        base.setdefault(k, v)


def merged_meta(name, target, srcs, fold):
    """New content for target/name, or None if unchanged (or the target's copy is unreadable)."""
    dst = target / name
    base = jload(dst) if dst.exists() else {}
    if base is None:
        print(f"warning: {dst} is unreadable; leaving it alone", file=sys.stderr)
        return None
    before = json.dumps(base, sort_keys=True)
    for s in srcs:
        data = jload(s / name)
        if data:
            fold(base, data)
    return base if json.dumps(base, sort_keys=True) != before else None


def put(dst, src=None, data=None):  # write beside, then rename: a crash never leaves a half file
    tmp = dst.with_name(f".resync-{dst.name}.tmp")
    try:
        if src:
            shutil.copy2(src, tmp)
        else:
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def short(u):
    return u[:8] + "..."


def run(a):
    if not (a.status or a.dry_run or a.force):
        up = claude_running()
        if up:
            print("Quit Claude fully (system tray too) and retry")
            return 1
        if up is None:
            print("Could not tell whether Claude is running. Quit Claude fully (system tray too) and "
                  "retry; use --force only if you are sure it is closed.")
            return 1
    parents, folders = discover()
    if not folders:
        print("No Claude Desktop session index found (looked in %APPDATA%\\Claude, "
              "%LOCALAPPDATA%\\Claude-Data\\*, and MSIX package folders).")
        return 1
    who = identity(parents)
    if not who:
        print("Can't tell which account is signed in (no lastKnownAccountUuid in config.json, "
              "no oauthAccount in ~/.claude.json). Open Claude once, then retry.")
        return 1
    acct, parent, how = who
    orgs = uuid_dirs(parent / MAIN / acct)
    if orgs:
        org = max(orgs, key=lambda d: (newest_ptr(d), d.stat().st_mtime)).name
    else:
        oa = jload(HOME / ".claude.json") or {}
        oa = oa.get("oauthAccount") or {}
        org = oa.get("organizationUuid") if oa.get("accountUuid") == acct else None
        if not (isinstance(org, str) and UUID.match(org)):
            print(f"Account {short(acct)} has no org folder yet. Open Claude on that account once, then retry.")
            return 1
    target = parent / MAIN / acct / org
    srcs = [f.path for f in folders if f.path.resolve() != target.resolve()]

    if a.status or a.dry_run:
        print(f"current account: {short(acct)} (from {how})   target: {short(acct)}/{short(org)}")
        for f in sorted(folders, key=lambda f: -f.n):
            tag = "  <-- target" if f.path.resolve() == target.resolve() else ""
            if not (f.n or tag):
                continue  # skip empty folders
            print(f"  {short(f.acct)}/{short(f.org)}  {f.n:4d} pointers  [{f.kind}]{tag}")
        if len(orgs) > 1:
            print(f"note: account has {len(orgs)} org folders; using the one with the newest pointers")
        if a.status:
            up = claude_running()
            print("Claude Desktop running:", "unknown" if up is None else "yes" if up else "no")
            return 0

    tombs = {m.group(1) for d in [target, *srcs] for f in files(d) if (m := DELETED.match(f.name))}
    have = {f.name for f in files(target)}
    copy, tcopy = {}, {}
    for d in srcs:
        for f in files(d):
            if f.name in have:
                continue
            if (m := LOCAL.match(f.name)) and m.group(1) not in tombs:
                copy.setdefault(f.name, f)
            elif DELETED.match(f.name):
                tcopy.setdefault(f.name, f)
    stale = [f for f in files(target) if (m := LOCAL.match(f.name)) and m.group(1) in tombs]
    meta = {n: c for n, fold in (("archived-sessions.idx", fold_archived), ("scheduled-tasks.json", fold_tasks))
            if (c := merged_meta(n, target, srcs, fold)) is not None}
    need_wait = not (target / "waiting-input").is_dir()
    changed = bool(copy or tcopy or stale or meta or need_wait)

    if a.dry_run:
        print(f"plan: copy {len(copy)} pointers + {len(tcopy)} tombstones, remove {len(stale)} stale, "
              f"update {', '.join(meta) or 'no'} metadata")
        print("nothing to do." if not changed else "(dry run: nothing written)")
        return 0

    target.mkdir(parents=True, exist_ok=True)
    (target / "waiting-input").mkdir(exist_ok=True)
    for f in [*copy.values(), *tcopy.values()]:
        put(target / f.name, src=f)
    for f in stale:
        f.unlink()
    for n, c in meta.items():
        put(target / n, data=c)

    ids = {f.stem for f in (HOME / ".claude" / "projects").glob("*/*.jsonl")}
    sids = [p.get("cliSessionId") for f in files(target) if LOCAL.match(f.name) and (p := jload(f))]
    sids = [s for s in sids if isinstance(s, str) and s]
    print(f"copied {len(copy)} pointers, removed {len(stale)} stale; "
          f"transcripts resolve on disk: {sum(s in ids for s in sids)}/{len(sids)}")
    if not changed:
        print("already in sync.")
        return 0
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "target": f"{acct}/{org}",
                                 "sources": len(srcs), "copied": len(copy), "tombstones": len(tcopy),
                                 "removed_stale": len(stale), "metadata": list(meta),
                                 "forced": a.force}) + "\n")
    except OSError as e:
        print(f"warning: audit log not written: {e}", file=sys.stderr)
    print("Done. Reopen Claude Desktop - the sidebar reads this index at launch.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--status", action="store_true", help="show index folders and the target")
    ap.add_argument("--force", action="store_true", help="skip the Claude-is-running check")
    try:
        return run(ap.parse_args())
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
