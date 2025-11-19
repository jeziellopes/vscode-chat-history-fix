#!/usr/bin/env python3
"""
VS Code Chat Session Index Repair Tool
=======================================

This script fixes the issue where VS Code Chat sessions exist on disk but don't
appear in the UI because they're missing from the session index in state.vscdb.

Problem:
- Chat session data exists in: chatSessions/*.json
- Chat session index in: state.vscdb -> chat.ChatSessionStore.index
- If sessions are missing from the index, VS Code won't show them

Solution:
- Scan all session JSON files
- Extract metadata (title, timestamp, etc.)
- Rebuild the chat.ChatSessionStore.index in state.vscdb

Usage:
    python3 fix_chat_session_index.py

IMPORTANT: Close VS Code completely before running this script!
"""

import json
import sqlite3
import shutil
from pathlib import Path
from datetime import datetime

def main():
    print("=" * 70)
    print("VS Code Chat Session Index Repair Tool")
    print("=" * 70)
    print()

    # Workspace storage path - UPDATE THIS if different
    workspace_storage = Path.home() / ".config/Code/User/workspaceStorage/68afb7ebecb251d147a02dcf70c41df7"

    if not workspace_storage.exists():
        print(f"❌ Error: Workspace storage not found: {workspace_storage}")
        print()
        print("Please update the workspace_storage path in this script.")
        return 1

    sessions_dir = workspace_storage / "chatSessions"
    db_path = workspace_storage / "state.vscdb"

    if not db_path.exists():
        print(f"❌ Error: Database not found: {db_path}")
        return 1

    print(f"📁 Workspace Storage: {workspace_storage}")
    print(f"📁 Sessions Directory: {sessions_dir}")
    print(f"📁 Database: {db_path}")
    print()

    # Check current index
    print("🔍 Checking current index...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    row = cursor.execute(
        "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
    ).fetchone()

    current_index = json.loads(row[0]) if row else {"version": 1, "entries": {}}
    current_count = len(current_index.get("entries", {}))

    conn.close()

    print(f"   Current index has {current_count} session(s)")
    print()

    # Scan session files
    if not sessions_dir.exists():
        print(f"⚠️  Warning: Sessions directory doesn't exist: {sessions_dir}")
        print("   No sessions to restore.")
        return 0

    session_files = list(sessions_dir.glob("*.json"))
    print(f"🔍 Found {len(session_files)} session file(s) on disk")
    print()

    if len(session_files) == 0:
        print("ℹ️  No session files found. Nothing to do.")
        return 0

    # Build new index
    print("📝 Building new index...")
    entries = {}
    successful = 0
    failed = 0

    for session_file in sorted(session_files):
        session_id = session_file.stem

        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                session_data = json.load(f)

            # Extract metadata
            title = "Untitled Session"
            last_message_date = 0
            is_empty = True

            if "requests" in session_data and session_data["requests"]:
                is_empty = False
                first_request = session_data["requests"][0]

                # Extract title from message parts
                if "message" in first_request and "parts" in first_request["message"]:
                    text_parts = [
                        p.get("text", "")
                        for p in first_request["message"]["parts"]
                        if "text" in p
                    ]
                    if text_parts:
                        # Clean up the title
                        title = text_parts[0].strip()
                        if len(title) > 100:
                            title = title[:97] + "..."
                        if not title:
                            title = "Untitled Session"

                # Get timestamp from last request
                last_request = session_data["requests"][-1]
                last_message_date = last_request.get("timestamp", 0)

            entries[session_id] = {
                "sessionId": session_id,
                "title": title,
                "lastMessageDate": last_message_date,
                "isImported": False,
                "initialLocation": session_data.get("initialLocation", "panel"),
                "isEmpty": is_empty
            }

            # Show preview
            title_preview = title[:50] + "..." if len(title) > 50 else title
            date_str = ""
            if last_message_date > 0:
                dt = datetime.fromtimestamp(last_message_date / 1000)
                date_str = f" ({dt.strftime('%Y-%m-%d %H:%M')})"

            print(f"   ✅ {session_id}: {title_preview}{date_str}")
            successful += 1

        except Exception as e:
            print(f"   ⚠️  {session_id}: Failed - {e}")
            failed += 1

    print()
    print(f"✅ Successfully indexed: {successful}")
    if failed > 0:
        print(f"⚠️  Failed to index: {failed}")
    print()

    if successful == 0:
        print("❌ No sessions could be indexed. Aborting.")
        return 1

    # Create backup
    backup_path = str(db_path) + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    print(f"📦 Creating backup: {Path(backup_path).name}")
    shutil.copy2(db_path, backup_path)
    print()

    # Build final index structure
    new_index = {
        "version": 1,
        "entries": entries
    }

    # Update database
    print("💾 Updating database...")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    index_json = json.dumps(new_index, separators=(',', ':'))
    cursor.execute(
        "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
        ('chat.ChatSessionStore.index', index_json)
    )

    conn.commit()
    conn.close()

    print()
    print("=" * 70)
    print("✨ SUCCESS! Chat session index has been rebuilt")
    print("=" * 70)
    print()
    print(f"📊 Summary:")
    print(f"   Before: {current_count} session(s) in index")
    print(f"   After:  {len(entries)} session(s) in index")
    print(f"   Restored: {len(entries) - current_count} session(s)")
    print()
    print("📝 Next Steps:")
    print("   1. Start VS Code")
    print("   2. Open the Chat view")
    print("   3. Your sessions should now be visible!")
    print()
    print(f"💾 Backup saved to: {Path(backup_path).name}")
    print("   (in case you need to restore)")
    print()

    return 0

if __name__ == "__main__":
    import sys

    print()
    print("⚠️  IMPORTANT: Please close VS Code completely before continuing!")
    print()

    response = input("Have you closed VS Code? (yes/no): ").strip().lower()
    if response not in ['yes', 'y']:
        print()
        print("❌ Aborted. Please close VS Code and run this script again.")
        sys.exit(1)

    print()
    exit_code = main()
    sys.exit(exit_code)
