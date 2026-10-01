"""Display-only durable call ledger. Canonical input excludes cache buckets.

Rate = reported output / sum of successful provider-call seconds, including
TTFT and parallel child calls (worker-seconds, not wall time or tool waits).
Never append this metadata to model messages.
"""
from __future__ import annotations

import json
import uuid

from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    db: object
    session: str
    task: str
    response: str
    delegated: bool = False


def _schema(db):
    def create(conn):
        conn.execute('CREATE TABLE IF NOT EXISTS chat_token_tasks (session TEXT PRIMARY KEY, task TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS chat_token_responses (id TEXT PRIMARY KEY, session TEXT NOT NULL, task TEXT NOT NULL, row_id INTEGER)')
        conn.execute('CREATE TABLE IF NOT EXISTS chat_token_calls (id TEXT PRIMARY KEY, session TEXT NOT NULL, task TEXT NOT NULL, response TEXT NOT NULL, usage TEXT, seconds REAL, delegated INTEGER)')
        conn.execute('CREATE INDEX IF NOT EXISTS chat_token_calls_session ON chat_token_calls(session)')
    db._execute_write(create)


def _session(agent):
    db = getattr(agent, '_session_db', None)
    sid = getattr(agent, 'session_id', None)
    if db is None or not sid:
        return None, None
    # Compression continuations share the conversation's accounting root.
    lineage = db.get_compression_lineage(sid)
    return db, lineage[0] if lineage else sid


def begin_response(agent):
    db, sid = _session(agent)
    if db is None:
        return None
    _schema(db)
    response = uuid.uuid4().hex
    def begin(conn):
        conn.execute('INSERT OR IGNORE INTO chat_token_tasks VALUES (?, ?)', (sid, uuid.uuid4().hex))
        task = conn.execute('SELECT task FROM chat_token_tasks WHERE session=?', (sid,)).fetchone()[0]
        conn.execute('INSERT INTO chat_token_responses VALUES (?, ?, ?, NULL)', (response, sid, task))
        return task
    task = db._execute_write(begin)
    agent._chat_token_scope = Scope(db, sid, task, response)
    # A process-local call identity survives session counter resets and retries.
    agent._chat_token_instance = getattr(agent, '_chat_token_instance', uuid.uuid4().hex)
    return response


def bind_child(parent, child):
    scope = getattr(parent, '_chat_token_scope', None)
    if scope is not None:
        child._chat_token_scope = Scope(scope.db, scope.session, scope.task, scope.response, True)
        child._chat_token_instance = uuid.uuid4().hex


def display_usage(canonical, raw, *, provider=None, api_mode=None):
    """Keep unsupported cache buckets unknown, using the normalizer's own field paths."""
    from agent.usage_pricing import _ANTHROPIC_USAGE_SHAPE, _CODEX_USAGE_SHAPE, _CHAT_USAGE_SHAPE
    mode = (api_mode or '').strip().lower()
    provider = (provider or '').strip().lower()
    shape = _ANTHROPIC_USAGE_SHAPE if mode == 'anthropic_messages' or provider == 'anthropic' else _CODEX_USAGE_SHAPE if mode == 'codex_responses' else _CHAT_USAGE_SHAPE
    def present(path):
        value = raw
        for part in path:
            value = value.get(part) if isinstance(value, dict) else getattr(value, part, None)
            if value is None:
                return False
        return True
    result = dict(canonical)
    for name, paths in zip(('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens'), shape):
        if not any(present(path) for path in paths):
            result[name] = None
    return result


def record_call(agent, usage, seconds):
    scope = getattr(agent, '_chat_token_scope', None)
    if scope is None:
        return
    instance = getattr(agent, '_chat_token_instance', None)
    if instance is None:
        instance = agent._chat_token_instance = uuid.uuid4().hex
    identity = f'{instance}:{scope.response}:{agent.session_api_calls}'
    scope.db._execute_write(lambda conn: conn.execute(
        'INSERT OR IGNORE INTO chat_token_calls VALUES (?, ?, ?, ?, ?, ?, ?)',
        (identity, scope.session, scope.task, scope.response,
         json.dumps(usage) if usage is not None else None, max(0, float(seconds)) if seconds is not None else None, int(scope.delegated))))


def finish_response(agent, row_id):
    scope = getattr(agent, '_chat_token_scope', None)
    if scope is not None:
        scope.db._execute_write(lambda conn: conn.execute(
            'UPDATE chat_token_responses SET row_id=? WHERE id=?', (row_id, scope.response)))


def new_task(agent):
    db, sid = _session(agent)
    if db is None:
        raise ValueError('Session storage unavailable')
    _schema(db)
    db._execute_write(lambda conn: conn.execute(
        'INSERT OR REPLACE INTO chat_token_tasks VALUES (?, ?)', (sid, uuid.uuid4().hex)))


def _totals(rows):
    keys = ('input', 'output', 'cache_read', 'cache_write')
    usages = [json.loads(row['usage']) if row['usage'] is not None else {} for row in rows]
    # An unsupported bucket remains unknown; a partly reported bucket must not
    # present its subtotal as a complete total.
    buckets = {key: sum(usage[key + '_tokens'] for usage in usages)
               if usages and all(usage.get(key + '_tokens') is not None for usage in usages) else None
               for key in keys}
    reported = {key for key in keys if any(usage.get(key + '_tokens') is not None for usage in usages)}
    required = reported | {'input', 'output'}
    missing = sum(any(usage.get(key + '_tokens') is None for key in required) for usage in usages)
    seconds = sum(row['seconds'] or 0 for row in rows)
    timed = all(row['seconds'] is not None for row in rows)
    total = sum(value or 0 for value in buckets.values()) if reported and not missing else None
    return {**buckets, 'total': total, 'calls': len(rows), 'missing_calls': missing,
            'delegated_calls': sum(row['delegated'] for row in rows), 'generation_seconds': seconds,
            'tokens_per_second': buckets['output'] / seconds if seconds > 0 and timed and buckets['output'] is not None else None}


def snapshot(agent):
    db, sid = _session(agent)
    if db is None:
        return None
    # No schema creation on the status hot path; old chats honestly have no ledger.
    with db._read_ctx() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='chat_token_tasks'").fetchone():
            return None
        task = conn.execute('SELECT task FROM chat_token_tasks WHERE session=?', (sid,)).fetchone()
        if task is None:
            return None
        calls = [dict(row) for row in conn.execute('SELECT * FROM chat_token_calls WHERE session=?', (sid,))]
        responses = [dict(row) for row in conn.execute('SELECT * FROM chat_token_responses WHERE session=? AND row_id IS NOT NULL', (sid,))]
    scope = getattr(agent, '_chat_token_scope', None)
    return {'task_id': task[0], 'session': _totals(calls),
            'task': _totals([row for row in calls if row['task'] == task[0]]),
            'response': _totals([row for row in calls if scope is not None and row['response'] == scope.response]),
            'responses': {str(r['row_id']): _totals([row for row in calls if row['response'] == r['id']]) for r in responses}}
