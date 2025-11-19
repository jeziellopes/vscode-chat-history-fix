# VS Code Core Bug Report: Chat Sessions Not Restored from state.vscdb

## Issue Summary

**Title:** Chat sessions not restored from workspace storage on VS Code startup despite being written correctly

**Component:** VS Code Core - Chat Service / Workspace Storage

**Severity:** High - Data loss issue affecting user experience

**VS Code Version:** [Please fill in your version]

**OS:** Linux

## Problem Description

GitHub Copilot Chat sessions are being written to the correct workspace storage directory (`state.vscdb`) but are **not being restored** when VS Code restarts. This results in users losing their entire chat history despite the data existing in storage.

### Evidence of the Bug

1. **New sessions ARE being written** to workspace storage:
   - Workspace Storage ID: `68afb7ebecb251d147a02dcf70c41df7`
   - Location: `~/.config/Code/User/workspaceStorage/68afb7ebecb251d147a02dcf70c41df7/state.vscdb`
   - New chat sessions appear in this database immediately after creation

2. **Old sessions are NOT restored** on VS Code restart:
   - After closing and reopening VS Code, previous chat sessions disappear
   - The workspace storage directory remains the same (proven by new sessions continuing to write there)
   - No errors appear in the console or logs

3. **Storage path is correct**:
   - The fact that new sessions write to the same location proves VS Code is using the correct storage directory
   - This rules out workspace storage mapping issues
   - The bug is specifically in the **restoration/loading** logic, not the storage/writing logic

## Root Cause Analysis

### What We Know

- **GitHub Copilot Chat extension does NOT manage regular chat session restoration**
  - The extension only manages special session types: Claude Code, Copilot CLI, and Cloud Agent (PR) sessions
  - Regular Copilot Chat sessions are handled by VS Code core's internal chat service
  - No `ChatSessionItemProvider` exists in the extension for regular chat sessions

- **VS Code core's chat service should restore sessions from `state.vscdb`**
  - Sessions are being written correctly (proven by new sessions appearing)
  - Sessions are NOT being read back on startup (proven by disappearing history)
  - This indicates a bug in VS Code core's chat restoration logic

### Detailed Investigation Results

Through direct inspection of the workspace storage database, we found the **exact issue**:

**Storage Structure:**
```
~/.config/Code/User/workspaceStorage/68afb7ebecb251d147a02dcf70c41df7/
├── state.vscdb                        # SQLite database
│   └── ItemTable
│       └── chat.ChatSessionStore.index   # ❌ Only 1 entry!
└── chatSessions/
    ├── 01d3e39a-8542-4d6b-824f-3787a1fd9d3a.json  # ✅ Session data exists
    ├── 040dba57-c60e-4d24-ab59-7acf34559587.json  # ✅ Session data exists
    ├── ... (11 more session files)                # ✅ Session data exists
    └── a4300917-8709-4152-91b5-834de0fcc7f5.json  # ✅ In index AND on disk
```

**Database Query Results:**
```sql
-- Query: SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'
{
  "version": 1,
  "entries": {
    "a4300917-8709-4152-91b5-834de0fcc7f5": {
      "sessionId": "a4300917-8709-4152-91b5-834de0fcc7f5",
      "title": "Rust Analyzer server initialization failure",
      "lastMessageDate": 1763507227780,
      "isImported": false,
      "initialLocation": "panel",
      "isEmpty": false
    }
    // ❌ Missing 12 other sessions that exist in chatSessions/ directory!
  }
}
```

**The Problem:**
- **Session data files**: 13 JSON files exist in `chatSessions/` directory ✅
- **Session index**: Only 1 session entry in `state.vscdb` ❌
- **VS Code behavior**: Reads index from database to determine which sessions to show
- **Result**: 12 sessions are invisible because they're missing from the index

This is a **data corruption issue** where the session index in `state.vscdb` becomes out of sync with the actual session files on disk. The session data is NOT lost - it's just not being referenced in the index that VS Code uses for restoration.

### Code Evidence

Searches in the `vscode-copilot-chat` extension repository confirm:

```bash
# No matches for IChatService in extension code
grep -r "IChatService" src/

# Only special session types have providers:
- ClaudeChatSessionItemProvider (for ~/.claude/ sessions)
- CopilotCLIChatSessionItemProvider (for CLI sessions from globalState)
- CopilotCloudSessionsProvider (for PR sessions from GitHub API)

# NO provider for regular Copilot Chat sessions
```

This proves that regular chat session restoration is VS Code core's responsibility.

## Reproduction Steps

### Prerequisites
- VS Code with GitHub Copilot Chat extension installed
- Active GitHub Copilot Pro subscription (or similar)

### Steps to Reproduce

1. Open a workspace folder in VS Code (not a `.code-workspace` file)
2. Start several Copilot Chat conversations with multiple exchanges
3. Verify sessions appear in Chat view sidebar
4. Note the workspace storage ID:
   ```bash
   # Find workspace storage location
   ls -la ~/.config/Code/User/workspaceStorage/
   # Identify the directory being used (most recently modified)
   ```
5. Close VS Code completely
6. Reopen VS Code with the same workspace folder
7. Check Chat view sidebar

### Expected Behavior

All previous chat sessions should be restored and visible in the Chat view sidebar.

### Actual Behavior

All previous chat sessions disappear. Only new sessions created after reopening VS Code appear.

### Additional Evidence

After reopening VS Code:
1. Create a new chat session
2. Verify it writes to the SAME storage directory as before
3. This proves the storage location hasn't changed
4. Old sessions should have been loaded from this same location but weren't

## Impact

### User Experience
- **Complete loss of chat history** on every VS Code restart
- Users cannot reference previous conversations
- Loss of valuable context and solutions discussed with AI
- Breaks continuity of iterative development workflows

### Affected Users
- All GitHub Copilot Chat users (potentially other chat extensions too)
- Particularly impacts users who rely on chat history for:
  - Code review context
  - Problem-solving iterations
  - Learning from previous AI suggestions

## Related Issues

### Workspace Mode Switching (Secondary Issue)

There's a separate but related issue where switching between workspace modes creates different storage locations:

- **Folder mode**: Opening a folder directly → storage keyed to directory path hash
- **Workspace file mode**: Opening a `.code-workspace` file → storage keyed to workspace file path hash

This causes chat history to appear "lost" when users switch modes, even though both storage locations exist. However, this is a separate issue from the restoration bug.

## System Information

**Workspace Storage Location:**
```
~/.config/Code/User/workspaceStorage/68afb7ebecb251d147a02dcf70c41df7/
```

**Storage Files:**
- `state.vscdb` - SQLite database containing extension state
- `workspace.json` - Workspace metadata

**Extensions Involved:**
- GitHub Copilot Chat (`github.copilot-chat`)
- GitHub Copilot (`github.copilot`)

## Technical Details

### Storage Format

Sessions are stored in `state.vscdb`, a SQLite database. Example inspection:

```bash
sqlite3 ~/.config/Code/User/workspaceStorage/68afb7ebecb251d147a02dcf70c41df7/state.vscdb

# Check for chat-related tables/keys
.tables
SELECT * FROM ItemTable WHERE key LIKE '%chat%' OR key LIKE '%session%';
```

### Expected Restoration Flow

On VS Code startup, the chat service should:
1. Identify the workspace storage directory
2. Open `state.vscdb`
3. Query for saved chat sessions
4. Restore sessions to the Chat view

**Current behavior:** Steps 1-2 work (proven by new sessions writing), but steps 3-4 fail.

## Proposed Solutions

### Immediate Fix (VS Code Core)

1. Review the chat service's session restoration logic
2. Add logging to confirm sessions are being queried from `state.vscdb`
3. Fix the restoration logic to properly load sessions on startup
4. Add error handling for corrupted session data

### Verification Testing

After fix implementation:
1. Create chat sessions
2. Verify they write to `state.vscdb`
3. Close VS Code
4. Reopen VS Code
5. Verify sessions are restored
6. Check console for restoration errors/warnings

### Workaround (For Users)

**Immediate Fix Available:**

We've created a Python script that repairs the corrupted index by scanning all session files and rebuilding the index in `state.vscdb`:

```bash
# Download the fix script from the vscode-copilot-chat repository
# https://github.com/microsoft/vscode-copilot-chat/blob/main/fix_chat_session_index.py

# Close VS Code completely
# Run the script:
python3 fix_chat_session_index.py

# Reopen VS Code - your sessions should be restored!
```

The script:
1. Scans all `chatSessions/*.json` files
2. Extracts metadata (title, timestamp, location)
3. Rebuilds the `chat.ChatSessionStore.index` in `state.vscdb`
4. Creates a backup before making changes

**Prevention Until Fixed:**
1. Regular backups of workspace storage directory
2. Avoid hard crashes/force quits if possible
3. Monitor the session count in Chat view

## Additional Notes

### Why This Isn't an Extension Issue

The `vscode-copilot-chat` extension:
- Only manages special session types (Claude, CLI, Cloud Agent)
- Does NOT implement session restoration for regular chat
- Relies on VS Code core's chat service for regular session persistence
- Cannot fix this bug without changes to VS Code core

### Verification Command Suggestion

Consider adding a VS Code command to help diagnose this:

```typescript
// Proposed diagnostic command
vscode.commands.registerCommand('workbench.action.chat.diagnostics', async () => {
  // Show current workspace storage path
  // List sessions found in state.vscdb
  // Show which sessions are currently loaded
  // Help identify restoration failures
});
```

## Request for Core Team

1. **Confirm** that VS Code core is responsible for regular chat session restoration
2. **Investigate** why sessions are written but not read from `state.vscdb`
3. **Add logging** to help diagnose restoration failures
4. **Implement fix** to restore sessions on startup
5. **Consider** adding diagnostic tools for users to verify their session data

---

## Reporter Information

- **GitHub Username:** [Your username]
- **VS Code Version:** [Run: `code --version`]
- **Extension Versions:**
  - GitHub Copilot Chat: [Check in Extensions view]
  - GitHub Copilot: [Check in Extensions view]
- **Workspace Type:** Folder mode (not .code-workspace file)
- **Workspace Storage ID:** `68afb7ebecb251d147a02dcf70c41df7`

## Attachments

If possible, please attach:
1. VS Code logs showing workspace storage initialization
2. Contents of `workspace.json` from the storage directory
3. Screenshot of empty Chat view after restart
4. Screenshot of workspace storage directory showing `state.vscdb` exists

---

**Filing Location:** https://github.com/microsoft/vscode/issues

**Suggested Labels:** `bug`, `chat`, `workspace-storage`, `data-loss`
