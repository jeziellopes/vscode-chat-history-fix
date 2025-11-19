#!/usr/bin/env python3
"""
VS Code Chat Session Index Repair Tool v3 - Automatic Repair
=============================================================

This script automatically finds and repairs ALL VS Code workspaces that have
missing chat sessions in their index.

Features:
- Scans all VS Code workspaces
- Detects index corruption (sessions on disk vs sessions in index)
- Automatically repairs all corrupted workspaces
- Shows detailed report of what was fixed

Usage:
    python3 fix_chat_session_index_v3.py [--dry-run] [--yes]

Options:
    --dry-run    Show what would be fixed without making changes
    --yes        Skip confirmation prompts and fix everything

IMPORTANT: Close VS Code completely before running this script!
"""

import json
import sqlite3
import shutil
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Set

class WorkspaceInfo:
    def __init__(self, workspace_dir: Path):
        self.path = workspace_dir
        self.id = workspace_dir.name
        self.sessions_dir = workspace_dir / "chatSessions"
        self.db_path = workspace_dir / "state.vscdb"

        # Load workspace metadata
        workspace_json = workspace_dir / "workspace.json"
        self.folder = None
        if workspace_json.exists():
            try:
                with open(workspace_json, 'r') as f:
                    info = json.load(f)
                    if 'folder' in info:
                        folder = info['folder']
                        if isinstance(folder, str):
                            self.folder = folder
                        elif isinstance(folder, dict) and 'path' in folder:
                            self.folder = folder['path']
            except:
                pass

        # Get session IDs from disk
        self.sessions_on_disk: Set[str] = set()
        if self.sessions_dir.exists():
            for session_file in self.sessions_dir.glob("*.json"):
                self.sessions_on_disk.add(session_file.stem)

        # Get session IDs from index
        self.sessions_in_index: Set[str] = set()
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
                ).fetchone()
                conn.close()

                if row:
                    index = json.loads(row[0])
                    self.sessions_in_index = set(index.get("entries", {}).keys())
            except:
                pass

    @property
    def missing_from_index(self) -> Set[str]:
        """Session files that exist but aren't in the index."""
        return self.sessions_on_disk - self.sessions_in_index

    @property
    def orphaned_in_index(self) -> Set[str]:
        """Index entries that don't have corresponding files."""
        return self.sessions_in_index - self.sessions_on_disk

    @property
    def needs_repair(self) -> bool:
        """True if the workspace has corrupted index."""
        return len(self.missing_from_index) > 0 or len(self.orphaned_in_index) > 0

    @property
    def has_sessions(self) -> bool:
        """True if workspace has any session files."""
        return len(self.sessions_on_disk) > 0

def scan_workspaces() -> List[WorkspaceInfo]:
    """Scan all VS Code workspaces and return their info."""
    storage_root = Path.home() / ".config/Code/User/workspaceStorage"

    if not storage_root.exists():
        return []

    workspaces = []
    for workspace_dir in storage_root.iterdir():
        if workspace_dir.is_dir():
            try:
                ws = WorkspaceInfo(workspace_dir)
                if ws.has_sessions:  # Only include workspaces with sessions
                    workspaces.append(ws)
            except Exception as e:
                print(f"⚠️  Warning: Failed to scan {workspace_dir.name}: {e}")

    return workspaces

def repair_workspace(workspace: WorkspaceInfo, dry_run: bool = False, show_details: bool = False) -> Dict:
    """Repair a workspace's chat session index."""
    result = {
        'success': False,
        'sessions_restored': 0,
        'sessions_removed': 0,
        'error': None,
        'restored_sessions': []
    }

    try:
        # Build new index from all session files
        entries = {}

        for session_id in sorted(workspace.sessions_on_disk):
            session_file = workspace.sessions_dir / f"{session_id}.json"

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

                # Track if this session will be restored
                if session_id in workspace.missing_from_index:
                    result['restored_sessions'].append({
                        'id': session_id,
                        'title': title,
                        'date': last_message_date
                    })

            except Exception as e:
                print(f"      ⚠️  Failed to read {session_id}: {e}")

        if not dry_run:
            # Create backup
            backup_path = str(workspace.db_path) + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(workspace.db_path, backup_path)

            # Update database
            new_index = {
                "version": 1,
                "entries": entries
            }

            conn = sqlite3.connect(workspace.db_path)
            cursor = conn.cursor()

            index_json = json.dumps(new_index, separators=(',', ':'))
            cursor.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                ('chat.ChatSessionStore.index', index_json)
            )

            conn.commit()
            conn.close()

        result['success'] = True
        result['sessions_restored'] = len(workspace.missing_from_index)
        result['sessions_removed'] = len(workspace.orphaned_in_index)

    except Exception as e:
        result['error'] = str(e)

    return result

def main():
    dry_run = '--dry-run' in sys.argv
    auto_yes = '--yes' in sys.argv

    print()
    print("=" * 70)
    print("VS Code Chat Session Index Repair Tool v3 - Automatic Repair")
    print("=" * 70)
    print()

    if dry_run:
        print("🔍 DRY RUN MODE - No changes will be made")
        print()

    # Scan all workspaces
    print("🔍 Scanning VS Code workspaces...")
    workspaces = scan_workspaces()

    if not workspaces:
        print("No workspaces with chat sessions found.")
        return 0

    print(f"   Found {len(workspaces)} workspace(s) with chat sessions")
    print()

    # Find workspaces that need repair
    needs_repair = [ws for ws in workspaces if ws.needs_repair]

    if not needs_repair:
        print("✅ All workspaces are healthy! No repairs needed.")
        return 0

    # Display workspaces that need repair
    print(f"🔧 Found {len(needs_repair)} workspace(s) needing repair:")
    print()

    total_missing = 0
    total_orphaned = 0

    for i, ws in enumerate(needs_repair, 1):
        print(f"{i}. Workspace: {ws.id}")
        if ws.folder:
            print(f"   Folder: {ws.folder}")
        print(f"   Sessions on disk: {len(ws.sessions_on_disk)}")
        print(f"   Sessions in index: {len(ws.sessions_in_index)}")

        if ws.missing_from_index:
            print(f"   ⚠️  Missing from index: {len(ws.missing_from_index)}")
            total_missing += len(ws.missing_from_index)

        if ws.orphaned_in_index:
            print(f"   🗑️  Orphaned in index: {len(ws.orphaned_in_index)}")
            total_orphaned += len(ws.orphaned_in_index)

        print()

    print(f"📊 Total issues:")
    print(f"   Sessions to restore: {total_missing}")
    print(f"   Orphaned entries to remove: {total_orphaned}")
    print()

    # Confirm before proceeding
    if not dry_run and not auto_yes:
        print("⚠️  This will modify the database for these workspaces.")
        print("   Backups will be created before making changes.")
        print()
        response = input("Proceed with repair? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print()
            print("❌ Aborted.")
            return 1
        print()

    # Repair all workspaces
    print("🔧 Repairing workspaces...")
    print()

    success_count = 0
    fail_count = 0
    all_restored_sessions = []

    for ws in needs_repair:
        folder_display = f" ({ws.folder})" if ws.folder else ""
        print(f"   Repairing: {ws.id}{folder_display}")

        result = repair_workspace(ws, dry_run=dry_run, show_details=dry_run)

        if result['success']:
            if result['sessions_restored'] > 0:
                print(f"      ✅ Will restore {result['sessions_restored']} session(s)" if dry_run else f"      ✅ Restored {result['sessions_restored']} session(s)")

                # Show session details in dry-run mode
                if dry_run and result['restored_sessions']:
                    all_restored_sessions.extend(result['restored_sessions'])
                    for session in result['restored_sessions'][:5]:  # Show first 5
                        title = session['title'][:60] + "..." if len(session['title']) > 60 else session['title']
                        date_str = ""
                        if session['date'] > 0:
                            dt = datetime.fromtimestamp(session['date'] / 1000)
                            date_str = f" ({dt.strftime('%Y-%m-%d %H:%M')})"
                        print(f"         • {title}{date_str}")

                    if len(result['restored_sessions']) > 5:
                        print(f"         ... and {len(result['restored_sessions']) - 5} more")

            if result['sessions_removed'] > 0:
                print(f"      🗑️  Will remove {result['sessions_removed']} orphaned entr(y|ies)" if dry_run else f"      🗑️  Removed {result['sessions_removed']} orphaned entr(y|ies)")
            success_count += 1
        else:
            print(f"      ❌ Failed: {result['error']}")
            fail_count += 1

        print()

    # Summary
    print("=" * 70)
    if dry_run:
        print("🔍 DRY RUN COMPLETE")
    else:
        print("✨ REPAIR COMPLETE")
    print("=" * 70)
    print()
    print(f"📊 Results:")
    print(f"   Workspaces repaired: {success_count}")
    if fail_count > 0:
        print(f"   Failed: {fail_count}")
    print(f"   Total sessions restored: {total_missing}")
    if total_orphaned > 0:
        print(f"   Total orphaned entries removed: {total_orphaned}")
    print()

    if not dry_run:
        print("📝 Next Steps:")
        print("   1. Start VS Code")
        print("   2. Open the Chat view")
        print("   3. Your sessions should now be visible!")
        print()
        print("💾 Backups were created for all modified databases")
        print("   (in case you need to restore)")
        print()
    else:
        print("To apply these changes, run without --dry-run:")
        print(f"   python3 {sys.argv[0]}")
        print()

    return 0 if fail_count == 0 else 1

if __name__ == "__main__":
    if '--help' in sys.argv or '-h' in sys.argv:
        print(__doc__)
        sys.exit(0)

    print()

    if '--dry-run' not in sys.argv:
        print("⚠️  IMPORTANT: Please close VS Code completely before continuing!")
        print()

        if '--yes' not in sys.argv:
            response = input("Have you closed VS Code? (yes/no): ").strip().lower()
            if response not in ['yes', 'y']:
                print()
                print("❌ Aborted. Please close VS Code and run this script again.")
                sys.exit(1)
            print()

    exit_code = main()
    sys.exit(exit_code)
