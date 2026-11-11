# Claude Resync

Restores Claude Desktop's Code/Cowork chat sidebar **and your Projects** after you switch accounts (Windows).

The sidebar lists pointer files from `claude-code-sessions\<account>\<org>\`, one folder per
account. Transcripts (`~\.claude\projects\...`) are shared, so switching accounts only changes
which pointers the app sees. Resync **copies** every account's pointers (plus tombstones,
archived list and scheduled tasks) into the account you are signed into. Source folders are
never modified, and `bridge-state.json` is never copied. Nothing leaves your machine.

## Projects

Claude Desktop's Projects (internally Cowork "spaces": name, instructions, linked folders) are
local too, but they live in a different tree: `local-agent-mode-sessions\<account>\<org>\spaces.json`.
Each account loads only its own file, so a switch hides them. Resync merges every account's
Projects into the signed-in account's file:

- Matched by project id. If the same project was edited in two accounts, the newer `updatedAt` wins.
- A project you delete in one account stays deleted there (remembered in
  `~\.claude\session-resync-projects.json`). Delete it in each account to remove it everywhere.
- Entries are copied only if they match the app's strict Project schema, because one malformed
  entry makes the app reject the whole file. An unreadable target file is left untouched.
- `remote-session-spaces.json` (cloud sessions linked to a project's folders) is merged too.
- A project's folder is only a path. Files inside it are not copied or moved.

## Setup

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/) (keep the **py launcher** option ticked).
2. Download this repo (green **Code** button, then **Download ZIP**) and unzip it anywhere.
3. Optional Desktop shortcut: right-click `Claude Resync.cmd`, then **Send to > Desktop (create shortcut)**.
4. Check it works: `py resync.py --status` (lists your account folders with session and project counts; changes nothing).

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
projects: added 2, updated 0; this account now has 2 (PHL113 Exam Prep notes, Uni Life Plan)
```

## Limits

- claude.ai **web** chats and web Projects, and remote-control bridges, are bound to the account
  on Anthropic's servers and do not port. (Desktop Projects do; see above.)
- A project's memory inside the Cowork VM is not copied.
- Sessions whose transcript is gone from `~\.claude\projects` appear but cannot resume.
- If an account has several org folders, the one with the newest pointer files is used.
