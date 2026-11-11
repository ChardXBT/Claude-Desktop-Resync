# Claude Resync

Makes every Claude Desktop account on your Windows PC show the **same chats, chat history and
Projects**, so you can switch accounts the way you do in Codex or Devin and keep working.

## Why your chats "disappear"

Claude Desktop files its sidebar per account+org, in two folders under its app data:

| What | Where (`<app-data>` = `%LOCALAPPDATA%\Claude-Data\claude_*`, `%APPDATA%\Claude` or the MSIX folder) |
|---|---|
| Code chats | `claude-code-sessions\<account>\<org>\local_<id>.json` (pointer; the transcript is in `~\.claude\projects`, shared by every account) |
| Cowork chats | `local-agent-mode-sessions\<account>\<org>\local_<id>.json` **plus** a `local_<id>\` folder holding that chat's transcript and files |
| Projects | `local-agent-mode-sessions\<account>\<org>\spaces.json` |
| Archive, scheduled tasks, backlog | `archived-sessions.idx`, `scheduled-tasks.json`, `backlog\tasks.json` in those folders |

Switching accounts relaunches the app on the other account's folders, so everything looks gone.
The chats in a Code project folder are grouped from those pointers, so a missing pointer is a
missing chat in that project.

## What Resync does

It mirrors every account+org folder to the same content, files included:

- **Chats:** each chat pointer and each Cowork chat folder (transcript, outputs, uploads) is
  copied to every account. If a chat changed in two places, the newest copy wins.
- **Deletions follow you:** deleting a chat (the app leaves a `deleted_<id>` marker), deleting a
  Project, or unarchiving a chat in one account does the same in every account.
- **Projects** are merged by id; the newer edit wins. Entries that don't match the app's strict
  Project format are skipped, because one bad entry makes the app reject the whole file.
- **Never touched:** your transcripts in `~\.claude\projects`, `bridge-state.json` (remote-control
  bridges belong to one account), `cowork-gb-cache.json`, and the app's `spaces-present` keys.
- Nothing leaves your machine.

**Safe while Claude is open.** The signed-in account's folders are add-only: Resync never
overwrites or deletes anything there, because the running app owns them. It only adds what is
missing, and reads them to carry your new chats, edits and deletions to the other accounts.
When you switch, the app relaunches on an account that is already up to date.

## Setup

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/) (keep the **py launcher** option ticked).
2. Download this repo (green **Code** button, then **Download ZIP**) and unzip it somewhere permanent.
3. Double-click **Enable Auto-Sync**. It starts the background mirror now and at every Windows
   sign-in (a shortcut in your Startup folder running `pythonw resync.py --watch`). Undo with
   `py resync.py --uninstall`.
4. Once, to fix the account you are signed into right now: quit Claude completely (system tray
   too), double-click **Claude Resync**, reopen Claude. After that, the mirror keeps every
   account current and you can switch freely.

Optional: right-click `Claude Resync.cmd` > **Send to > Desktop (create shortcut)**.

## Commands

```
py resync.py --status      accounts, chat/project counts per folder, mirror state
py resync.py --dry-run     what a full merge would change; writes nothing
py resync.py               full merge into every account, including the signed-in one (quit Claude first)
py resync.py --watch       mirror continuously (what Enable Auto-Sync runs, every 3 s)
py resync.py --install     / --uninstall   start or stop the mirror at sign-in
```

```
plan: 877 sessions added, 280 session files added, 88 deletions carried, 5 projects merged, ...
every account now has 213 code chats, 4 cowork chats and 2 projects; chats with history on disk: 215/215
```

Logs: one-off merges append to `~\.claude\session-resync.jsonl`; the mirror writes
`~\.claude\session-resync-watch.log`; `~\.claude\session-resync-state.json` remembers what each
folder held, which is how deletions are told apart from items a folder never had.

## Limits

- claude.ai **web** chats and web Projects, and remote-control bridges, live on Anthropic's
  servers per account and do not port.
- While Claude is open, the signed-in account only gains things; an edit or deletion made in
  another account reaches it the next time you switch away and back (or after a full merge with
  Claude closed).
- The very first time you sign into a brand-new account, the app may open before the mirror has
  filled its folders; restart Claude once.
- A Project's folder is just a path on disk; the files inside it are yours and stay where they are.
  A Cowork Project's memory inside the Cowork VM is not copied.
