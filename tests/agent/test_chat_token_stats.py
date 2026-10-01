"""Durable display accounting never changes the model transcript."""
from types import SimpleNamespace

from hermes_state import SessionDB


def test_corrections_keep_task_and_nested_usage_is_once_after_resume(tmp_path):
    from agent.chat_token_stats import begin_response, bind_child, record_call, finish_response, snapshot, new_task
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('chat', source='desktop', model='test')
    parent = SimpleNamespace(session_id='chat', _session_db=db, session_api_calls=1)
    first = begin_response(parent)
    child = SimpleNamespace(session_id='child', session_api_calls=1)
    bind_child(parent, child)
    grandchild = SimpleNamespace(session_id='grandchild', session_api_calls=1)
    bind_child(child, grandchild)
    usage = {'input_tokens': 30, 'output_tokens': 10, 'cache_read_tokens': 50, 'cache_write_tokens': 5}
    record_call(parent, usage, 2)
    record_call(grandchild, usage, 2)
    record_call(grandchild, usage, 2)
    finish_response(parent, 42)
    parent.session_api_calls += 1
    begin_response(parent)
    record_call(parent, usage, 2)
    # Late child spend stays assigned to the response/task that spawned it.
    child.session_api_calls += 1
    record_call(child, usage, 2)
    resumed = SimpleNamespace(session_id='chat', _session_db=db)
    stats = snapshot(resumed)
    assert stats['task']['total'] == stats['session']['total'] == 4 * 95
    assert stats['session']['tokens_per_second'] == 5
    assert stats['responses'][str(42)]['total'] == 3 * 95
    assert stats['responses'][str(42)]['delegated_calls'] == 2
    assert first != begin_response(resumed)
    new_task(resumed)
    assert snapshot(resumed)['task']['total'] is None
    assert snapshot(resumed)['session']['total'] == 4 * 95
    db.close()


def test_missing_usage_and_profile_isolation(tmp_path):
    from agent.chat_token_stats import begin_response, record_call, snapshot
    for name in ('a', 'b'):
        db = SessionDB(tmp_path / f'{name}.db')
        db.create_session('same', source='desktop', model='test')
        agent = SimpleNamespace(session_id='same', _session_db=db, session_api_calls=1)
        begin_response(agent)
        record_call(agent, None if name == 'a' else {'input_tokens': 0, 'output_tokens': 0}, 1)
        result = snapshot(agent)['session']
        assert result['total'] == (None if name == 'a' else 0)
        assert result['missing_calls'] == (1 if name == 'a' else 0)
        assert result['cache_write'] is None
        db.close()


def test_response_counter_restart_does_not_drop_a_real_call(tmp_path):
    from agent.chat_token_stats import begin_response, record_call, snapshot
    db = SessionDB(tmp_path / 'restart.db')
    db.create_session('chat', source='desktop', model='test')
    agent = SimpleNamespace(session_id='chat', _session_db=db, session_api_calls=1)
    begin_response(agent)
    record_call(agent, {'input_tokens': 1, 'output_tokens': 2}, 1)
    begin_response(agent)
    record_call(agent, {'input_tokens': 1, 'output_tokens': 2}, 1)
    assert snapshot(agent)['session']['calls'] == 2
    db.close()


def test_partial_calls_do_not_claim_complete_totals_or_speed(tmp_path):
    from agent.chat_token_stats import begin_response, record_call, snapshot
    db = SessionDB(tmp_path / 'partial.db')
    db.create_session('chat', source='desktop', model='test')
    agent = SimpleNamespace(session_id='chat', _session_db=db, session_api_calls=1)
    begin_response(agent)
    record_call(agent, {'input_tokens': 100, 'output_tokens': 10}, 2)
    agent.session_api_calls += 1
    record_call(agent, {'input_tokens': 100, 'output_tokens': None}, 2)
    totals = snapshot(agent)['session']
    assert totals['output'] is None
    assert totals['total'] is None
    assert totals['missing_calls'] == 1
    assert totals['tokens_per_second'] is None
    agent.session_api_calls += 1
    record_call(agent, None, 50)
    totals = snapshot(agent)['session']
    assert totals['generation_seconds'] == 54
    assert totals['missing_calls'] == 2
    assert totals['tokens_per_second'] is None
    db.close()


def test_partial_codex_appserver_usage_preserves_unknown_display_fields(tmp_path):
    from agent.chat_token_stats import begin_response, snapshot
    from agent.codex_runtime import _record_codex_app_server_usage
    db = SessionDB(tmp_path / 'codex.db')
    db.create_session('chat', source='desktop', model='test')
    agent = SimpleNamespace(session_id='chat', _session_db=db, _session_db_created=True,
                            session_api_calls=0, model='test', provider='openai', base_url='https://stub.invalid',
                            session_estimated_cost_usd=0)
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens', 'output_tokens',
                'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens'):
        setattr(agent, 'session_' + key, 0)
    begin_response(agent)
    _record_codex_app_server_usage(agent, SimpleNamespace(token_usage_last={'inputTokens': 100}))
    totals = snapshot(agent)['session']
    assert totals['output'] is None
    assert totals['cache_read'] is None
    assert totals['input'] is None  # Inclusive input cannot be split without cached input.
    assert totals['total'] is None
    assert totals['missing_calls'] == 1
    db.close()


def test_absent_output_is_unknown_not_zero():
    from agent.chat_token_stats import display_usage
    from agent.usage_pricing import normalize_usage
    raw = {'prompt_tokens': 12}
    canonical = normalize_usage(raw)
    stats = display_usage({'input_tokens': canonical.input_tokens, 'output_tokens': canonical.output_tokens}, raw)
    assert stats['input_tokens'] == 12
    assert stats['output_tokens'] is None


def test_compression_continuation_and_reopened_database_keep_totals(tmp_path):
    from agent.chat_token_stats import begin_response, record_call, snapshot
    path = tmp_path / 'durable.db'
    db = SessionDB(path)
    db.create_session('root', source='desktop', model='test')
    root = SimpleNamespace(session_id='root', _session_db=db, session_api_calls=1)
    begin_response(root)
    record_call(root, {'input_tokens': 10, 'output_tokens': 2}, 1)
    task = snapshot(root)['task_id']
    db.end_session('root', 'compression')
    db.create_session('tip', source='desktop', model='test', parent_session_id='root')
    db.close()
    db = SessionDB(path)
    tip = SimpleNamespace(session_id='tip', _session_db=db, session_api_calls=1)
    begin_response(tip)
    record_call(tip, {'input_tokens': 10, 'output_tokens': 2}, 1)
    assert snapshot(tip)['task_id'] == task
    assert snapshot(tip)['session']['total'] == 24
    db.close()
