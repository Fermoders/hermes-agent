"""Real provider telemetry reaches the gateway's typed display usage."""
from types import SimpleNamespace
from hermes_state import SessionDB


def test_usage_less_response_is_visible_in_gateway(tmp_path):
    from agent.chat_token_stats import begin_response
    from agent.turn_usage import record_response_usage
    from tui_gateway.server import _get_usage
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('chat', source='desktop', model='test')
    agent = SimpleNamespace(session_id='chat', _session_db=db, session_api_calls=0,
                            context_compressor=None, model='test', provider='test')
    begin_response(agent)
    record_response_usage(agent, SimpleNamespace(usage=None), messages=[], api_call_count=1,
                          api_duration=2, compression_attempts=0, max_compression_attempts=3)
    stats = _get_usage(agent).get('token_stats')
    assert stats is not None
    assert stats['session']['missing_calls'] == 1
    assert stats['session']['total'] is None
    db.close()


def test_real_provider_cache_buckets_and_explicit_task_rpc(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    from run_agent import AIAgent
    from agent.chat_token_stats import begin_response, finish_response
    from agent.turn_usage import record_response_usage
    from tui_gateway import server
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('chat', source='desktop', model='test')
    agent = AIAgent(api_key='test', base_url='https://example.invalid/v1', provider='openai',
                    api_mode='chat_completions', model='test', session_id='chat', platform='cli',
                    quiet_mode=True, skip_context_files=True, skip_memory=True,
                    save_trajectories=False, enabled_toolsets=['file'])
    agent._session_db = db
    begin_response(agent)
    usage = SimpleNamespace(prompt_tokens=85, completion_tokens=10,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=50, cache_write_tokens=5),
                            completion_tokens_details=None)
    record_response_usage(agent, SimpleNamespace(usage=usage), messages=[], api_call_count=1,
                          api_duration=2, compression_attempts=0, max_compression_attempts=3)
    finish_response(agent, 42)
    stats = server._get_usage(agent)['token_stats']
    assert stats['session']['input'] == 30
    assert stats['session']['total'] == 95
    assert stats['session']['tokens_per_second'] == 5
    assert stats['responses']['42']['cache_write'] == 5
    sid = 'token-test-rpc'
    session = {'agent': agent, 'history': [], 'running': True, 'session_key': 'chat'}
    monkeypatch.setitem(server._sessions, sid, session)
    assert server._methods['session.tokens.new_task']('busy', {'session_id': sid})['error']['code'] == 4002
    session['running'] = False
    result = server._methods['session.tokens.new_task']('reset', {'session_id': sid})
    assert 'error' not in result
    assert result['result']['token_stats']['task']['total'] is None
    assert result['result']['token_stats']['session']['total'] == 95
    agent.close()
    db.close()


def test_lazy_resumed_chat_reads_durable_stats_without_building_agent(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from agent.chat_token_stats import begin_response, record_call
    from tui_gateway import server
    db = SessionDB(tmp_path / 'lazy.db')
    db.create_session('chat', source='desktop', model='test')
    agent = SimpleNamespace(session_id='chat', _session_db=db, session_api_calls=1)
    begin_response(agent)
    record_call(agent, {'input_tokens': 10, 'output_tokens': 2}, 1)
    @contextmanager
    def owner_db(session):
        yield db
    monkeypatch.setattr(server, '_session_db', owner_db)
    stats = server._session_usage_snapshot({'agent': None, 'session_key': 'chat'})
    assert stats['token_stats']['session']['total'] == 12
    db.close()
