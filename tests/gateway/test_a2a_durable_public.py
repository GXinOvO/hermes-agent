"""Crash a real completion producer, then consume via native A2A HTTP readers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

import pytest


PEER = Path(__file__).with_name('a2a_durable_peer.py')


def child_env(home):
    env = dict(os.environ, HERMES_HOME=str(home), HOME=str(home / 'user'))
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    return env


def seed(home, mode='seal'):
    process = subprocess.Popen([sys.executable, str(PEER), str(home), mode], env=child_env(home),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        first = json.loads(process.stdout.readline())
        raw = json.dumps({'jsonrpc': '2.0', 'id': 'producer', 'method': 'SendMessage',
                          'params': {'message': {'role': 'ROLE_USER', 'messageId': 'one-input',
                                                 'contextId': 'real-context', 'parts': [{'text': 'do the work once'}]}}}).encode()
        request = urllib.request.Request(first['url'], data=raw, headers={'Content-Type': 'application/json'})
        try:
            urllib.request.urlopen(request, timeout=15).read()
        except (OSError, urllib.error.URLError):
            pass
        output, error = process.communicate(timeout=20)
        print('SEAL_RECEIPT', json.dumps({'exit': process.returncode, 'stdout': output, 'stderr': error}))
        assert process.returncode == 23, error
        assert (home / 'executions').read_text() == '1'
        return json.loads((home / 'producer.json').read_text())
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def recover(home, mode):
    env = child_env(home)
    env['A2A_REPLY_TIMEOUT'] = '1'
    result = subprocess.run([sys.executable, str(PEER), str(home), mode], env=env,
                            capture_output=True, text=True, timeout=35, check=False)
    print('RECOVERY_RECEIPT', json.dumps({'mode': mode, 'exit': result.returncode,
                                        'stdout': result.stdout, 'stderr': result.stderr}))
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize('mode', ['recover', 'lost'])
def test_a2a_crashed_producer_durable_public_consumption(mode, tmp_path):
    identity = seed(tmp_path)
    first = recover(tmp_path, mode)
    assert first['delivered'] == int(mode == 'recover')
    assert (first['part']['state'], first['part']['attempts']) == ('delivered' if mode == 'recover' else 'unknown', 1)
    for key, raw in first['public'].items():
        assert 'sealed native answer' in raw, (key, raw)
        assert identity['task'] in raw, (key, raw)
    assert json.loads(first['other'])['error']['code'] == -32001
    second = recover(tmp_path, 'recover')
    assert second['delivered'] == 0
    assert second['executions'] == '1'
    assert second['part']['attempts'] == 1
    assert 'sealed native answer' in second['public']['GetTask']


def test_a2a_wrong_context_cannot_consume_sealed_result(tmp_path):
    seed(tmp_path)
    wrong = recover(tmp_path, 'wrong')
    assert not wrong['success']
    assert (wrong['part']['state'], wrong['part']['attempts']) == ('pending', 0)
    assert recover(tmp_path, 'recover')['delivered'] == 1


def test_a2a_completed_input_resend_returns_original_public_task(tmp_path):
    identity = seed(tmp_path)
    assert recover(tmp_path, 'recover')['delivered'] == 1
    repeated = recover(tmp_path, 'resend')
    task = json.loads(repeated['response'])['result']['task']
    assert task['id'] == identity['task']
    assert 'sealed native answer' in repeated['response']
    assert repeated['executions'] == '1'


def test_a2a_existing_public_subscription_receives_durable_publication(tmp_path):
    seed(tmp_path)
    assert recover(tmp_path, 'watching')['delivered'] == 1


@pytest.mark.parametrize('mode', ['wrong-payload', 'wrong-task', 'wrong-metadata', 'unclaimed'])
def test_a2a_strict_send_requires_exact_sealed_result_claim(mode, tmp_path):
    seed(tmp_path)
    result = recover(tmp_path, mode)
    assert not result['success']
    assert (result['part']['state'], result['part']['attempts']) == ('pending', 0)
    assert recover(tmp_path, 'recover')['delivered'] == 1
