"""Registry + on-disk lineage probes; no agent construction or live home access."""
from types import SimpleNamespace

import pytest


@pytest.fixture
def activity(tmp_path):
    from hermes_state import SessionDB
    from tools import delegate_tool_registry as registry

    saved = dict(registry._active_subagents)
    registry._active_subagents.clear()
    a = SessionDB(tmp_path / "a" / "state.db")
    b = SessionDB(tmp_path / "b" / "state.db")
    for db in (a, b):
        db.create_session("parent", "cli")
    yield a, b
    registry._active_subagents.clear()
    registry._active_subagents.update(saved)
    a.close()
    b.close()


def register(db, sid, parent, *, tree_parent=None):
    from tools.delegate_tool_child_run import _register_child
    db.create_session(sid, "subagent", parent_session_id=parent,
                      model_config={"_delegate_from": parent})
    child = SimpleNamespace(_subagent_id=sid, _parent_subagent_id=tree_parent,
                            _delegate_depth=1, _parent_session_id=parent,
                            session_id=sid, _session_db=db, model="test")
    _register_child(child, None, "work", owner_session_id=None,
                    owner_transport=None, owner_session_record=None)
    return child


def test_registry_lifecycle_is_observable_without_parent_or_database_writes(activity, monkeypatch):
    from tui_gateway import server as change_watcher
    from tools.delegate_tool_registry import _unregister_subagent
    a, b = activity
    monkeypatch.setattr(change_watcher, "_watcher_home", lambda: a.db_path.parent)
    monkeypatch.setattr(change_watcher, "_served_profile_homes", {b.db_path.parent})
    child = register(a, "child", "parent")
    before = change_watcher._sessions_sig()
    _unregister_subagent("child", agent=child)
    after = change_watcher._sessions_sig()
    assert before != after
    assert after == change_watcher._sessions_sig()


def test_cold_rpc_list_publishes_authoritative_count(activity, monkeypatch):
    from tui_gateway import server
    a, _ = activity
    monkeypatch.setattr(server, "_get_db", lambda: a)
    register(a, "child", "parent")
    result = server.handle_request({"id": 1, "method": "session.list", "params": {}})
    assert result["result"]["sessions"][0]["active_descendant_count"] == 1


def test_info_snapshot_tracks_child_finish_independent_of_parent_running(activity, monkeypatch):
    from tui_gateway import server
    a, _ = activity
    monkeypatch.setattr(server, "_get_db", lambda: a)
    monkeypatch.setattr(server, "_session_live_title", lambda *args: "")
    monkeypatch.setattr(server, "_session_usage_snapshot", lambda *args: {})
    child = register(a, "child", "parent")
    owner = {"session_key": "parent", "running": False, "agent": None}
    info = server._session_info(None, owner)
    assert info["running"] is False
    assert info["active_descendant_count"] == 1
    from tools.delegate_tool_registry import _unregister_subagent
    _unregister_subagent("child", agent=child)
    assert server._session_info(None, owner)["active_descendant_count"] == 0


def test_cold_counts_need_no_live_parent_or_transport(activity):
    from tui_gateway.session_descendant_activity import active_descendant_counts
    a, _ = activity
    register(a, "child", "parent")
    assert active_descendant_counts(a, ["parent", "unrelated"]) == {"parent": 1, "unrelated": 0}


def test_nested_child_survives_finished_intermediate_parent(activity):
    from tui_gateway.session_descendant_activity import active_descendant_counts
    from tools.delegate_tool_registry import _unregister_subagent
    a, _ = activity
    register(a, "child", "parent")
    register(a, "grandchild", "child", tree_parent="child")
    assert active_descendant_counts(a, ["parent", "child"]) == {"parent": 2, "child": 1}
    _unregister_subagent("child")
    assert active_descendant_counts(a, ["parent", "child"]) == {"parent": 1, "child": 1}


@pytest.mark.parametrize("outcome", ["completed", "cancelled", "failed"])
def test_terminal_cleanup_returns_explicit_zero(activity, outcome):
    from tui_gateway.session_descendant_activity import active_descendant_counts
    from tools.delegate_tool_registry import _active_subagents, _unregister_subagent
    a, _ = activity
    child = register(a, "child", "parent")
    assert active_descendant_counts(a, ["parent"])["parent"] == 1
    _active_subagents["child"]["status"] = outcome
    assert active_descendant_counts(a, ["parent"])["parent"] == 0
    _unregister_subagent("child", agent=child)
    assert active_descendant_counts(a, ["parent"]) == {"parent": 0}


def test_profile_isolation_a_b_a_even_when_session_ids_collide(activity):
    from tui_gateway.session_descendant_activity import active_descendant_counts
    a, b = activity
    register(a, "child-a", "parent")
    assert active_descendant_counts(a, ["parent"]) == {"parent": 1}
    assert active_descendant_counts(b, ["parent"]) == {"parent": 0}
    register(b, "child-b", "parent")
    assert active_descendant_counts(b, ["parent"]) == {"parent": 1}
    assert active_descendant_counts(a, ["parent"]) == {"parent": 1}


def test_compression_and_reopened_db_do_not_lose_live_children(activity):
    from hermes_state import SessionDB
    from tui_gateway.session_descendant_activity import active_descendant_counts
    a, _ = activity
    register(a, "child", "parent")
    a.create_session("tip", "cli", parent_session_id="parent")
    a.append_message("tip", "user", "continue")
    assert a.resolve_resume_session_id("parent") == "tip"
    with SessionDB(a.db_path) as reconnected:
        assert active_descendant_counts(reconnected, ["parent", "tip"]) == {"parent": 1, "tip": 1}


def test_branch_siblings_and_unknown_profile_records_fail_closed(activity):
    from tools.delegate_tool_registry import _register_subagent
    from tui_gateway.session_descendant_activity import active_descendant_counts
    a, _ = activity
    a.create_session("branch", "cli", parent_session_id="parent",
                     model_config={"_branched_from": "parent"})
    register(a, "branch-child", "branch")
    _register_subagent({"subagent_id": "unscoped", "owner_agent_session_id": "parent", "status": "running"})
    assert active_descendant_counts(a, ["parent", "branch"]) == {"parent": 0, "branch": 1}
