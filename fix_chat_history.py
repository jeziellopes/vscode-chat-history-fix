#!/usr/bin/env python3
"""
VS Code Chat History Repair Tool
=================================

Fixes missing chat sessions in VS Code by rebuilding the session index.

Problem:
- Chat session files exist in: chatSessions/*.json or chatSessions/*.jsonl
- But they don't appear in VS Code's UI
- Because they're missing from: state.vscdb -> chat.ChatSessionStore.index

Solution:
- Scans session JSON and JSONL files
- Rebuilds the index in state.vscdb
- Can recover orphaned sessions from other workspaces

Usage:
    # Auto-repair ALL workspaces
    python3 fix_chat_history.py
    
    # List workspaces that need repair
    python3 fix_chat_history.py --list
    
    # Repair specific workspace
    python3 fix_chat_history.py <workspace_id>

    # Merge sessions from duplicate workspace storage folders
    python3 fix_chat_history.py --merge

Options:
    --list             List workspaces that need repair
    --show-all         (with --list) Show all workspaces, including healthy ones
    --dry-run          Preview changes without modifying anything
    --yes              Skip confirmation prompts
    --remove-orphans   Remove orphaned index entries (default: keep)
    --recover-orphans  Copy orphaned sessions from other workspaces
    --merge            Merge sessions from duplicate workspace folders
    --insiders         Use VS Code Insiders storage instead of regular VS Code
    --help, -h         Show this help message

Examples:
    # Safe preview of what would be fixed
    python3 fix_chat_history.py --dry-run
    
    # Fix everything automatically
    python3 fix_chat_history.py --yes
    
    # Recover sessions from other workspaces
    python3 fix_chat_history.py --recover-orphans
    
    # List workspaces that need repair
    python3 fix_chat_history.py --list
    
    # List all workspaces (including healthy ones)
    python3 fix_chat_history.py --list --show-all
    
    # Fix specific workspace
    python3 fix_chat_history.py f4c750964946a489902dcd863d1907de

    # Use VS Code Insiders instead of regular VS Code
    python3 fix_chat_history.py --insiders

    # Merge duplicate workspace folders (common after migrating machines)
    python3 fix_chat_history.py --merge

IMPORTANT: Close VS Code completely before running this script!
"""

import json
import sqlite3
import shutil
import sys
import platform
import io

def _ensure_utf8_stream(stream):
    """Return a UTF-8-capable text stream, avoiding AttributeError on .buffer."""
    encoding = getattr(stream, "encoding", None)
    if not encoding or not encoding.lower().startswith("cp"):
        return stream
    # Prefer reconfigure when available (Python 3.7+)
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            return stream
        except (TypeError, ValueError):
            pass
    # Fall back to wrapping the underlying buffer if present
    if hasattr(stream, "buffer"):
        return io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace")
    # As a last resort, leave the stream unchanged
    return stream


# Ensure emoji/unicode output works on Windows (cp1252 terminals)
sys.stdout = _ensure_utf8_stream(sys.stdout)
sys.stderr = _ensure_utf8_stream(sys.stderr)

import base64
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Set, Optional

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

# Global flag for VS Code Insiders mode
_use_insiders = False

def get_vscode_storage_root() -> Path:
    """Get the VS Code workspace storage directory for the current platform.
    
    By default uses regular VS Code ("Code"). Pass --insiders flag to use
    VS Code Insiders ("Code - Insiders") storage instead.
    """
    home = Path.home()
    system = platform.system()
    app_name = "Code - Insiders" if _use_insiders else "Code"
    
    if system == "Darwin":  # macOS
        return home / f"Library/Application Support/{app_name}/User/workspaceStorage"
    elif system == "Windows":
        return home / f"AppData/Roaming/{app_name}/User/workspaceStorage"
    else:  # Linux and others
        return home / f".config/{app_name}/User/workspaceStorage"

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

def parse_jsonl_session(session_file: Path) -> Optional[Dict]:
    """Parse a .jsonl (JSON Lines) session file and extract metadata.
    
    JSONL format (used by newer VS Code / Copilot Chat):
      - Line with kind:0 = initial state snapshot (creationDate, initialLocation, sessionId, requests)
      - Lines with kind:1 = set mutations (k=key path, v=value). e.g. customTitle
      - Lines with kind:2 = array splice/push (k=key path, v=items to add). e.g. requests
    
    Optimized to handle very large files (80MB+) by:
      - Only fully parsing small lines (kind:0, kind:1)
      - Using regex to extract timestamps from large kind:2 lines
    """
    import re
    try:
        title = "Untitled Session"
        last_message_date = 0
        creation_date = 0
        initial_location = "panel"
        is_empty = True
        first_request_text = None
        custom_title = None
        
        # Regex to extract timestamps from kind:2 lines without full JSON parse
        timestamp_re = re.compile(r'"timestamp"\s*:\s*(\d+)')

        with open(session_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                # Quick prefix check to determine kind without full parse
                # For large kind:2 lines, avoid full json.loads
                if line.startswith('{"kind":2'):
                    # This is an array splice (requests) - can be very large
                    is_empty = False
                    # Extract timestamps with regex (much faster than json.loads)
                    for m in timestamp_re.finditer(line):
                        ts = int(m.group(1))
                        if ts > last_message_date:
                            last_message_date = ts
                    # Extract first request text if not found yet
                    if not first_request_text and '"message"' in line and '"parts"' in line:
                        # Only parse if line is not too huge (< 1MB) to get title
                        if len(line) < 1_000_000:
                            try:
                                entry = json.loads(line)
                                k = entry.get("k", [])
                                v = entry.get("v", [])
                                if k == ["requests"] and isinstance(v, list):
                                    for req in v:
                                        if not first_request_text and "message" in req and "parts" in req["message"]:
                                            text_parts = [
                                                p.get("text", "")
                                                for p in req["message"]["parts"]
                                                if "text" in p
                                            ]
                                            if text_parts:
                                                first_request_text = text_parts[0].strip()
                                                break
                            except (json.JSONDecodeError, Exception):
                                pass
                        else:
                            # For very large lines, try regex to get first message text
                            if not first_request_text:
                                text_match = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.){1,200})', line)
                                if text_match:
                                    first_request_text = text_match.group(1)
                    continue
                
                # For kind:0 and kind:1, parse fully (these are typically small)
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                kind = entry.get("kind")

                if kind == 0:
                    # Initial state snapshot
                    v = entry.get("v", {})
                    creation_date = v.get("creationDate", 0)
                    initial_location = v.get("initialLocation", "panel")
                    # Check if initial state already has requests
                    initial_requests = v.get("requests", [])
                    if initial_requests:
                        is_empty = False
                        # Get first request text
                        first_req = initial_requests[0]
                        if "message" in first_req and "parts" in first_req["message"]:
                            text_parts = [
                                p.get("text", "")
                                for p in first_req["message"]["parts"]
                                if "text" in p
                            ]
                            if text_parts and not first_request_text:
                                first_request_text = text_parts[0].strip()
                        # Get timestamp from last initial request
                        last_req = initial_requests[-1]
                        ts = last_req.get("timestamp", 0)
                        if ts > last_message_date:
                            last_message_date = ts

                elif kind == 1:
                    # Set mutation
                    k = entry.get("k", [])
                    v = entry.get("v")
                    if k == ["customTitle"] and isinstance(v, str) and v:
                        custom_title = v

        # Determine title
        if custom_title:
            title = custom_title
        elif first_request_text:
            title = first_request_text
            if len(title) > 100:
                title = title[:97] + "..."
        
        if not title or title.isspace():
            title = "Untitled Session"

        # Use creation_date as fallback for lastMessageDate
        if last_message_date == 0 and creation_date > 0:
            last_message_date = creation_date

        return {
            "title": title,
            "lastMessageDate": last_message_date,
            "initialLocation": initial_location,
            "isEmpty": is_empty
        }
    except Exception:
        return None


class WorkspaceInfo:
    def __init__(self, workspace_dir: Path):
        self.path = workspace_dir
        self.id = workspace_dir.name
        self.sessions_dir = workspace_dir / "chatSessions"
        self.db_path = workspace_dir / "state.vscdb"

        # Load workspace metadata
        workspace_json = workspace_dir / "workspace.json"
        self.folder = None
        self.workspace_file = None
        if workspace_json.exists():
            try:
                with open(workspace_json, 'r') as f:
                    info = json.load(f)
                    # Check for folder-based workspace
                    if 'folder' in info:
                        folder = info['folder']
                        if isinstance(folder, str):
                            self.folder = folder
                        elif isinstance(folder, dict) and 'path' in folder:
                            self.folder = folder['path']
                    # Check for .code-workspace file
                    elif 'workspace' in info:
                        self.workspace_file = info['workspace']
            except:
                pass

        # Get session IDs from disk (support both .json and .jsonl)
        self.sessions_on_disk: Set[str] = set()
        if self.sessions_dir.exists():
            for session_file in self.sessions_dir.glob("*.json"):
                self.sessions_on_disk.add(session_file.stem)
            for session_file in self.sessions_dir.glob("*.jsonl"):
                self.sessions_on_disk.add(session_file.stem)

        # Get session IDs from index
        self.sessions_in_index: Set[str] = set()
        # Session IDs that are empty (no requests) according to the index
        self.empty_sessions_in_index: Set[str] = set()
        # Get session IDs from agentSessions.model.cache (used by Agent panel)
        self.sessions_in_agent_cache: Set[str] = set()
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
                ).fetchone()

                if row:
                    index = json.loads(row[0])
                    entries_map = index.get("entries", {})
                    self.sessions_in_index = set(entries_map.keys())
                    self.empty_sessions_in_index = {
                        sid for sid, meta in entries_map.items()
                        if meta.get("isEmpty", False)
                    }

                # Parse agentSessions.model.cache
                row2 = cursor.execute(
                    "SELECT value FROM ItemTable WHERE key = 'agentSessions.model.cache'"
                ).fetchone()
                if row2:
                    agent_cache = json.loads(row2[0])
                    if isinstance(agent_cache, list):
                        for item in agent_cache:
                            if not isinstance(item, dict):
                                continue
                            resource = item.get("resource", "")
                            if "vscode-chat-session://local/" in resource:
                                b64 = resource.split("/")[-1]
                                try:
                                    session_id = base64.b64decode(b64).decode()
                                    self.sessions_in_agent_cache.add(session_id)
                                except:
                                    pass

                conn.close()
            except:
                pass
    
    def get_display_name(self) -> str:
        """Get a user-friendly display name for this workspace."""
        # Try to get name from folder
        if self.folder:
            project_name = extract_project_name(self.folder)
            if project_name:
                return f"{project_name} ({self.id}) [Folder]"
        
        # Try to get name from .code-workspace file
        if self.workspace_file:
            workspace_name = extract_project_name(self.workspace_file)
            if workspace_name:
                # Remove .code-workspace extension if present
                if workspace_name.endswith('.code-workspace'):
                    workspace_name = workspace_name[:-15]
                return f"{workspace_name} ({self.id}) [Workspace File]"
        
        # Fallback to "Unknown"
        return f"Unknown ({self.id})"

    @property
    def missing_from_index(self) -> Set[str]:
        """Session files that exist but aren't in the chat index."""
        return self.sessions_on_disk - self.sessions_in_index

    @property
    def missing_from_agent_cache(self) -> Set[str]:
        """Non-empty session files that exist on disk but aren't in the agent sessions cache.
        
        Empty sessions (isEmpty=True) are intentionally excluded from the agent panel
        cache because VS Code would discard them on load anyway, causing a visible
        count drop ("20+ chats suddenly showing 10+").
        """
        non_empty_on_disk = self.sessions_on_disk - self.empty_sessions_in_index
        return non_empty_on_disk - self.sessions_in_agent_cache

    @property
    def orphaned_in_index(self) -> Set[str]:
        """Index entries that don't have corresponding files."""
        return self.sessions_in_index - self.sessions_on_disk

    @property
    def needs_repair(self) -> bool:
        """True if the workspace has corrupted index or missing agent cache entries."""
        return (len(self.missing_from_index) > 0 or 
                len(self.orphaned_in_index) > 0 or 
                len(self.missing_from_agent_cache) > 0)

    @property
    def has_sessions(self) -> bool:
        """True if workspace has any session files."""
        return len(self.sessions_on_disk) > 0

def scan_workspaces() -> List[WorkspaceInfo]:
    """Scan all VS Code workspaces and return their info."""
    storage_root = get_vscode_storage_root()

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

def find_orphan_in_other_workspaces(session_id: str, current_workspace: WorkspaceInfo, all_workspaces: List[WorkspaceInfo]) -> Optional[Dict]:
    """Check if an orphaned session ID exists as a file in another workspace.
    
    Returns a dict with workspace info and whether it's the same project folder.
    """
    for ws in all_workspaces:
        if ws.id != current_workspace.id and session_id in ws.sessions_on_disk:
            same_project = folders_match(current_workspace.folder, ws.folder)
            return {
                'workspace': ws,
                'same_project': same_project
            }
    return None


def _session_id_to_resource(session_id: str) -> str:
    """Convert a session ID to a vscode-chat-session URI with base64-encoded ID."""
    # Use URL-safe base64 and strip padding so the ID is safe in a URI path segment
    b64 = base64.urlsafe_b64encode(session_id.encode()).decode().rstrip("=")
    return f"vscode-chat-session://local/{b64}"


def _update_agent_sessions_cache(cursor, entries: Dict):
    """Update agentSessions.model.cache and agentSessions.state.cache.
    
    The Agent panel in VS Code Insiders reads from these keys, not from
    chat.ChatSessionStore.index. Without entries here, sessions are invisible
    in the right panel even though they exist on disk.
    
    agentSessions.model.cache format (list):
    [
        {
            "providerType": "local",
            "providerLabel": "Local",
            "resource": "vscode-chat-session://local/<base64_session_id>",
            "icon": "vm",
            "label": "<title>",
            "status": 1,
            "timing": {
                "created": <timestamp>,
                "lastRequestStarted": <timestamp>,
                "lastRequestEnded": <timestamp>
            }
        },
        ...
    ]
    
    agentSessions.state.cache format (list):
    [
        {
            "resource": "vscode-chat-session://local/<base64_session_id>",
            "archived": false,
            "read": <timestamp>
        },
        ...
    ]
    """
    # Load existing caches to preserve non-chat entries (codex, copilotcli, etc.)
    existing_model_cache = []
    existing_state_cache = []
    
    row = cursor.execute(
        "SELECT value FROM ItemTable WHERE key = 'agentSessions.model.cache'"
    ).fetchone()
    if row:
        try:
            existing_model_cache = json.loads(row[0])
        except:
            pass
    
    row = cursor.execute(
        "SELECT value FROM ItemTable WHERE key = 'agentSessions.state.cache'"
    ).fetchone()
    if row:
        try:
            existing_state_cache = json.loads(row[0])
        except:
            pass
    
    # Build sets of existing chat session resources for quick lookup
    existing_model_resources = set()
    non_chat_model_entries = []
    for item in existing_model_cache:
        r = item.get("resource", "")
        if "vscode-chat-session://" in r:
            existing_model_resources.add(r)
        else:
            non_chat_model_entries.append(item)
    
    existing_state_resources = set()
    non_chat_state_entries = []
    for item in existing_state_cache:
        r = item.get("resource", "")
        if "vscode-chat-session://" in r:
            existing_state_resources.add(r)
        else:
            non_chat_state_entries.append(item)
    
    # Keep existing chat entries that are already in the cache
    kept_model_entries = [
        item for item in existing_model_cache
        if "vscode-chat-session://" in item.get("resource", "")
    ]
    kept_state_entries = [
        item for item in existing_state_cache
        if "vscode-chat-session://" in item.get("resource", "")
    ]
    
    # Add missing sessions to both caches
    for session_id, entry_data in entries.items():
        # Never add empty sessions to the agent panel cache — VS Code would
        # show them momentarily then discard them (causing the count-drop symptom).
        if entry_data.get("isEmpty", False):
            continue

        resource = _session_id_to_resource(session_id)
        
        if resource not in existing_model_resources:
            # Create model cache entry
            ts = entry_data.get("lastMessageDate", 0)
            model_entry = {
                "providerType": "local",
                "providerLabel": "Local",
                "resource": resource,
                "icon": "vm",
                "label": entry_data.get("title", "Untitled Session"),
                "status": 1,
                "timing": {
                    "created": ts
                }
            }
            kept_model_entries.append(model_entry)
        
        if resource not in existing_state_resources:
            # Create state cache entry
            ts = entry_data.get("lastMessageDate", 0)
            state_entry = {
                "resource": resource,
                "archived": False,
                "read": ts
            }
            kept_state_entries.append(state_entry)
    
    # Combine: non-chat entries + chat entries
    new_model_cache = non_chat_model_entries + kept_model_entries
    new_state_cache = non_chat_state_entries + kept_state_entries
    
    # Write back to database
    cursor.execute(
        "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
        ('agentSessions.model.cache', json.dumps(new_model_cache, separators=(',', ':')))
    )
    cursor.execute(
        "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
        ('agentSessions.state.cache', json.dumps(new_state_cache, separators=(',', ':')))
    )


def repair_workspace(workspace: WorkspaceInfo, dry_run: bool = False, show_details: bool = False, remove_orphans: bool = False) -> Dict:
    """Repair a workspace's chat session index."""
    result = {
        'success': False,
        'sessions_restored': 0,
        'sessions_removed': 0,
        'agent_cache_added': 0,
        'error': None,
        'restored_sessions': []
    }

    try:
        # Build new index from all session files
        entries = {}
        
        # If not removing orphans, start with existing index entries
        if not remove_orphans and workspace.db_path.exists():
            try:
                conn = sqlite3.connect(workspace.db_path)
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'"
                ).fetchone()
                conn.close()
                
                if row:
                    existing_index = json.loads(row[0])
                    entries = existing_index.get("entries", {})
            except:
                pass

        for session_id in sorted(workspace.sessions_on_disk):
            # Skip re-parsing sessions already in the index (optimization for large workspaces)
            if session_id in entries:
                continue

            json_file = workspace.sessions_dir / f"{session_id}.json"
            jsonl_file = workspace.sessions_dir / f"{session_id}.jsonl"

            try:
                title = "Untitled Session"
                last_message_date = 0
                is_empty = True
                initial_location = "panel"

                if jsonl_file.exists():
                    # Parse newer .jsonl format (preferred; may coexist with legacy .json)
                    parsed = parse_jsonl_session(jsonl_file)
                    if parsed:
                        title = parsed["title"]
                        last_message_date = parsed["lastMessageDate"]
                        is_empty = parsed["isEmpty"]
                        initial_location = parsed["initialLocation"]
                    else:
                        print(f"      ⚠️  Failed to parse JSONL {session_id}")
                        continue

                elif json_file.exists():
                    # Parse legacy .json format (fallback when no .jsonl exists)
                    with open(json_file, 'r', encoding='utf-8') as f:
                        session_data = json.load(f)

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

                    initial_location = session_data.get("initialLocation", "panel")

                else:
                    continue  # No file found

                entries[session_id] = {
                    "sessionId": session_id,
                    "title": title,
                    "lastMessageDate": last_message_date,
                    "isImported": False,
                    "initialLocation": initial_location,
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

            # Also update agentSessions.model.cache and agentSessions.state.cache
            # These are what the Agent panel (right side) reads from
            _update_agent_sessions_cache(cursor, entries)

            conn.commit()
            conn.close()

        result['success'] = True
        result['sessions_restored'] = len(workspace.missing_from_index)
        result['agent_cache_added'] = len(workspace.missing_from_agent_cache)

        # Only count removed sessions if we're actually removing orphans
        if remove_orphans:
            result['sessions_removed'] = len(workspace.orphaned_in_index)
        else:
            result['sessions_removed'] = 0

    except Exception as e:
        result['error'] = str(e)

    return result

def list_workspaces_mode(show_all: bool = False):
    """List workspaces with chat sessions."""
    print()
    print("=" * 70)
    if show_all:
        print("VS Code Workspaces with Chat Sessions")
    else:
        print("VS Code Workspaces That Need Repair")
    print("=" * 70)
    print()

    workspaces = scan_workspaces()

    if not workspaces:
        print("No workspaces with chat sessions found.")
        return 0

    if not show_all:
        workspaces = [ws for ws in workspaces if ws.needs_repair]
        if not workspaces:
            print("✅ No workspaces need repair.")
            return 0

    print(f"Found {len(workspaces)} workspace(s):")
    print()

    for i, ws in enumerate(workspaces, 1):
        status = "⚠️  NEEDS REPAIR" if ws.needs_repair else "✅ HEALTHY"
        print(f"{i}. {ws.get_display_name()} - {status}")
        
        # Show full ID if we have Unknown workspace
        if not ws.folder and not ws.workspace_file:
            print(f"   ID: {ws.id}")
        
        if ws.folder:
            print(f"   Folder: {ws.folder}")
        elif ws.workspace_file:
            print(f"   Workspace file: {ws.workspace_file}")
        
        print(f"   Sessions on disk: {len(ws.sessions_on_disk)}")
        print(f"   Sessions in index: {len(ws.sessions_in_index)}")
        print(f"   Sessions in agent cache: {len(ws.sessions_in_agent_cache)}")
        
        if ws.missing_from_index:
            print(f"   ⚠️  Missing from index: {len(ws.missing_from_index)}")
        
        if ws.missing_from_agent_cache:
            print(f"   ⚠️  Missing from agent cache: {len(ws.missing_from_agent_cache)}")
        
        if ws.orphaned_in_index:
            print(f"   🗑️  Orphaned in index: {len(ws.orphaned_in_index)}")
        
        print()

    needs_repair = [ws for ws in workspaces if ws.needs_repair]
    
    if needs_repair:
        print(f"📊 Summary: {len(needs_repair)} workspace(s) need repair")
        print()
        print("To repair all workspaces:")
        print("  python3 fix_chat_history.py")
        print()
        print("To repair a specific workspace:")
        print(f"  python3 fix_chat_history.py {needs_repair[0].id}")
        print()
    else:
        print("✅ All workspaces are healthy!")
        print()

    return 0

def repair_single_workspace(workspace_id: str, dry_run: bool, remove_orphans: bool, recover_orphans: bool, auto_yes: bool):
    """Repair a specific workspace by ID."""
    storage_root = get_vscode_storage_root()
    workspace_path = storage_root / workspace_id

    if not workspace_path.exists():
        print(f"❌ Error: Workspace ID '{workspace_id}' not found")
        print()
        print("Run with --list to see available workspaces.")
        return 1

    print()
    print("=" * 70)
    print("VS Code Chat History Repair Tool - Single Workspace")
    print("=" * 70)
    print()

    if dry_run:
        print("🔍 DRY RUN MODE - No changes will be made")
        print()

    workspace = WorkspaceInfo(workspace_path)
    
    print(f"🔧 Workspace: {workspace.get_display_name()}")
    if not workspace.folder and not workspace.workspace_file:
        print(f"   ID: {workspace.id}")
    if workspace.folder:
        print(f"   Folder: {workspace.folder}")
    elif workspace.workspace_file:
        print(f"   Workspace file: {workspace.workspace_file}")
    
    print(f"   Sessions on disk: {len(workspace.sessions_on_disk)}")
    print(f"   Sessions in index: {len(workspace.sessions_in_index)}")
    print()

    if not workspace.needs_repair:
        print("✅ This workspace doesn't need repair!")
        return 0

    # Show what needs fixing
    if workspace.missing_from_index:
        print(f"⚠️  Missing from chat index: {len(workspace.missing_from_index)}")
    
    if workspace.missing_from_agent_cache:
        print(f"⚠️  Missing from agent panel cache: {len(workspace.missing_from_agent_cache)}")
    
    recoverable_orphans = {}
    
    if workspace.orphaned_in_index:
        orphan_msg = f"🗑️  Orphaned in index: {len(workspace.orphaned_in_index)}"
        if remove_orphans:
            orphan_msg += " (will be removed)"
        else:
            orphan_msg += " (will be kept)"
        print(orphan_msg)
        
        # Check if orphans exist in other workspaces
        all_workspaces = scan_workspaces()
        for session_id in workspace.orphaned_in_index:
            found_info = find_orphan_in_other_workspaces(session_id, workspace, all_workspaces)
            if found_info:
                recoverable_orphans[session_id] = found_info
                found_ws = found_info['workspace']
                same_project = found_info['same_project']
                
                if same_project:
                    project_name = extract_project_name(workspace.folder)
                    print(f"   💡 Session {session_id[:8]}... found in workspace: {found_ws.get_display_name()}")
                    print(f"      ⭐ Same project folder: '{project_name}' - likely belongs here!")
                else:
                    print(f"   💡 Session {session_id[:8]}... found in workspace: {found_ws.get_display_name()}")
        
        if recoverable_orphans and not recover_orphans:
            print(f"   💡 Use --recover-orphans to copy these {len(recoverable_orphans)} session(s) back")
    
    print()

    # Recover orphaned sessions if requested
    if recover_orphans and recoverable_orphans and not dry_run:
        print("📥 Recovering orphaned sessions...")
        
        workspace.sessions_dir.mkdir(parents=True, exist_ok=True)
        
        for session_id, found_info in recoverable_orphans.items():
            found_ws = found_info['workspace']
            # Copy both .json and .jsonl files if they exist
            copied = False
            for ext in [".json", ".jsonl"]:
                source_file = found_ws.sessions_dir / f"{session_id}{ext}"
                if source_file.exists():
                    target_file = workspace.sessions_dir / f"{session_id}{ext}"
                    try:
                        shutil.copy2(source_file, target_file)
                        copied = True
                    except Exception as e:
                        print(f"   ❌ Failed to copy {session_id[:8]}...{ext}: {e}")
            
            if copied:
                print(f"   ✅ Copied {session_id[:8]}... from {found_ws.get_display_name()}")
                workspace.sessions_on_disk.add(session_id)
            else:
                print(f"   ❌ No session files found for {session_id[:8]}...")
        
        print()

    # Confirm before proceeding
    if not dry_run and not auto_yes:
        print("⚠️  This will modify the database for this workspace.")
        print("   A backup will be created before making changes.")
        print()
        response = input("Proceed with repair? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print()
            print("❌ Aborted.")
            return 1
        print()

    # Repair
    print("🔧 Repairing workspace...")
    result = repair_workspace(workspace, dry_run=dry_run, remove_orphans=remove_orphans)

    if result['success']:
        print()
        print("=" * 70)
        print("✨ REPAIR COMPLETE" if not dry_run else "🔍 DRY RUN COMPLETE")
        print("=" * 70)
        print()
        print(f"📊 Summary:")
        if result['sessions_restored'] > 0:
            print(f"   Sessions restored: {result['sessions_restored']}")
        if result.get('agent_cache_added', 0) > 0:
            print(f"   Agent panel cache entries added: {result['agent_cache_added']}")
        if result['sessions_removed'] > 0:
            print(f"   Orphaned entries removed: {result['sessions_removed']}")
        if (result['sessions_restored'] == 0 and result.get('agent_cache_added', 0) == 0
                and result['sessions_removed'] == 0):
            print(f"   (nothing to change)")
        print()
        
        if not dry_run:
            print("📝 Next Steps:")
            print("   1. Start VS Code")
            print("   2. Open the Chat view")
            print("   3. Your sessions should now be visible!")
            print()
            print("💾 Backup created for the database")
            print()
        else:
            print("To apply these changes, run without --dry-run:")
            print(f"   python3 fix_chat_history.py {workspace_id}")
            print()
        
        return 0
    else:
        print(f"❌ Repair failed: {result['error']}")
        return 1

def repair_all_workspaces(dry_run: bool, auto_yes: bool, remove_orphans: bool, recover_orphans: bool):
    """Auto-repair all workspaces that need it."""
    print()
    print("=" * 70)
    print("VS Code Chat History Repair Tool - Auto Repair")
    print("=" * 70)
    print()

    if dry_run:
        print("🔍 DRY RUN MODE - No changes will be made")
        print()
    
    if remove_orphans:
        print("🗑️  REMOVE ORPHANS MODE - Orphaned index entries will be removed")
        print()
    
    if recover_orphans:
        print("📥 RECOVER ORPHANS MODE - Orphaned sessions will be copied from other workspaces")
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
    recoverable_orphans = {}  # session_id -> source workspace

    for i, ws in enumerate(needs_repair, 1):
        print(f"{i}. Workspace: {ws.get_display_name()}")
        # Show full ID if we have Unknown workspace
        if not ws.folder and not ws.workspace_file:
            print(f"   ID: {ws.id}")
        if ws.folder:
            print(f"   Folder: {ws.folder}")
        elif ws.workspace_file:
            print(f"   Workspace file: {ws.workspace_file}")
        print(f"   Sessions on disk: {len(ws.sessions_on_disk)}")
        print(f"   Sessions in index: {len(ws.sessions_in_index)}")
        print(f"   Sessions in agent cache: {len(ws.sessions_in_agent_cache)}")

        if ws.missing_from_index:
            print(f"   ⚠️  Missing from chat index: {len(ws.missing_from_index)}")
            total_missing += len(ws.missing_from_index)

        if ws.missing_from_agent_cache:
            print(f"   ⚠️  Missing from agent panel cache: {len(ws.missing_from_agent_cache)}")

        if ws.orphaned_in_index:
            orphan_msg = f"   🗑️  Orphaned in index: {len(ws.orphaned_in_index)}"
            if remove_orphans:
                orphan_msg += " (will be removed)"
            else:
                orphan_msg += " (will be kept - use --remove-orphans to remove)"
            print(orphan_msg)
            total_orphaned += len(ws.orphaned_in_index)
            
            # Check if orphans exist in other workspaces
            for session_id in ws.orphaned_in_index:
                found_info = find_orphan_in_other_workspaces(session_id, ws, workspaces)
                if found_info:
                    recoverable_orphans[session_id] = found_info
                    found_ws = found_info['workspace']
                    same_project = found_info['same_project']
                    
                    if same_project:
                        # Highlight that it's from the same project
                        project_name = extract_project_name(ws.folder)
                        print(f"      💡 Session {session_id[:8]}... found in workspace: {found_ws.get_display_name()}")
                        print(f"         ⭐ Same project folder: '{project_name}' - likely belongs here!")
                    else:
                        print(f"      💡 Session {session_id[:8]}... found in workspace: {found_ws.get_display_name()}")

        print()

    print(f"📊 Total issues:")
    print(f"   Sessions to restore: {total_missing}")
    print(f"   Orphaned entries: {total_orphaned}")
    if recoverable_orphans:
        print(f"   🔍 Orphans found in other workspaces: {len(recoverable_orphans)}")
        if recover_orphans:
            print(f"      📥 Will be recovered (copied back)")
        else:
            print(f"      (Use --recover-orphans to copy them back)")
    print()

    # Copy orphaned sessions from other workspaces if requested
    total_recovered = 0
    if recover_orphans and recoverable_orphans and not dry_run:
        print("📥 Recovering orphaned sessions from other workspaces...")
        print()
        
        # Group by target workspace
        recovery_map = {}  # workspace -> list of (session_id, source_workspace)
        for session_id, found_info in recoverable_orphans.items():
            # Find which workspace needs this session
            for ws in needs_repair:
                if session_id in ws.orphaned_in_index:
                    if ws not in recovery_map:
                        recovery_map[ws] = []
                    recovery_map[ws].append((session_id, found_info['workspace']))
                    break
        
        for target_ws, sessions_to_recover in recovery_map.items():
            print(f"   Recovering to: {target_ws.get_display_name()}")
            
            # Ensure sessions directory exists
            target_ws.sessions_dir.mkdir(parents=True, exist_ok=True)
            
            for session_id, source_ws in sessions_to_recover:
                # Copy both .json and .jsonl files if they exist
                copied = False
                for ext in [".json", ".jsonl"]:
                    source_file = source_ws.sessions_dir / f"{session_id}{ext}"
                    if source_file.exists():
                        target_file = target_ws.sessions_dir / f"{session_id}{ext}"
                        try:
                            shutil.copy2(source_file, target_file)
                            copied = True
                        except Exception as e:
                            print(f"      ❌ Failed to copy {session_id[:8]}...{ext}: {e}")
                
                if copied:
                    print(f"      ✅ Copied {session_id[:8]}... from {source_ws.get_display_name()}")
                    total_recovered += 1
                    target_ws.sessions_on_disk.add(session_id)
                else:
                    print(f"      ❌ No session files found for {session_id[:8]}...")
            
            print()
        
        print(f"📥 Recovered {total_recovered} session(s)")
        print()
    elif recover_orphans and recoverable_orphans and dry_run:
        print("📥 DRY RUN: Would recover these sessions:")
        for session_id, found_info in recoverable_orphans.items():
            found_ws = found_info['workspace']
            print(f"   {session_id[:8]}... from {found_ws.get_display_name()}")
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

    for ws in needs_repair:
        print(f"   Repairing: {ws.get_display_name()}")
        if ws.folder:
            print(f"      Path: {ws.folder}")

        result = repair_workspace(ws, dry_run=dry_run, show_details=dry_run, remove_orphans=remove_orphans)

        if result['success']:
            if result['sessions_restored'] > 0:
                print(f"      ✅ Will restore {result['sessions_restored']} session(s)" if dry_run else f"      ✅ Restored {result['sessions_restored']} session(s)")

            if result.get('agent_cache_added', 0) > 0:
                print(f"      ✅ Will add {result['agent_cache_added']} agent panel cache entr(y|ies)" if dry_run else f"      ✅ Added {result['agent_cache_added']} agent panel cache entr(y|ies)")

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
    if total_orphaned > 0 and remove_orphans:
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
        print(f"   python3 fix_chat_history.py")
        print()

    return 0 if fail_count == 0 else 1


def _find_duplicate_workspaces() -> Dict[str, list]:
    """Find workspace storage folders that share the same workspace URI.
    
    When migrating VS Code storage from one machine to another, VS Code may
    create new storage folders with different hashes for the same workspace.
    Sessions in the old folder become invisible because VS Code reads from
    the new (active) folder.
    
    Returns a dict of {uri: [folder_info, ...]} for URIs with duplicates.
    """
    from collections import defaultdict
    storage_root = get_vscode_storage_root()
    uri_to_folders = defaultdict(list)

    for d in storage_root.iterdir():
        if not d.is_dir():
            continue
        workspace_json = d / "workspace.json"
        if not workspace_json.exists():
            continue
        try:
            ws_info = json.loads(workspace_json.read_text(encoding='utf-8'))
        except:
            continue

        uri = ws_info.get('folder', ws_info.get('workspace', None))
        if not uri:
            continue

        db = d / "state.vscdb"
        mtime = db.stat().st_mtime if db.exists() else 0

        chat_dir = d / "chatSessions"
        session_ids = set()
        if chat_dir.exists():
            for f in chat_dir.iterdir():
                if f.suffix in ('.json', '.jsonl'):
                    session_ids.add(f.stem)

        uri_to_folders[uri].append({
            'hash': d.name,
            'path': d,
            'mtime': mtime,
            'session_ids': session_ids,
            'n_sessions': len(session_ids),
        })

    return {uri: folders for uri, folders in uri_to_folders.items() if len(folders) > 1}


def _merge_one_workspace(uri: str, folders: list) -> Dict:
    """Merge sessions from old/duplicate folders into the active (newest) folder."""
    folders.sort(key=lambda x: x['mtime'], reverse=True)
    active = folders[0]
    old_folders = folders[1:]

    result = {
        'files_copied': 0,
        'sessions_added_to_index': 0,
        'sessions_added_to_agent_cache': 0,
        'errors': [],
    }

    active_session_ids = active['session_ids'].copy()
    active_sessions_dir = active['path'] / "chatSessions"

    # Collect files to copy
    files_to_copy = []
    for old in old_folders:
        old_sessions_dir = old['path'] / "chatSessions"
        missing_ids = old['session_ids'] - active_session_ids
        for sid in sorted(missing_ids):
            for ext in ('.json', '.jsonl'):
                src = old_sessions_dir / f"{sid}{ext}"
                if src.exists():
                    dst = active_sessions_dir / f"{sid}{ext}"
                    if not dst.exists():
                        files_to_copy.append((src, dst, sid))

    if not files_to_copy:
        return result

    merged_session_ids = {sid for _, _, sid in files_to_copy}
    print(f"   Copying {len(files_to_copy)} file(s) ({len(merged_session_ids)} sessions)...")

    active_sessions_dir.mkdir(exist_ok=True)
    for src, dst, sid in files_to_copy:
        try:
            shutil.copy2(src, dst)
            result['files_copied'] += 1
        except Exception as e:
            result['errors'].append(f"Failed to copy {src.name}: {e}")

    # Update database
    db_path = active['path'] / "state.vscdb"
    if db_path.exists():
        backup_path = str(db_path) + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(db_path, backup_path)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Update chat.ChatSessionStore.index
        row = cursor.execute("SELECT value FROM ItemTable WHERE key = 'chat.ChatSessionStore.index'").fetchone()
        index_data = json.loads(row[0]) if row else {"version": 1, "entries": {}}
        entries = index_data.get("entries", {})
        added_entries = {}

        for sid in sorted(merged_session_ids):
            if sid not in entries:
                json_file = active_sessions_dir / f"{sid}.json"
                jsonl_file = active_sessions_dir / f"{sid}.jsonl"
                parsed = None

                if json_file.exists():
                    try:
                        with open(json_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        title = "Untitled Session"
                        last_message_date = 0
                        is_empty = True
                        initial_location = data.get("initialLocation", "panel")
                        if "requests" in data and data["requests"]:
                            is_empty = False
                            first_req = data["requests"][0]
                            if "message" in first_req and "parts" in first_req["message"]:
                                text_parts = [p.get("text", "") for p in first_req["message"]["parts"] if "text" in p]
                                if text_parts:
                                    title = text_parts[0].strip()
                                    if len(title) > 100:
                                        title = title[:97] + "..."
                                    if not title:
                                        title = "Untitled Session"
                            last_message_date = data["requests"][-1].get("timestamp", 0)
                        parsed = {"title": title, "lastMessageDate": last_message_date,
                                  "isEmpty": is_empty, "initialLocation": initial_location}
                    except:
                        pass

                elif jsonl_file.exists():
                    parsed = parse_jsonl_session(jsonl_file)

                if parsed:
                    entries[sid] = {
                        "sessionId": sid, "title": parsed["title"],
                        "lastMessageDate": parsed["lastMessageDate"], "isImported": False,
                        "initialLocation": parsed.get("initialLocation", "panel"),
                        "isEmpty": parsed.get("isEmpty", True)
                    }
                    added_entries[sid] = entries[sid]
                    result['sessions_added_to_index'] += 1

        index_data["entries"] = entries
        cursor.execute(
            "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
            ('chat.ChatSessionStore.index', json.dumps(index_data, separators=(',', ':')))
        )

        # Update agentSessions.model.cache and state.cache
        _update_agent_sessions_cache(cursor, added_entries)
        result['sessions_added_to_agent_cache'] = len(added_entries)

        conn.commit()
        conn.close()

    return result


def merge_workspaces_mode(dry_run: bool = False, auto_yes: bool = False) -> int:
    """Find and merge sessions from duplicate workspace storage folders.
    
    When migrating VS Code storage from another machine, VS Code creates new
    workspace storage folders with different hashes. Sessions in the old folders
    become invisible. This mode copies them into the active folder and updates
    the database.
    """
    print()
    print("=" * 70)
    print("VS Code Chat Session Merge")
    print("=" * 70)
    print()

    storage_root = get_vscode_storage_root()
    app_name = "VS Code Insiders" if _use_insiders else "VS Code"
    print(f"Storage: {storage_root}")
    print(f"Mode: {'DRY-RUN (preview)' if dry_run else 'APPLY'}")
    print()

    if not storage_root.exists():
        print(f"❌ {app_name} workspace storage not found at:")
        print(f"   {storage_root}")
        return 1

    duplicates = _find_duplicate_workspaces()

    if not duplicates:
        print("✅ No duplicate workspace storage folders found. Nothing to merge.")
        return 0

    # Calculate merge plans
    merge_plans = []
    total_to_merge = 0

    for uri, folders in sorted(duplicates.items()):
        folders.sort(key=lambda x: x['mtime'], reverse=True)
        active = folders[0]
        all_old_ids = set()
        for old in folders[1:]:
            all_old_ids |= old['session_ids']
        missing = all_old_ids - active['session_ids']

        if missing:
            total_to_merge += len(missing)
            merge_plans.append((uri, folders, missing))

            name = uri.split('/')[-1].replace('%20', ' ')
            print(f"📂 {name}")
            print(f"   Active folder:  {active['hash'][:8]}... ({active['n_sessions']} sessions)")
            for old in folders[1:]:
                overlap = len(old['session_ids'] & active['session_ids'])
                to_merge = len(old['session_ids'] - active['session_ids'])
                if to_merge > 0:
                    print(f"   Old folder:     {old['hash'][:8]}... ({old['n_sessions']} sessions, {to_merge} to merge)")
            print(f"   Sessions to merge: {len(missing)}")
            print()

    if total_to_merge == 0:
        print("✅ All sessions already in active folders. Nothing to merge.")
        return 0

    print(f"📊 Total sessions to merge: {total_to_merge}")
    print()

    if dry_run:
        print("🔍 DRY RUN — no changes made.")
        print()
        print("To apply, run without --dry-run:")
        flag = " --insiders" if _use_insiders else ""
        print(f"   python3 fix_chat_history.py --merge{flag}")
        return 0

    if not auto_yes:
        print(f"⚠️  Close {app_name} completely before continuing!")
        print()
        response = input(f"Merge {total_to_merge} sessions? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print("❌ Aborted.")
            return 1

    print()
    print("🔧 Merging sessions...")
    print()

    total_copied = 0
    total_indexed = 0
    total_cached = 0

    for uri, folders, missing in merge_plans:
        name = uri.split('/')[-1].replace('%20', ' ')
        print(f"   [{name}]")
        result = _merge_one_workspace(uri, folders)
        total_copied += result['files_copied']
        total_indexed += result['sessions_added_to_index']
        total_cached += result['sessions_added_to_agent_cache']
        if result['errors']:
            for e in result['errors']:
                print(f"   ⚠️  {e}")

    print()
    print("✨ MERGE COMPLETE")
    print(f"   Files copied:             {total_copied}")
    print(f"   Index entries added:       {total_indexed}")
    print(f"   Agent cache entries added: {total_cached}")
    print()
    print("📝 Next Steps:")
    print(f"   1. Start {app_name}")
    print("   2. Open the Chat/Agent panel")
    print("   3. Your merged sessions should now be visible!")
    print()

    return 0


def main():
    global _use_insiders

    # Parse flags
    dry_run = '--dry-run' in sys.argv
    auto_yes = '--yes' in sys.argv
    remove_orphans = '--remove-orphans' in sys.argv
    recover_orphans = '--recover-orphans' in sys.argv
    list_mode = '--list' in sys.argv
    show_all = '--show-all' in sys.argv
    merge_mode = '--merge' in sys.argv
    show_help = '--help' in sys.argv or '-h' in sys.argv
    _use_insiders = '--insiders' in sys.argv

    if show_help:
        print(__doc__)
        return 0

    # List mode
    if list_mode:
        return list_workspaces_mode(show_all=show_all)

    # Merge mode - merge sessions from duplicate workspace storage folders
    if merge_mode:
        return merge_workspaces_mode(dry_run, auto_yes)

    # Find first non-flag argument to use as workspace id
    workspace_id = None
    for arg in sys.argv[1:]:
        if not arg.startswith('-'):
            workspace_id = arg
            break

    # Single workspace mode
    if workspace_id:
        if not dry_run and not auto_yes:
            print("⚠️  IMPORTANT: Please close VS Code completely before continuing!")
            print()
            response = input("Have you closed VS Code? (yes/no): ").strip().lower()
            if response not in ['yes', 'y']:
                print()
                print("❌ Aborted. Please close VS Code and run this script again.")
                return 1
            print()

        return repair_single_workspace(workspace_id, dry_run, remove_orphans, recover_orphans, auto_yes)

    # Auto-repair all workspaces mode (default)
    if not dry_run and not auto_yes:
        print()
        print("⚠️  IMPORTANT: Please close VS Code completely before continuing!")
        print()
        response = input("Have you closed VS Code? (yes/no): ").strip().lower()
        if response not in ['yes', 'y']:
            print()
            print("❌ Aborted. Please close VS Code and run this script again.")
            return 1

    return repair_all_workspaces(dry_run, auto_yes, remove_orphans, recover_orphans)

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
