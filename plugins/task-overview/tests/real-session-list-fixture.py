import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from hermes_state import SessionDB
from hermes_cli.web_routers import profiles

root = Path(os.environ['FOLLOWUP_FIXTURE_ROOT']).resolve()
home = Path(os.environ['HERMES_HOME']).resolve()
if not home.is_relative_to(root):
    raise RuntimeError('The fixture requires an isolated task-owned HERMES_HOME')
home.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='session-list-', dir=home) as temp:
    fixture = Path(temp)
    db = SessionDB(db_path=fixture / 'state.db')
    try:
        for i in range(102):
            sid = f'fixture-recent-{i:03d}'
            db.create_session(sid, 'cli', profile_name='fixture-owner')
            db.set_session_title(sid, f'Synthetic session {i:03d}')
            db._write_sql('UPDATE sessions SET started_at = ? WHERE id = ?', (1000 + i, sid))
        db.create_session('fixture-pinned-old', 'cli', profile_name='fixture-owner')
        db.set_session_title('fixture-pinned-old', 'Synthetic pinned session')
        db._write_sql('UPDATE sessions SET started_at = ? WHERE id = ?', (1, 'fixture-pinned-old'))
        db.set_session_pinned('fixture-pinned-old', True)
        db.create_session('fixture-hidden', 'cli', profile_name='fixture-owner')
        db.set_session_title('fixture-hidden', 'Synthetic hidden session')
        db.set_session_hidden('fixture-hidden', True)
        db.create_session('fixture-compression-root', 'cli', profile_name='fixture-owner')
        db.set_session_title('fixture-compression-root', 'Synthetic compression session')
        db.end_session('fixture-compression-root', 'compression')
        db.create_session('fixture-compression-tip', 'cli', parent_session_id='fixture-compression-root', profile_name='fixture-owner')
        db._write_sql('UPDATE sessions SET started_at = ? WHERE id = ?', (2000, 'fixture-compression-root'))
    finally:
        db.close()
    with patch.object(profiles, '_cron_profile_home', lambda name: (name, fixture)):
        response = profiles.get_profiles_sessions(
            limit=100, offset=0, min_messages=0, archived='exclude', order='created',
            profile='fixture-owner', source=None, sources=None, exclude_sources=None, full=False)
    assert response['errors'] == [], response['errors']
    assert response['storage'] == {}, response['storage']
    sessions = response['sessions']
    assert all(row['profile'] == 'fixture-owner' for row in sessions)
    assert not any(row['id'] == 'fixture-hidden' for row in sessions)
    assert any(row['id'] == 'fixture-pinned-old' and row['pinned'] for row in sessions)
    lineage = [row for row in sessions if row.get('_lineage_root_id') == 'fixture-compression-root']
    assert len(lineage) == 1 and lineage[0]['id'] == 'fixture-compression-tip', lineage
    safe = {**response, 'sessions': [{key: row.get(key) for key in ('id', '_lineage_root_id', 'profile', 'title', 'pinned', 'unread')} for row in sessions]}
    output = root / 'real-session-list-fixture.json'
    output.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'result': 'PASS', 'fixture': str(output), 'returned_rows': len(sessions), 'reported_total': response['profile_totals']['fixture-owner'], 'pinned_backfill': True, 'compression_tip_root': True, 'hidden_excluded': True}))
