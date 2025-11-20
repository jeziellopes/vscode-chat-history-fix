#!/usr/bin/env python3
"""
VS Code Chat Session Index Repair Tool v2
==========================================

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
    python3 fix_chat_session_index_v2.py [workspace_id] [--dry-run] [--remove-orphans] [--yes]

    If workspace_id is not provided, the script will list available workspaces.

Options:
    --dry-run          Show what would be fixed without making changes
    --remove-orphans   Remove orphaned index entries (sessions in index but no file)
                       By default orphaned entries are kept for safety
    --yes              Skip confirmation prompt

IMPORTANT: Close VS Code completely before running this script!
"""

import json
import sqlite3
import shutil
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Set, Dict

def extract_project_name(folder_path: Optional[str]) -> Optional[str]:
    """Extract the project/folder name from a workspace folder path."""
    if not folder_path:
        return None
    
    # Handle URI format (file:///path/to/folder)
    if folder_path.startswith('file://'):
        folder_path = folder_path[7:]  # Remove 'file://'
    
    # Get the last component of the path (the folder name)
    try:
        return Path(folder_path).name
    except:
        return None

def folders_match(folder1: Optional[str], folder2: Optional[str]) -> bool:
    """Check if two workspace folders likely refer to the same project."""
    if not folder1 or not folder2:
        return False
    
    name1 = extract_project_name(folder1)
    name2 = extract_project_name(folder2)
    
    if not name1 or not name2:
        return False
    
    # Case-insensitive comparison
    return name1.lower() == name2.lower()

def get_sessions_from_disk(sessions_dir: Path) -> Set[str]:
    """Get set of session IDs from disk."""
    if not sessions_dir.exists():
        return set()
    return {f.stem for f in sessions_dir.glob("*.json")}

def get_sessions_from_index(db_path: Path) -> Set[str]:
    """Get set of session IDs from database index."""
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
        ).fetchone()
        conn.close()
        if row:
            index = json.loads(row[0])
            return set(index.get("entries", {}).keys())
    except:
        pass
    return set()

def find_session_in_workspaces(session_id: str, exclude_workspace: str, current_workspace_folder: Optional[str] = None) -> Optional[Dict]:
    """Find a session file in other workspaces.
    
    Returns dict with workspace info and whether it matches the current project folder.
    """
    storage_root = Path.home() / ".config/Code/User/workspaceStorage"
    if not storage_root.exists():
        return None
    
    for workspace_dir in storage_root.iterdir():
        if not workspace_dir.is_dir() or workspace_dir.name == exclude_workspace:
            continue
        
        sessions_dir = workspace_dir / "chatSessions"
        session_file = sessions_dir / f"{session_id}.json"
        
        if session_file.exists():
            # Get workspace info
            workspace_json = workspace_dir / "workspace.json"
            folder = None
            if workspace_json.exists():
                try:
                    with open(workspace_json, 'r') as f:
                        info = json.load(f)
                        if 'folder' in info:
                            folder_data = info['folder']
                            if isinstance(folder_data, str):
                                folder = folder_data
                            elif isinstance(folder_data, dict) and 'path' in folder_data:
                                folder = folder_data['path']
                except:
                    pass
            
            same_project = folders_match(current_workspace_folder, folder)
            
            return {
                'workspace_id': workspace_dir.name,
                'folder': folder,
                'session_file': session_file,
                'same_project': same_project
            }
    
    return None

def list_workspaces():
    """List all VS Code workspace storage directories."""
    storage_root = Path.home() / ".config/Code/User/workspaceStorage"

    if not storage_root.exists():
        print(f"❌ Error: VS Code workspace storage not found: {storage_root}")
        return []

    workspaces = []
    for workspace_dir in storage_root.iterdir():
        if workspace_dir.is_dir():
            workspace_json = workspace_dir / "workspace.json"
            sessions_dir = workspace_dir / "chatSessions"
            db_path = workspace_dir / "state.vscdb"

            # Check if this workspace has chat sessions
            session_count = 0
            if sessions_dir.exists():
                session_count = len(list(sessions_dir.glob("*.json")))

            # Read workspace metadata if available
            workspace_info = None
            if workspace_json.exists():
                try:
                    with open(workspace_json, 'r') as f:
                        workspace_info = json.load(f)
                except:
                    pass

            workspaces.append({
                'id': workspace_dir.name,
                'path': workspace_dir,
                'has_db': db_path.exists(),
                'has_sessions': sessions_dir.exists(),
                'session_count': session_count,
                'info': workspace_info
            })

    return workspaces

def print_workspaces(workspaces):
    """Print available workspaces in a readable format."""
    print("Available VS Code Workspaces:")
    print("=" * 70)
    print()

    workspaces_with_sessions = [w for w in workspaces if w['session_count'] > 0]

    if not workspaces_with_sessions:
        print("No workspaces with chat sessions found.")
        return

    for i, ws in enumerate(workspaces_with_sessions, 1):
        print(f"{i}. Workspace ID: {ws['id']}")

        if ws['info'] and 'folder' in ws['info']:
            folder = ws['info']['folder']
            if isinstance(folder, str):
                print(f"   Folder: {folder}")
            elif isinstance(folder, dict) and 'path' in folder:
                print(f"   Folder: {folder['path']}")

        print(f"   Sessions: {ws['session_count']}")
        print(f"   Database: {'✅' if ws['has_db'] else '❌'}")
        print()

def repair_workspace(workspace_path, dry_run: bool = False, remove_orphans: bool = False):
    """Repair the chat session index for a specific workspace.

    dry_run: if True, do not write changes to the database
    remove_orphans: if True, orphaned index entries will be removed;
                    otherwise they will be preserved (kept) in the index
    """
    workspace_storage = Path(workspace_path)

    print("=" * 70)
    print("VS Code Chat Session Index Repair Tool v2")
    print("=" * 70)
    print()

    if not workspace_storage.exists():
        print(f"❌ Error: Workspace storage not found: {workspace_storage}")
        return 1

    sessions_dir = workspace_storage / "chatSessions"
    db_path = workspace_storage / "state.vscdb"

    if not db_path.exists():
        print(f"❌ Error: Database not found: {db_path}")
        return 1

    print(f"📁 Workspace ID: {workspace_storage.name}")
    print(f"📁 Sessions Directory: {sessions_dir}")
    print(f"📁 Database: {db_path}")
    
    # Get workspace folder for project matching
    workspace_json = workspace_storage / "workspace.json"
    current_workspace_folder = None
    if workspace_json.exists():
        try:
            with open(workspace_json, 'r') as f:
                info = json.load(f)
                if 'folder' in info:
                    folder_data = info['folder']
                    if isinstance(folder_data, str):
                        current_workspace_folder = folder_data
                    elif isinstance(folder_data, dict) and 'path' in folder_data:
                        current_workspace_folder = folder_data['path']
        except:
            pass
    
    if current_workspace_folder:
        project_name = extract_project_name(current_workspace_folder)
        if project_name:
            print(f"📁 Project: {project_name}")
    
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
    
    # Check for orphans and cross-workspace sessions
    sessions_on_disk = get_sessions_from_disk(sessions_dir)
    sessions_in_index = get_sessions_from_index(db_path)
    
    missing_from_index = sessions_on_disk - sessions_in_index
    orphaned_in_index = sessions_in_index - sessions_on_disk
    
    if missing_from_index:
        print(f"   ⚠️  Missing from index: {len(missing_from_index)}")
    
    if orphaned_in_index:
        print(f"   🗑️  Orphaned in index: {len(orphaned_in_index)}")
        
        # Check if orphans exist in other workspaces
        recoverable_orphans = {}
        for session_id in orphaned_in_index:
            found_info = find_session_in_workspaces(session_id, workspace_storage.name, current_workspace_folder)
            if found_info:
                recoverable_orphans[session_id] = found_info
                folder_display = f" ({found_info['folder']})" if found_info['folder'] else ""
                
                if found_info['same_project']:
                    # Highlight that it's from the same project
                    project_name = extract_project_name(current_workspace_folder)
                    print(f"      💡 Session {session_id[:8]}... found in workspace {found_info['workspace_id']}{folder_display}")
                    print(f"         ⭐ Same project folder: '{project_name}' - likely belongs here!")
                else:
                    print(f"      💡 Session {session_id[:8]}... found in workspace {found_info['workspace_id']}{folder_display}")
        
        if recoverable_orphans:
            print(f"      🔍 {len(recoverable_orphans)} orphan(s) found in other workspaces")
            print(f"         You can copy these .json files if you want to recover them")
    
    print()

    if len(session_files) == 0:
        print("ℹ️  No session files found. Nothing to do.")
        return 0

    # Build new index
    print("📝 Building new index...")
    entries = {}

    # If not removing orphans, preserve existing index entries and merge
    if not remove_orphans and db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
            ).fetchone()
            conn.close()
            if row:
                existing_index = json.loads(row[0])
                entries = existing_index.get("entries", {}).copy()
        except Exception:
            # If we fail to read existing index, continue and rebuild from disk
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

    # Create backup and update database (unless dry_run)
    new_index = {
        "version": 1,
        "entries": entries
    }

    # initialize backup_path for static analyzers
    backup_path = None

    if dry_run:
        print("(dry-run) Would create backup and write updated index to database")
    else:
        backup_path = str(db_path) + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        print(f"📦 Creating backup: {Path(backup_path).name}")
        shutil.copy2(db_path, backup_path)
        print()

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
    if dry_run:
        print("💾 No backup created in dry-run mode")
    else:
        backup_name = Path(backup_path).name if backup_path else 'unknown'
        print(f"💾 Backup saved to: {backup_name}")
        print("   (in case you need to restore)")
    print()

    return 0

def main():
    # Parse flags
    dry_run = '--dry-run' in sys.argv
    remove_orphans = '--remove-orphans' in sys.argv
    auto_yes = '--yes' in sys.argv

    # find first non-flag argument to use as workspace id
    workspace_id = None
    for a in sys.argv[1:]:
        if not a.startswith('-'):
            workspace_id = a
            break

    if workspace_id:
        storage_root = Path.home() / ".config/Code/User/workspaceStorage"
        workspace_path = storage_root / workspace_id

        if not workspace_path.exists():
            print(f"❌ Error: Workspace ID '{workspace_id}' not found")
            print()
            print("Run without arguments to see available workspaces.")
            return 1

        if not dry_run and not auto_yes:
            print("⚠️  IMPORTANT: Please close VS Code completely before continuing!")
            print()
            response = input("Have you closed VS Code? (yes/no): ").strip().lower()
            if response not in ['yes', 'y']:
                print()
                print("❌ Aborted. Please close VS Code and run this script again.")
                return 1
            print()

        return repair_workspace(workspace_path, dry_run=dry_run, remove_orphans=remove_orphans)

    # No workspace ID provided - list available workspaces
    workspaces = list_workspaces()

    if not workspaces:
        print("❌ No workspaces found.")
        return 1

    print_workspaces(workspaces)

    workspaces_with_sessions = [w for w in workspaces if w['session_count'] > 0]

    if not workspaces_with_sessions:
        return 0

    print()
    print("Usage:")
    print(f"  python3 {sys.argv[0]} <workspace_id>")
    print()
    print("Example:")
    print(f"  python3 {sys.argv[0]} {workspaces_with_sessions[0]['id']}")
    print()

    return 0

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
