"""Read-only per-conversation activity from the canonical live child registry.

No transport or parent AIAgent is required. Profile identity is the canonical
state.db path; durable delegation edges and compression tips survive rebuilds.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_TERMINAL = frozenset({"completed", "cancelled", "canceled", "failed", "error", "finished", "interrupted", "timeout"})


def _db_identity(db):
    path = getattr(db, "db_path", None)
    return os.path.normcase(str(Path(path).resolve())) if path else None


def active_descendant_counts(db, session_ids):
    """Snapshot counts (including zero) for stored ids, scoped to ``db``.

    Resolve DB lineage outside the registry lock. Walk delegation ancestry, not
    generic parent_session_id, so branch/reset siblings never claim each other's
    work. A finished intermediate child need not remain in the live registry.
    """
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock

    counts = dict.fromkeys(session_ids, 0)
    identity = _db_identity(db)
    if identity is None:
        return counts
    with _active_subagents_lock:
        records = [dict(record) for record in _active_subagents.values()]
    if not records:
        return counts
    tips = {}

    def tip(sid):
        sid = str(sid or "")
        if sid not in tips:
            tips[sid] = str(db.resolve_resume_session_id(sid) or sid) if sid else ""
        return tips[sid]

    wanted = {sid: tip(sid) for sid in counts}
    for record in records:
        if record.get("status") in _TERMINAL:
            continue
        child_db = getattr(record.get("agent"), "_session_db", None)
        if _db_identity(child_db) != identity:
            # Gateway records may have no child DB during construction. Their
            # captured owning session still gives an explicit profile identity.
            owner = record.get("owner_session_record")
            home = owner.get("profile_home") if isinstance(owner, dict) else None
            if not home or os.path.normcase(str((Path(home) / "state.db").resolve())) != identity:
                continue
        ancestors = set()
        current = str(record.get("owner_agent_session_id") or "")
        seen = set()
        while current and current not in seen:
            seen.add(current)
            resolved = tip(current)
            ancestors.add(resolved)
            row = db.get_session(current) or db.get_session(resolved)
            config = row.get("model_config") if row else None
            if isinstance(config, str):
                try:
                    config = json.loads(config)
                except (ValueError, TypeError):
                    config = None
            # Compression tip may not retain the original delegation marker.
            if not isinstance(config, dict) or not config.get("_delegate_from"):
                lineage = db.get_compression_lineage(current)
                root = lineage[0] if lineage else current
                root_row = db.get_session(root) if root and root != current else None
                config = root_row.get("model_config") if root_row else config
                if isinstance(config, str):
                    config = json.loads(config)
            current = str(config.get("_delegate_from") or "") if isinstance(config, dict) else ""
        for sid, resolved in wanted.items():
            if resolved in ancestors:
                counts[sid] += 1
    return counts


def activity_revision():
    """Registry lifecycle cache key; never put this private identity on the wire."""
    from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
    with _active_subagents_lock:
        return tuple(sorted((str(key), id(record.get("agent")), str(record.get("status")))
                            for key, record in _active_subagents.items()))


def annotate_activity(db, rows):
    counts = active_descendant_counts(db, [row["id"] for row in rows])
    return [{**row, "active_descendant_count": counts[row["id"]]} for row in rows]


def descendant_activity(db, session_id):
    """Wire field shared by list rows and live info/resume/activate snapshots."""
    return {"active_descendant_count": active_descendant_counts(db, [session_id])[session_id]}
