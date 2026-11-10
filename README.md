# Claude Resync

Restores Claude Desktop's Code/Cowork chat sidebar after you switch accounts (Windows).

The sidebar lists pointer files from `claude-code-sessions\<account>\<org>\`, one folder per
account. Transcripts (`~\.claude\projects\...`) are shared, so switching accounts only changes
which pointers the app sees. Resync **copies** every account's pointers (plus tombstones,
archived list and scheduled tasks) into the account you are signed into. Source folders are
never modified, and `bridge-state.json` is never copied. Nothing leaves your machine.

## Setup

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/) (keep the **py launcher** option ticked).
2. Download this repo (green **Code** button, then **Download ZIP**) and unzip it anywhere.
3. Optional Desktop shortcut: right-click `Claude Resync.cmd`, then **Send to > Desktop (create shortcut)**.
4. Check it works: `py resync.py --status` (lists your account folders; changes nothing).

## Usage

1. Sign into the other account in Claude Desktop.
2. Quit Claude completely (check the system tray too).
3. Double-click **Claude Resync**.
4. Reopen Claude.

Safe to repeat; a second run prints `already in sync.` Options: `--status`, `--dry-run`, `--force`
(skip the running check; the app can overwrite the merge on exit). Each change appends a line to
`~\.claude\session-resync.jsonl`.

```
copied 12 pointers, removed 3 stale; transcripts resolve on disk: 201/206
```

## Limits

- claude.ai web chats, Projects and remote-control bridges are bound to the account and do not port.
- Sessions whose transcript is gone from `~\.claude\projects` appear but cannot resume.
- If an account has several org folders, the one with the newest pointer files is used.
