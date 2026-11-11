#!/usr/bin/env python3
"""Claude Resync: make every Claude Desktop account on this PC show the same chats and Projects.

Claude Desktop files its sidebar per account+org, in two trees under its app-data folder:
  claude-code-sessions/<acct>/<org>/       Code sessions (local_<id>.json pointers; the transcripts
                                           live in ~/.claude/projects and are shared already)
  local-agent-mode-sessions/<acct>/<org>/  Cowork sessions (pointer + a local_<id>/ folder holding
                                           that session's transcript and files) and Projects
                                           (spaces.json)
Switching accounts relaunches the app on the other account's folders, so its history looks gone.
Resync mirrors every account+org folder to the same content, files included:

  py resync.py              one full merge into every account (quit Claude first)
  py resync.py --watch      keep mirroring in the background, safe while Claude runs
  py resync.py --install    run --watch at every sign-in to Windows, and start it now
  py resync.py --uninstall  stop it and remove it from sign-in
  --status, --dry-run, --force (skip the "Claude is running" check of a one-off merge)

While Claude runs, the signed-in account's folders are add-only: resync never overwrites or
deletes anything there, because the running app owns them. It only adds what is missing, and it
reads them to learn about new chats, edits and deletions, which it carries to every other account.
"""
import argparse, json, os, re, shutil, stat, subprocess, sys, time
from pathlib import Path

HOME = Path.home()
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
LOCAL = re.compile(r"^local_([\w-]+)\.json$")
SDIR = re.compile(r"^local_([\w-]+)$")
DELETED = re.compile(r"^deleted_([\w-]+)$")
MAIN, COWORK = "claude-code-sessions", "local-agent-mode-sessions"
SPACES, RSS, ARCH = "spaces.json", "remote-session-spaces.json", "archived-sessions.idx"
WHOLE = {MAIN: ("scheduled-tasks.json", "backlog/tasks.json"), COWORK: ("scheduled-tasks.json",)}
STATE = HOME / ".claude" / "session-resync-state.json"  # what each folder held at the last pass
AUDIT = HOME / ".claude" / "session-resync.jsonl"
WLOG = HOME / ".claude" / "session-resync-watch.log"
LOCK = HOME / ".claude" / "session-resync-watch.lock"
STARTUP = Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming") / \
    r"Microsoft\Windows\Start Menu\Programs\Startup\Claude Resync Watch.lnk"
CLI_MARKS = (".local\\bin", "claude-cli-nodejs", "node_modules", "\\npm\\", "programs\\devin")
# Never mirrored: bridge-state.json (remote-control bridges are bound to one account),
# cowork-gb-cache.json (per-account flags), spaces-present/ (the app's own markers and keys).


# ---------------------------------------------------------------- filesystem helpers
def lp(p):
    """Long-path form for Windows: session folders nest deeper than MAX_PATH (260)."""
    s = str(p)
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s)
    return "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s


def jload(p):
    """Parsed JSON object, or None if missing/unreadable/not an object."""
    try:
        with open(lp(p), encoding="utf-8-sig") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def scan(d):
    """{name: (DirEntry stat, is_dir)} for regular files and real dirs; never follows links."""
    out = {}
    try:
        with os.scandir(lp(d)) as it:
            for e in it:
                st = e.stat(follow_symlinks=False)
                if stat.S_ISREG(st.st_mode):
                    out[e.name] = (st, False)
                elif stat.S_ISDIR(st.st_mode) and not getattr(st, "st_reparse_tag", 0):
                    out[e.name] = (st, True)
    except OSError:
        pass
    return out


def mtime(p):
    try:
        return os.stat(lp(p), follow_symlinks=False).st_mtime
    except OSError:
        return None


def same(a, b):
    try:
        with open(lp(a), "rb") as x, open(lp(b), "rb") as y:
            return x.read() == y.read()
    except OSError:
        return False


class Writer:
    """Every write goes through here: atomic, counted, and skipped in dry-run."""
    def __init__(self, dry):
        self.dry, self.n = dry, {}

    def count(self, what):
        self.n[what] = self.n.get(what, 0) + 1

    def copy(self, src, dst, what):
        self.count(what)
        if self.dry:
            return
        os.makedirs(lp(Path(dst).parent), exist_ok=True)
        tmp = Path(dst).with_name(f".resync-{Path(dst).name}.tmp")
        try:
            shutil.copy2(lp(src), lp(tmp))  # keeps mtime, so equal copies never re-copy
            os.replace(lp(tmp), lp(dst))
        except BaseException:
            try:
                os.unlink(lp(tmp))
            except OSError:
                pass
            raise

    def data(self, dst, data, what):
        self.count(what)
        if self.dry:
            return
        os.makedirs(lp(Path(dst).parent), exist_ok=True)
        tmp = Path(dst).with_name(f".resync-{Path(dst).name}.tmp")
        try:
            with open(lp(tmp), "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=1)
            os.replace(lp(tmp), lp(dst))
        except BaseException:
            try:
                os.unlink(lp(tmp))
            except OSError:
                pass
            raise

    def remove(self, p, what):
        self.count(what)
        if not self.dry:
            os.unlink(lp(p))

    def mkdir(self, p):
        if not self.dry and not os.path.isdir(lp(p)):
            os.makedirs(lp(p), exist_ok=True)


# ---------------------------------------------------------------- discovery
def app_roots():
    local = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData/Local")
    roam = Path(os.environ.get("APPDATA") or HOME / "AppData/Roaming")
    cands = [roam / "Claude", *sorted(local.glob("Claude-Data/*")),
             *sorted(local.glob("Packages/*/LocalCache/Roaming/Claude"))]
    out, seen = [], set()
    for p in cands:
        if p.resolve() not in seen and ((p / MAIN).is_dir() or (p / COWORK).is_dir()):
            seen.add(p.resolve())
            out.append(p)
    return out


def uuid_dirs(d):
    return sorted(n for n, (_, isdir) in scan(d).items() if isdir and UUID.match(n))


def pairs_of(root):
    """Every account+org pair either tree knows, lowercased."""
    ps = set()
    for tree in (MAIN, COWORK):
        for a in uuid_dirs(root / tree):
            ps.update((a.lower(), o.lower()) for o in uuid_dirs(root / tree / a))
    return sorted(ps)


def folder(root, tree, acct, org):
    if tree == COWORK:  # the app uses <acct8>/<org8> when that short folder exists
        short = root / COWORK / acct[:8] / org[:8]
        if os.path.isdir(lp(short)) and not os.path.islink(lp(short)):
            return short
    base = root / tree
    a = next((x for x in uuid_dirs(base) if x.lower() == acct), acct)
    o = next((x for x in uuid_dirs(base / a) if x.lower() == org), org)
    return base / a / o


def signed_in(roots):
    """(account, root) the app is signed into, or None."""
    for r in sorted(roots, key=lambda p: mtime(p / "config.json") or 0, reverse=True):
        a = (jload(r / "config.json") or {}).get("lastKnownAccountUuid")
        if isinstance(a, str) and UUID.match(a):
            return a.lower(), r
    a = ((jload(HOME / ".claude.json") or {}).get("oauthAccount") or {}).get("accountUuid")
    if isinstance(a, str) and UUID.match(a):
        return a.lower(), roots[0]
    return None


def claude_running():
    """True/False, or None when the process list can't be read."""
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / r"System32\WindowsPowerShell\v1.0\powershell.exe"
    cmd = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'claude.exe' } "
           "| ForEach-Object { '|' + $_.ExecutablePath }")
    try:
        r = subprocess.run([str(ps), "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    paths = [l[1:].strip().lower() for l in r.stdout.splitlines() if l.startswith("|")]
    return any(not p or not any(m in p for m in CLI_MARKS) for p in paths)  # unknown path = app


# ---------------------------------------------------------------- mirroring
def mirror_sessions(tree, folders, prot, w, exclude=frozenset()):
    """Pointers, tombstones and (Cowork) session folders: newest copy wins everywhere.

    folders: {pair: path}; prot: pairs that are add-only. Returns {session id: winning path}."""
    listing = {p: scan(d) for p, d in folders.items()}
    tombs = {m.group(1).lower() for l in listing.values() for n in l if (m := DELETED.match(n))}
    win, tfiles, sdirs = {}, {}, {}
    for p, l in listing.items():
        for n, (st, isdir) in l.items():
            if (m := LOCAL.match(n)) and not isdir:
                sid = m.group(1).lower()
                if sid in tombs or sid in exclude:
                    continue
                if n not in win or st.st_mtime > win[n][1]:
                    win[n] = (folders[p] / n, st.st_mtime)
            elif DELETED.match(n) and not isdir:
                tfiles.setdefault(n, folders[p] / n)
            elif tree == COWORK and isdir and (m := SDIR.match(n)) and m.group(1).lower() not in tombs:
                sdirs.setdefault(n, []).append(folders[p] / n)
    trees = {n: newest_files(srcs) for n, srcs in sdirs.items()}  # walk each session folder once
    for p, d in folders.items():
        have, guard = listing[p], p in prot
        for n, (src, t) in win.items():
            if n not in have:
                w.copy(src, d / n, "sessions added")
            elif not guard and have[n][0].st_mtime < t and not same(src, d / n):
                w.copy(src, d / n, "sessions updated")
        for n, src in tfiles.items():
            if n not in have:
                w.copy(src, d / n, "deletions carried")
        if not guard:
            for n in have:
                m = LOCAL.match(n)
                if m and (m.group(1).lower() in tombs or m.group(1).lower() in exclude):
                    w.remove(d / n, "deleted or misfiled pointers removed")
        for n, files in trees.items():
            mirror_tree(files, d / n, guard, w)
    return {LOCAL.match(n).group(1).lower(): src for n, (src, _) in win.items()}


def newest_files(srcs):
    """{relative path: (newest source file, mtime, {folder: mtime})} across copies of one folder."""
    files = {}
    def walk(base, rel):
        for n, (st, isdir) in scan(base / rel if rel else base).items():
            r = f"{rel}/{n}" if rel else n
            if isdir:
                walk(base, r)
            elif not n.startswith(".resync-"):
                src, t, at = files.get(r, (None, -1, {}))
                at[str(base)] = st.st_mtime
                files[r] = (base / r, st.st_mtime, at) if st.st_mtime > t else (src, t, at)
    for s in srcs:
        walk(s, "")
    return files


def mirror_tree(files, dst, guard, w):
    """Mirror a session folder: union of files, newest wins; add-only when guarded."""
    for r, (src, t, at) in files.items():
        cur = at.get(str(dst), None)
        if cur is None:
            cur = mtime(dst / r)  # folder created this pass, or a short/long path alias
        if cur is None:
            w.copy(src, dst / r, "session files added")
        elif not guard and cur < t and not same(src, dst / r):
            w.copy(src, dst / r, "session files updated")


def mirror_whole(name, folders, prot, w):
    """A file the app rewrites as a whole: the newest copy wins (add-only when guarded)."""
    best = max(((d / name, t) for d in folders.values() if (t := mtime(d / name)) is not None),
               key=lambda x: x[1], default=None)
    if not best:
        return
    for p, d in folders.items():
        t = mtime(d / name)
        if t is None:
            w.copy(best[0], d / name, f"{name} added")
        elif p not in prot and t < best[1] and not same(best[0], d / name):
            w.copy(best[0], d / name, f"{name} updated")


def valid_space(s):
    """Mirrors the app's strict Project schema: one bad entry makes it reject the whole file."""
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    return (isinstance(s, dict) and isinstance(s.get("id"), str) and bool(UUID.match(s["id"]))
            and isinstance(s.get("name"), str) and isinstance(s.get("folders"), list)
            and isinstance(s.get("projects"), list) and isinstance(s.get("links", []), list)
            and num(s.get("createdAt")) and num(s.get("updatedAt")))


def stamp(s):
    t = s.get("updatedAt") if isinstance(s, dict) else None
    return t if isinstance(t, (int, float)) and not isinstance(t, bool) else 0


# kind: (file, list key, item key fn, item filter, keep newest?)
SETS = {
    "projects": (SPACES, "spaces", lambda s: s["id"].lower(), valid_space, True),
    "cloud-links": (RSS, "entries", lambda e: e["sessionId"],
                    lambda e: isinstance(e, dict) and isinstance(e.get("sessionId"), str), False),
    "archived": (ARCH, "archived", lambda s: s, lambda s: isinstance(s, str), False),
}


def mirror_set(kind, folders, prot, state, w, names=None):
    """Merge a keyed list across folders. Items removed from any folder since the last pass are
    removed everywhere; items that reappear on their own count as re-added. Folders whose file is
    unreadable are left alone and teach nothing."""
    fname, lkey, key, ok, newest = SETS[kind]
    seen, gone = state.setdefault("seen", {}), set(state.setdefault("removed", {}).get(kind, []))
    cur, base, absent = {}, {}, set()
    for p, d in folders.items():
        f = d / fname
        if mtime(f) is None:  # no file yet: receives the union, but teaches no deletions
            cur[p], base[p] = {}, {}
            absent.add(p)
            continue
        data = jload(f)
        if data is None or not isinstance(data.get(lkey, []), list):
            print(f"warning: {f} is unreadable; leaving it alone", file=sys.stderr)
            continue
        base[p] = data
        cur[p] = {key(i): i for i in data.get(lkey, []) if ok(i)}
    for p, items in cur.items():
        prev = seen.get(f"{kind}|{folders[p]}")
        if prev is not None and p not in absent:
            gone |= set(prev) - set(items)
            gone -= set(items) - set(prev)
    union = {}
    for items in cur.values():
        for k, i in items.items():
            if k not in gone and (k not in union or (newest and stamp(i) > stamp(union[k]))):
                union[k] = i
    for p, items in cur.items():
        if p not in prot:
            mine = [i for i in base[p].get(lkey, []) if ok(i)]
            want = [union[key(i)] for i in mine if key(i) in union]  # keep the folder's order
            want += [i for k, i in union.items() if k not in {key(x) for x in mine}]
            if json.dumps(want, sort_keys=True) != json.dumps(mine, sort_keys=True) and (want or mine):
                bad = [i for i in base[p].get(lkey, []) if not ok(i)]  # never drop the app's data
                extra = {"v": 1} if kind == "archived" else {}
                w.data(folders[p] / fname, {**extra, **base[p], lkey: want + bad}, f"{kind} merged")
                if names is not None and kind == "projects":
                    names.update(i["name"] for k, i in union.items() if k not in items)
                items = {key(i): i for i in want}
        if not w.dry:
            seen[f"{kind}|{folders[p]}"] = sorted(items)
    if not w.dry:
        state["removed"][kind] = sorted(gone)
    return len(union)


def sync(root, acct, guard_signed_in, w, state):
    """One mirror pass over every account+org folder under one app-data root."""
    pairs = pairs_of(root)
    if not pairs:
        return None
    prot = {p for p in pairs if guard_signed_in and p[0] == acct}
    main = {p: folder(root, MAIN, *p) for p in pairs}
    cow = {p: folder(root, COWORK, *p) for p in pairs}
    for d in [*main.values(), *cow.values()]:
        w.mkdir(d)
    for d in main.values():
        w.mkdir(d / "waiting-input")
    cowork_ids = {LOCAL.match(n).group(1).lower() for d in cow.values() for n in scan(d)
                  if LOCAL.match(n)}
    code = mirror_sessions(MAIN, main, prot, w, exclude=cowork_ids)  # Cowork chats live in COWORK
    cw = mirror_sessions(COWORK, cow, prot, w)
    for tree, folders in ((MAIN, main), (COWORK, cow)):
        for name in WHOLE[tree]:
            mirror_whole(name, folders, prot, w)
    mirror_set("archived", main, prot, state, w)
    names = set()
    nproj = mirror_set("projects", cow, prot, state, w, names)
    mirror_set("cloud-links", cow, prot, state, w)
    return {"pairs": pairs, "prot": prot, "code": code, "cowork": cw, "projects": nproj,
            "new_projects": sorted(names), "main": main, "cow": cow}


def history(res):
    """(sessions whose transcript is on disk, sessions with a transcript id)."""
    ids = {f.stem for f in (HOME / ".claude" / "projects").glob("*/*.jsonl")}
    ok = n = 0
    for sid, ptr in res["code"].items():
        c = (jload(ptr) or {}).get("cliSessionId")
        if isinstance(c, str) and c:
            n += 1
            ok += c in ids
    for sid, ptr in res["cowork"].items():
        c = (jload(ptr) or {}).get("cliSessionId")
        if isinstance(c, str) and c:
            n += 1
            proj = Path(ptr).parent / f"local_{sid}" / ".claude" / "projects"
            ok += any(mtime(proj / d / f"{c}.jsonl") is not None
                      for d, (_, isdir) in scan(proj).items() if isdir)
    return ok, n


# ---------------------------------------------------------------- commands
def short(u):
    return u[:8]


def status(roots, who):
    acct = who[0] if who else None
    print(f"signed-in account: {short(acct) if acct else 'unknown'}")
    for r in roots:
        for p in pairs_of(r):
            mp, cp = folder(r, MAIN, *p), folder(r, COWORK, *p)
            nc = sum(1 for n in scan(mp) if LOCAL.match(n))
            nw = sum(1 for n in scan(cp) if LOCAL.match(n))
            npj = sum(map(valid_space, (jload(cp / SPACES) or {}).get("spaces") or []))
            tag = "  <-- signed in" if p[0] == acct else ""
            print(f"  {short(p[0])}/{short(p[1])}  {nc:4d} code chats  {nw:3d} cowork chats  "
                  f"{npj:3d} projects{tag}")
    print("background mirror:", "installed" if STARTUP.exists() else "not installed",
          "/ running" if watch_running() else "/ not running")
    up = claude_running()
    print("Claude Desktop running:", "unknown" if up is None else "yes" if up else "no")


def save_state(state):
    try:
        Writer(False).data(STATE, state, "state")
    except OSError as e:
        print(f"warning: state not saved: {e}", file=sys.stderr)


def once(a):
    roots = app_roots()
    if not roots:
        print("No Claude Desktop data found (looked in %APPDATA%\\Claude, %LOCALAPPDATA%\\Claude-Data\\*, "
              "and MSIX package folders).")
        return 1
    who = signed_in(roots)
    if a.status:
        status(roots, who)
        return 0
    up = None if (a.dry_run or a.force) else claude_running()
    if up or (up is None and not (a.dry_run or a.force)):
        print("Quit Claude fully (system tray too) and retry." if up else
              "Could not tell whether Claude is running. Quit it fully and retry; use --force only "
              "if you are sure it is closed.")
        if watch_running():
            print("(The background mirror is running, so the other accounts are already kept in step.)")
        return 1
    state = jload(STATE) or {}
    w = Writer(a.dry_run)
    total = {}
    live = a.force and claude_running() is not False  # forced while open: keep its folders add-only
    for r in roots:
        acct = who[0] if who and r == who[1] else None
        res = sync(r, acct, live and acct is not None, w, state)
        if res:
            total = res
    if not a.dry_run:
        save_state(state)
    acts = ", ".join(f"{v} {k}" for k, v in sorted(w.n.items())) or "nothing to change"
    print(("plan: " if a.dry_run else "") + acts)
    if total:
        ok, n = history(total)
        print(f"every account now has {len(total['code'])} code chats, {len(total['cowork'])} cowork "
              f"chats and {total['projects']} projects; chats with history on disk: {ok}/{n}")
        if total["new_projects"]:
            print("projects added: " + ", ".join(total["new_projects"]))
    if a.dry_run:
        print("(dry run: nothing written)")
        return 0
    if w.n:
        try:
            AUDIT.parent.mkdir(parents=True, exist_ok=True)
            with AUDIT.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mode": "once",
                                     "changes": w.n, "forced": a.force}) + "\n")
        except OSError as e:
            print(f"warning: audit log not written: {e}", file=sys.stderr)
        print("Done. Open Claude: every account now shows the same chats and Projects.")
    else:
        print("already in sync.")
    return 0


def log(msg):
    try:
        WLOG.parent.mkdir(parents=True, exist_ok=True)
        if WLOG.exists() and WLOG.stat().st_size > 1_000_000:
            os.replace(WLOG, WLOG.with_suffix(".log.1"))
        with WLOG.open("a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def take_lock():
    """An exclusive lock held for the watcher's lifetime, or None if another watcher holds it."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def watch_running():
    fh = take_lock()
    if fh:
        fh.close()
        return False
    return True


def watch(a):
    """Mirror forever. The signed-in account stays add-only even when Claude is closed: switching
    happens inside the app, so treating it as live is always safe and needs no process polling."""
    lock = take_lock()
    if not lock:
        print("The background mirror is already running.")
        return 0
    log(f"watch started (pid {os.getpid()}, every {a.interval}s)")
    state = jload(STATE) or {}
    last_err = None
    while True:
        try:
            roots = app_roots()
            who = signed_in(roots) if roots else None
            w = Writer(False)
            if who:  # unknown sign-in: write nothing rather than risk the live account's folders
                for r in roots:
                    sync(r, who[0] if r == who[1] else None, r == who[1], w, state)
            if w.n:
                save_state(state)
                log(f"signed in {short(who[0]) if who else '?'}: " +
                    ", ".join(f"{v} {k}" for k, v in sorted(w.n.items())))
            last_err = None
        except Exception as e:  # keep running; log each distinct failure once
            msg = f"{type(e).__name__}: {e}"
            if msg != last_err:
                log("error: " + msg)
                last_err = msg
        if a.once_for_test:
            return 0
        time.sleep(a.interval)


def pyw():
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def install(a):
    if os.name != "nt":
        print("--install is for Windows; on other systems run `resync.py --watch` from your login items.")
        return 1
    script = str(Path(__file__).resolve())
    STARTUP.parent.mkdir(parents=True, exist_ok=True)
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{STARTUP}');"
          f"$s.TargetPath='{pyw()}';$s.Arguments='\"{script}\" --watch';"
          f"$s.WorkingDirectory='{Path(script).parent}';$s.WindowStyle=7;"
          "$s.Description='Claude Resync: mirror chats and Projects across accounts';$s.Save()")
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True)
    if r.returncode or not STARTUP.exists():
        print("Could not create the sign-in shortcut:", r.stderr.strip())
        return 1
    if not watch_running():
        subprocess.Popen([pyw(), script, "--watch"], cwd=str(Path(script).parent),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) |
                         getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0), close_fds=True)
        for _ in range(20):
            if watch_running():
                break
            time.sleep(0.25)
    print(f"Background mirror installed: starts at every sign-in ({STARTUP.name}) and is "
          f"{'running' if watch_running() else 'NOT running yet (see ' + str(WLOG) + ')'}.")
    print("Switch accounts in Claude whenever you like; every account keeps the same chats and Projects.")
    return 0


def uninstall(a):
    removed = STARTUP.exists()
    if removed:
        STARTUP.unlink()
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(py|pyw|python|pythonw)(\\d.*)?\\.exe$' "
          "-and $_.CommandLine -like '*resync.py*--watch*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True)
    print(("Removed from sign-in. " if removed else "It was not set to start at sign-in. ") +
          "The background mirror is " + ("still running." if watch_running() else "stopped."))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--status", action="store_true", help="show accounts, counts and the mirror state")
    ap.add_argument("--force", action="store_true", help="skip the Claude-is-running check")
    ap.add_argument("--watch", action="store_true", help="mirror continuously (safe while Claude runs)")
    ap.add_argument("--install", action="store_true", help="run --watch at every sign-in, start it now")
    ap.add_argument("--uninstall", action="store_true", help="stop --watch and remove it from sign-in")
    ap.add_argument("--interval", type=float, default=3.0, help=argparse.SUPPRESS)
    ap.add_argument("--once-for-test", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    try:
        if a.install:
            return install(a)
        if a.uninstall:
            return uninstall(a)
        if a.watch:
            return watch(a)
        return once(a)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
