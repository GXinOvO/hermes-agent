from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import queue
import shlex
import threading
import time

from tests.gateway.fixtures.local_recovery_probe import child_env, daemon
from tests.gateway.test_default_public_a2a_startup import read_state
from tests.gateway.test_handoff_completion_parent_context import ScriptedModel, _call
from tests.gateway.test_native_telegram_startup_recovery import BotAPI, wait_for


class AutomationModel(ScriptedModel):
    def _reply(self, messages):
        last_user = next((str(m.get('content') or '') for m in reversed(messages) if m['role'] == 'user'), '')
        if 'Background process ' in last_user and 'AUTOMATION_PROCESS_DONE_07' in last_user:
            return 'DEFAULT_AUTOMATION_07_SEALED'
        called = any(call['function']['name'] == 'terminal' for m in messages if m['role'] == 'assistant'
                     for call in m.get('tool_calls') or [])
        if not called and 'START_PROCESS_07' in last_user:
            return _call('terminal', command='python ' + shlex.quote(str(self.server.script)), background=True, notify=True)
        return 'AUTOMATION_PROCESS_STARTED_07'


def test_default_background_completion_recovery_does_not_execute_again(tmp_path):
    root = Path(__file__).resolve().parents[2]
    home, user = tmp_path / 'state', tmp_path / 'user'
    home.mkdir(mode=0o700)
    user.mkdir()
    release, counter = home / 'release', home / 'business-calls'
    script = home / 'business.py'
    script.write_text('from pathlib import Path\nimport time\nh=Path(__file__).parent\n'
        'while not (h/"release").exists(): time.sleep(.05)\n'
        'with (h/"business-calls").open("a") as f: f.write("executed\\n")\n'
        'print("AUTOMATION_PROCESS_DONE_07")\n')
    model = ThreadingHTTPServer(('127.0.0.1', 0), AutomationModel)
    model.requests, model.lock, model.script = [], threading.Lock(), script
    bot = ThreadingHTTPServer(('127.0.0.1', 0), BotAPI)
    bot.calls, bot.sent, bot.updates = [], [], queue.Queue()
    threads = [threading.Thread(target=peer.serve_forever, daemon=True) for peer in (model, bot)]
    for thread in threads:
        thread.start()
    base = f'http://127.0.0.1:{model.server_port}/v1'
    config = {'model': {'provider': 'custom', 'default': 'local-wire-stub', 'base_url': base,
                        'api_key': 'loopback-only', 'api_mode': 'chat_completions'},
        'gateway': {'multiplex_profiles': False},
        'platforms': {'telegram': {'enabled': True, 'extra': {
            'base_url': f'http://127.0.0.1:{bot.server_port}/bot', 'dm_policy': 'allowlist'}}},
        'auxiliary': {'title_generation': {'enabled': False}}, 'platform_toolsets': {'telegram': ['terminal']},
        'display': {'background_process_notifications': 'result'}, 'terminal': {'cwd': str(home)}}
    (home / 'config.yaml').write_text(json.dumps(config))
    env = child_env()
    env.update(HOME=str(user), USERPROFILE=str(user), HERMES_HOME=str(home), PYTHONPATH=str(root),
        OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=base, PYTHONUNBUFFERED='1',
        HERMES_DISABLE_LAZY_INSTALLS='1', TELEGRAM_BOT_TOKEN='987654321:owned-loopback-fixture',
        TELEGRAM_ALLOWED_USERS='202')
    target = 'DEFAULT_AUTOMATION_07_SEALED'

    def replies(marker):
        return [body for body in bot.sent if marker in str(body.get('text', '')).replace('\\', '')]

    def diagnostic():
        return repr(read_state(home)) + '\n' + repr(bot.sent) + '\n' + '\n'.join(
            path.read_text() for path in (home / 'logs').glob('*.log'))

    boots = []
    try:
        with daemon(root, home, env, barrier=True, fixture='default_automation_crash.py') as (proc, desc):
            bot.updates.put({'update_id': 1, 'message': {'message_id': 1, 'date': int(time.time()),
                'chat': {'id': 202, 'type': 'private', 'first_name': 'Owned'},
                'from': {'id': 202, 'is_bot': False, 'first_name': 'Owned'}, 'text': 'START_PROCESS_07'}})
            wait_for(lambda: replies('AUTOMATION_PROCESS_STARTED_07'), diagnostic, timeout=45)
            release.touch()
            assert proc.wait(timeout=60) == 23, diagnostic()
            before = json.loads((home / 'automation-preclaim-exit.json').read_text())['pending']
            rows, _ = read_state(home)
            row = next(row for row in rows if row['admission_id'] == before['binding']['admission_id'])
            payload = json.loads(row['payload_json'])
            assert row['status'] == 'terminal' and row['outcome'] == 'completed'
            assert row['principal_id'].startswith('automation:')
            assert before['binding']['policy'] == 'strict_automation_v1'
            assert payload['native_text_v1']['automation']['result_recovery_v1'] == 'consume_only'
            assert not replies(target) and counter.read_text().splitlines() == ['executed']
            boots.append({'pid': proc.pid, 'epoch': desc['authority_epoch'], 'stage': 'default_completion_settled',
                          'policy': before['binding']['policy'], 'admission': row['admission_id']})
        original_model_count = len(model.requests)
        for _ in range(2):
            with daemon(root, home, env, barrier=False) as (proc, desc):
                wait_for(lambda: len(replies(target)) == 1, diagnostic, timeout=35)
                _, pending = read_state(home)
                value = pending['gateway.pending_final.v1:' + row['admission_id']]
                assert value['binding'] == before['binding']
                assert value['parts']['text']['state'] == 'delivered' and value['parts']['text']['attempts'] == 1
                assert value['parts']['text']['delivered_at'] >= value['created_at']
                assert counter.read_text().splitlines() == ['executed']
                assert len(model.requests) == original_model_count
                boots.append({'pid': proc.pid, 'epoch': desc['authority_epoch'], 'stage': 'plain_gateway_restart',
                              'admission': row['admission_id'], 'part': value['parts']['text']})
        assert len(replies(target)) == 1 and len({boot['pid'] for boot in boots}) == 3
        assert boots[0]['epoch'] < boots[1]['epoch'] < boots[2]['epoch']
        assert boots[1]['part'] == boots[2]['part']
        notifications = [body for body in model.requests if 'Background process ' in next(
            (str(m.get('content') or '') for m in reversed(body['messages']) if m['role'] == 'user'), '')]
        assert len(notifications) == 1
        print('DEFAULT_AUTOMATION_07 ' + json.dumps({'boots': boots, 'business_executions': 1,
            'completion_model_calls': len(notifications), 'all_model_calls': len(model.requests),
            'completion_wire_sends': len(replies(target)), 'no_model_after_restart': True}))
    finally:
        release.touch()
        for peer in (model, bot):
            peer.shutdown()
            peer.server_close()
        for thread in threads:
            thread.join(timeout=5)
