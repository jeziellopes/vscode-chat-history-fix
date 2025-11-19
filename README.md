# VS Code Chat History Fix

**Lost your VS Code chat history?** This tool can restore it! 🔧

## Quick Fix (Recommended)

### Step 1: Preview What Will Be Restored

```bash
python3 fix_chat_session_index_v3.py --dry-run
```

This shows you exactly which sessions will be restored **without making any changes**. You'll see:
- Which workspaces have issues
- How many sessions will be restored
- Preview of session titles and dates

### Step 2: Apply the Fix

**Close VS Code completely**, then run:

```bash
python3 fix_chat_session_index_v3.py
```

### Step 3: Verify

Reopen VS Code and check the Chat view - your sessions should be back!

---

## What Happened?

Your chat sessions aren't actually lost - they just became invisible due to a corrupted index in VS Code's database. The session files still exist on disk, but VS Code doesn't know to load them.

**This tool rebuilds the index so VS Code can find your sessions again.**

---

## Which Script to Use?

### Option 1: Automatic Fix (Easiest) ⭐

**`fix_chat_session_index_v3.py`** - Finds and fixes all corrupted workspaces automatically.

```bash
# See what would be fixed (safe preview)
python3 fix_chat_session_index_v3.py --dry-run

# Fix everything (asks for confirmation)
python3 fix_chat_session_index_v3.py

# Fix everything automatically (no prompts)
python3 fix_chat_session_index_v3.py --yes
```

### Option 2: Manual Selection

**`fix_chat_session_index_v2.py`** - Choose which workspace to fix.

```bash
# List your workspaces
python3 fix_chat_session_index_v2.py

# Fix a specific workspace
python3 fix_chat_session_index_v2.py <workspace_id>
```

---

## Important Notes

### ⚠️ Close VS Code First!

Always close VS Code **completely** before running these scripts. Otherwise, VS Code might overwrite your fixes.

### ✅ Safe to Use

- Creates automatic backups before making changes
- Only modifies the index, never deletes your session data
- Can preview changes with `--dry-run` mode

### 📋 Requirements

- Python 3.6 or newer
- No installation needed - uses only Python standard library

---

## How It Works

1. **Scans** your VS Code workspace storage for session files
2. **Detects** sessions that exist on disk but are missing from the index
3. **Rebuilds** the index to include all your sessions
4. **Backs up** the database before making any changes

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

🔍 DRY RUN COMPLETE

To apply these changes, run without --dry-run:
   python3 fix_chat_session_index_v3.py
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

## Troubleshooting

**Script says "No workspaces found"**
- Make sure you've used VS Code Chat before
- Check that `~/.config/Code/User/workspaceStorage/` exists

**Sessions still not showing**
- Ensure you closed VS Code before running the script
- Try reloading VS Code window (Ctrl+Shift+P → "Reload Window")
- Check the backup was created successfully

**Want to undo the fix?**
- Find the backup file: `state.vscdb.backup.TIMESTAMP`
- Copy it back to `state.vscdb`

---

## Report the Bug

This is a VS Code core issue, not an extension bug. Help get it fixed permanently:

1. See `VSCODE_CORE_BUG_REPORT.md` for details
2. File at: https://github.com/microsoft/vscode/issues

---

## License

MIT

---

**Need help?** Open an issue with details about your problem.
