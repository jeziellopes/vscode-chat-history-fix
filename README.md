
# vscdb-fix

A utility to repair corrupted chat session indices in VS Code workspace storage databases (`state.vscdb`).


> **Problem:** VS Code chat sessions become invisible due to index corruption in `state.vscdb`, even though session data files remain intact on disk.
> **Solution:** vscdb-fix scans session files and rebuilds the database index to restore visibility of all chat sessions.

## Quick Start

### Step 1: Preview Changes

```bash
python3 fix_chat_history.py --dry-run
```

Displays affected workspaces and sessions to be restored without modifying any files.

### Step 2: Apply the Fix

**Close VS Code completely**, then run:

```bash
python3 fix_chat_history.py
```

### Step 3: Verify

Restart VS Code and verify sessions appear in the Chat view.

### VS Code Insiders

For VS Code Insiders users, add the `--insiders` flag:

```bash
python3 fix_chat_history.py --insiders --dry-run
python3 fix_chat_history.py --insiders
```

---

## Technical Overview

### Storage Architecture

VS Code's core chat service (not the GitHub Copilot extension) manages regular chat sessions using the following structure:

```
~/.config/Code/User/workspaceStorage/<workspace-id>/
├── state.vscdb                    # SQLite database
│   ├── chat.ChatSessionStore.index  # Index of all sessions
│   ├── agentSessions.model.cache    # Agent panel session list
│   └── agentSessions.state.cache    # Agent panel read/archive state
└── chatSessions/
    ├── session-1.json             # Legacy full JSON format
    ├── session-2.jsonl            # Newer JSONL mutation log format
    └── session-3.json
```

**Session File Formats:**
- `.json` — Legacy format: full conversation as a single JSON object
- `.jsonl` — Newer format (JSON Lines): mutation log with `kind:0` (initial state), `kind:1` (set mutation), `kind:2` (array splice/push)

**Database Keys:**
- `chat.ChatSessionStore.index` — Session metadata index (title, timestamp, location)
- `agentSessions.model.cache` — Agent/Copilot panel session list (resource URIs, labels, timing)
- `agentSessions.state.cache` — Agent panel read/archive state

**Session Restoration Process:**
- On startup, VS Code reads both `chat.ChatSessionStore.index` and `agentSessions.model.cache` to populate the Chat/Agent panel

### Root Cause

The index in `state.vscdb` can become corrupted or out of sync with actual session files, causing:
- Session data files remain intact on disk
- Index missing entries for existing sessions
- VS Code unable to discover sessions during restoration

**Example scenario:**
- Session files on disk: 13
- Index entries in database: 1
- Sessions visible in UI: 1

### Repair Process

The tool performs the following operations:
1. Scans `chatSessions/` directory for all session JSON files
2. Extracts metadata from each session file
3. Rebuilds `chat.ChatSessionStore.index` in `state.vscdb`
4. Creates timestamped backup before modifications

---

## Usage

### Quick Start (Recommended)

Auto-repair all workspaces that need fixing:

```bash
# Safe preview - see what would be fixed
python3 fix_chat_history.py --dry-run

# Fix everything (asks for confirmation)
python3 fix_chat_history.py

# Fix everything automatically (no prompts)
python3 fix_chat_history.py --yes
```

### List Workspaces

List workspaces that need repair:

```bash
python3 fix_chat_history.py --list
```

To include healthy workspaces as well:

```bash
python3 fix_chat_history.py --list --show-all
```

### Repair Specific Workspace

If you want to fix only one workspace:

```bash
# Fix a specific workspace by ID
python3 fix_chat_history.py <workspace_id>

# Example
python3 fix_chat_history.py f4c750964946a489902dcd863d1907de
```

### Advanced Options

```bash
# Show all workspaces, including healthy ones
python3 fix_chat_history.py --list --show-all

# Recover orphaned sessions from other workspaces
python3 fix_chat_history.py --recover-orphans

# Remove orphaned index entries (default: keep them)
python3 fix_chat_history.py --remove-orphans

# Combine flags: recover orphans + auto-confirm
python3 fix_chat_history.py --recover-orphans --yes

# Use VS Code Insiders instead of regular VS Code
python3 fix_chat_history.py --insiders

# Merge duplicate workspace folders (see Machine Migration below)
python3 fix_chat_history.py --merge

# Help and all options
python3 fix_chat_history.py --help
```

---

## Cross-Workspace Orphan Detection 💡

When the tool detects orphaned sessions (entries in the index but no file on disk), it automatically checks **all other workspaces** to see if the session file exists elsewhere.

**🆕 Project Folder Matching:** The tool now intelligently detects if an orphaned session belongs to the same project by comparing folder names!

This helps you:
- **Recover accidentally moved sessions** - If a session was associated with the wrong workspace
- **Identify same-project sessions** - Highlights sessions from the same project folder (e.g., both workspaces have "my-app" in the path)
- **Understand orphaned entries** - Know if they're truly lost or just in the wrong workspace

### Example Output

**Orphan from a different project:**
```
🗑️  Orphaned in index: 2
   💡 Session abc12345... found in workspace a1b2c3d4 (/home/user/other-project)
```

**Orphan from the SAME project (highlighted!):**
```
🗑️  Orphaned in index: 2
   💡 Session def67890... found in workspace e5f6g7h8 (file:///home/user/workspace/my-app)
      ⭐ Same project folder: 'my-app' - likely belongs here!
```

### How It Works

The tool extracts the project folder name from both workspaces and compares:
- Current workspace: `/home/user/workspace/my-app` → Project: `my-app`
- Other workspace: `/home/user/old-workspace/my-app` → Project: `my-app`
- **Match found!** ⭐ These are likely the same project

This is especially helpful when you:
- Switch between different VS Code workspace configurations for the same project
- Have multiple workspace IDs pointing to the same folder
- Moved or renamed your project folder

### Example

```
🗑️  Orphaned in index: 2 (will be kept - use --remove-orphans to remove)
   💡 Session abc12345... found in workspace a1b2c3d4 (/home/user/other-project)
   💡 Session def67890... found in workspace e5f6g7h8 (file:///home/user/workspace/my-app)
      ⭐ Same project folder: 'my-app' - likely belongs here!
```

This means:
- Session `abc12345` is in the index but file missing - found in a **different project**
- Session `def67890` is in the index but file missing - found in the **same project** (my-app)
- The ⭐ marker highlights sessions that likely belong to your current project
- You can copy either file if you want to recover it

### How to Recover Cross-Workspace Sessions

**Method 1: Automatic Recovery (Recommended)** 🆕

```bash
# Automatically copy orphaned sessions from other workspaces
python3 fix_chat_history.py --recover-orphans

# Or for a specific workspace
python3 fix_chat_history.py <workspace-id> --recover-orphans
```

**Method 2: Manual Copy**

```bash
# Copy the session file from the other workspace
cp ~/.config/Code/User/workspaceStorage/<source-workspace-id>/chatSessions/<session-id>.json \
   ~/.config/Code/User/workspaceStorage/<target-workspace-id>/chatSessions/

# Then re-run the repair tool to add it to the index
python3 fix_chat_history.py <target-workspace-id>
```

---

## Important Considerations

### Prerequisites

- Close VS Code completely before running repair scripts to prevent database locks and conflicts

### Safety Features

- Automatic backup creation before any modifications
- Read-only preview mode via `--dry-run` flag
- Index-only modifications - session data files remain untouched
- Zero data loss risk

### System Requirements

- Python 3.6+
- No external dependencies (uses Python standard library only)
- Cross-platform: Linux, macOS, Windows

---

## Example Output

### Preview Mode (--dry-run)

```
🔍 Scanning VS Code workspaces...
   Found 3 workspace(s) with chat sessions

🔧 Found 1 workspace(s) needing repair:

1. Workspace: 68afb7ebecb251d147a02dcf70c41df7
   Folder: /home/user/my-project
   Sessions on disk: 13
   Sessions in index: 1
   ⚠️  Missing from index: 12

📊 Total issues:
   Sessions to restore: 12

🔧 Repairing workspaces...

   Repairing: 68afb7ebecb251d147a02dcf70c41df7 (/home/user/my-project)
      ✅ Will restore 12 session(s)
         • How to fix TypeScript compilation errors (2024-10-28 22:50)
         • Implement user authentication system (2024-10-06 19:25)
         • Debug React component rendering issue (2024-10-07 09:22)
         • Setup PostgreSQL database connection (2024-10-25 11:03)
         • Write unit tests for API endpoints (2024-10-08 16:50)
         ... and 7 more
      🗑️  Orphaned in index: 2 (will be kept - use --remove-orphans to remove)
         💡 Session abc12345... found in workspace a1b2c3d4 (/home/user/other-project)
         💡 Session def67890... found in workspace e5f6g7h8 (/home/user/another-project)

🔍 DRY RUN COMPLETE

To apply these changes, run without --dry-run:
   python3 fix_chat_history.py
```

### Actual Repair

```
✨ REPAIR COMPLETE
   Workspaces repaired: 1
   Total sessions restored: 12

📝 Next Steps:
   1. Start VS Code
   2. Open the Chat view
   3. Your sessions should now be visible!

💾 Backups were created for all modified databases
```

---

## Machine Migration

When transferring VS Code workspace storage from one machine to another (e.g., copying `%APPDATA%\Code\User\` to a new laptop), VS Code may create **new** workspace storage folders with different hashes — even for the same workspace URI. This means:

- Your old sessions exist on disk in the **old** folder
- VS Code reads from a **new** folder (different hash)
- Sessions are invisible in the UI

The `--merge` flag detects these duplicate folders and copies the missing sessions into the active one:

```bash
# Preview what would be merged
python3 fix_chat_history.py --merge --dry-run

# Apply the merge (close VS Code first!)
python3 fix_chat_history.py --merge --yes

# For VS Code Insiders
python3 fix_chat_history.py --merge --insiders --yes
```

This will:
1. Find workspace URIs that have multiple storage folders
2. Identify the active (newest) folder for each
3. Copy missing session files from old folders into the active one
4. Update `chat.ChatSessionStore.index`, `agentSessions.model.cache`, and `agentSessions.state.cache`

---

## Troubleshooting

**No workspaces found**
- Verify VS Code Chat has been used previously
- Confirm workspace storage directory exists: `~/.config/Code/User/workspaceStorage/` (Linux/macOS) or `%APPDATA%\Code\User\workspaceStorage\` (Windows)
- For VS Code Insiders, use `--insiders` flag or check `Code - Insiders` directory

**Sessions not restored after repair**
- Confirm VS Code was completely closed before running the script
- Reload VS Code window: `Ctrl+Shift+P` -> "Reload Window"
- Verify backup file creation was successful
- Check workspace ID matches current project
- If you migrated from another machine, try `--merge` mode first — VS Code may have created new storage folders

**Rollback procedure**
- Locate backup: `state.vscdb.backup.<timestamp>`
- Replace current database: `cp state.vscdb.backup.<timestamp> state.vscdb`

---

## Upstream Issue

This is a VS Code core bug, not a GitHub Copilot extension issue. The Copilot extension manages only specialized sessions (Claude Code, Copilot CLI, PR sessions) - regular chat session restoration is handled by VS Code's core chat service.

**Analysis:**
- `chat.ChatSessionStore.index` in `state.vscdb` becomes desynchronized from session files
- Write operations succeed but read/restoration logic fails
- Likely race condition in VS Code's chat service initialization

### Reporting

- Technical details: See `VSCODE_CORE_BUG_REPORT.md`
- File issues: https://github.com/microsoft/vscode/issues

---

## FAQ

**Can sessions be transferred between workspaces?**  
Yes. Session files are standard JSON or JSONL. Copy files between workspace `chatSessions/` directories, then run the repair script to update the index.

**How do I fix sessions after migrating to a new machine?**  
Use `python3 fix_chat_history.py --merge` — this detects duplicate workspace storage folders (old vs new hashes) and merges sessions into the active one.

**Does this work with VS Code Insiders?**  
Yes. Add the `--insiders` flag to any command, e.g., `python3 fix_chat_history.py --insiders --dry-run`.

**Folder mode vs workspace file (.code-workspace) storage?**  
Different workspace modes use distinct storage locations. Chat histories exist in both locations but are isolated by workspace context.

**Does this tool delete any data?**  
No. Only the database index is modified. Session data files are read-only operations.

**What are orphaned index entries?**  
Index references to non-existent session files. Retained by default for safety (e.g., temporarily unmounted drives). Use `--remove-orphans` to clean up.

---

## Use Cases

Addresses the following symptoms:
- Chat history disappears after VS Code restart
- Previously visible sessions no longer appear in Chat view
- Session count mismatch between filesystem and UI
- Workspace migration with incomplete session restoration

## Contributing

Bug reports and improvements welcome via issues or pull requests.

## License

MIT

## Support

For issues, provide:
- OS and VS Code version
- Output from `--dry-run` mode
- Complete error messages and stack traces
