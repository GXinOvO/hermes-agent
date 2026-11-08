"""Task-authorized Docker fixture for the protected provider's real native callers."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli.plugins import PluginManager
from hermes_constants import get_hermes_home
from tools.registry import registry as tools

IMAGE = 'sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3'
LABEL = 'scheduled-bot-iteration-20261106-protected-owned-r19-qualification'
DOCKER = ['/usr/bin/sudo', '-n', '/usr/bin/docker']


class Native:
    """Record actual dispatch and exact resource observations without replacing execution."""
    def __init__(self, root, config, registry):
        self.root, self.config, self.registry = root, config, registry
        self.events = []
        self.env = None
        self.owner = 'protected-r19-owner'
        self.task = 'protected-r19-native'

    def record(self, kind, value):
        self.events.append({'kind': kind, 'value': value})
        (self.root / 'observations.json').write_text(json.dumps(self.events, indent=2))
        return value

    def tool(self, name, args):
        with scoped_current_session_id(self.owner):
            value = json.loads(tools.get_entry(name).handler(args, task_id=self.task, session_id=self.task))
        return self.record(name, value)

    def terminal(self, command, **options):
        return self.tool('terminal', {'command': command, **options})

    def python(self, script):
        value = self.terminal('python3 -B -c ' + shlex.quote(script), timeout=10)
        assert value.get('exit_code') == 0, value
        return self.record('python_json', json.loads(value['output']))

    def process(self, action, sid=None, **options):
        args = {'action': action, **options}
        if sid is not None:
            args['session_id'] = sid
        return self.tool('process_manage', args)

    def docker(self, args, absent=False):
        argv = DOCKER + args
        p = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
        value = self.record('docker', {'argv': argv, 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
        assert p.returncode == 0 or (absent and 'no such' in p.stderr.lower()), value
        return value

    def bytes(self, path):
        return bytes.fromhex(self.python('from pathlib import Path;import json;print(json.dumps(Path(' + repr(path) + ').read_bytes().hex()))'))


@pytest.fixture
def protected_native(monkeypatch, request):
    root_value = os.environ.get('R19_QUALIFICATION_ROOT')
    if not root_value:
        pytest.skip('requires explicit task-authorized protected Docker qualification root')
    root = Path(root_value) / request.node.name
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    (root / 'state').mkdir(mode=0o700)
    authority = Path(os.environ['R19_AUTHORITY_ROOT'])
    config = {'authority_root': str(authority), 'state_root': str(root / 'state'),
              'volume': 'hermes-protected-r19-q-' + hashlib.sha256(str(root).encode()).hexdigest()[:24],
              'label': LABEL, 'image': IMAGE}
    contents = json.dumps({'plugins': {'enabled': ['owned-process'], 'isolation': 'in_process',
                            'entries': {'owned-process': {'settings': {'protected': config}}}},
                          'terminal': {'backend': 'protected_owned', 'cwd': '/cas', 'timeout': 12}})
    (get_hermes_home() / 'config.yaml').write_text(contents)
    (root / 'config.json').write_text(contents)
    from tools import process_registry as pr
    from tools import terminal_tool as terminal
    monkeypatch.setenv('TERMINAL_ENV', 'protected_owned')
    monkeypatch.setenv('TERMINAL_CWD', '/cas')
    monkeypatch.setenv('TERMINAL_TIMEOUT', '12')
    for name in ('_active_environments', '_last_activity', '_task_env_overrides', '_session_cwd'):
        monkeypatch.setattr(terminal, name, {})
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, 'process_registry', registry)
    native = Native(root, config, registry)
    manager = PluginManager()
    manager.discover_and_load()
    from agent.terminal_env_registry import get_provider
    provider = get_provider('protected_owned')
    plugin = sys.modules[provider.__class__.__module__]
    owner_module = sys.modules[plugin.ProtectedOwnedEnvironment.__mro__[1].__module__]
    finish = owner_module.RemoteHandle.finish
    def observed_finish(handle, code, lifecycle=None):
        value = finish(handle, code, lifecycle)
        native.record('wire_finish', {'id': handle.identifier, 'code': code, 'owned_lifecycle': lifecycle})
        return value
    monkeypatch.setattr(owner_module.RemoteHandle, 'finish', observed_finish)
    try:
        assert manager._plugins['owned-process'].error is None
        initial = native.terminal('printf QUALIFIED; pwd; printf lawful > /cas/lawful.txt', timeout=10)
        assert initial.get('exit_code') == 0 and 'QUALIFIED/cas' in initial['output'], initial
        assert len(terminal._active_environments) == 1
        native.env = next(iter(terminal._active_environments.values()))
        native.record('native_environment', {'type': type(native.env).__name__, 'module': type(native.env).__module__,
            'worker': native.env.receipt, 'container': native.env.service.active, 'state': str(native.env.state)})
        from tools import file_tools
        assert file_tools._get_file_ops(native.task).env is native.env
        yield native
    finally:
        for session in list(registry._running.values()):
            native.process('kill', session.id)
        terminal.cleanup_all_environments()
        if native.env is not None:
            native.env.cleanup()
            native.record('closed_environment', {'worker_returncode': native.env.proc.returncode,
                'reader_alive': native.env.reader.is_alive(), 'cleanup_complete': native.env.cleanup_complete,
                'handles': [{'id': h.identifier, 'pump_alive': h._pump.is_alive(), 'lifecycle': h.lifecycle}
                            for h in native.env.all_handles], 'file_adapter': native.env.receipts})
        manager.unload()
        for state in (root / 'state').iterdir():
            cleanup = state / 'cleanup.json'
            if cleanup.is_file():
                for cid in json.loads(cleanup.read_text())['ids']:
                    native.docker(['container', 'inspect', cid], absent=True)
        inspected = native.docker(['volume', 'inspect', config['volume']], absent=True)
        if inspected['exit'] == 0:
            volume = json.loads(inspected['stdout'])[0]
            assert volume['Name'] == config['volume']
            assert volume['Labels']['hermes.task'] == LABEL
            assert volume['Labels']['hermes.owner'] == hashlib.sha256(config['state_root'].encode()).hexdigest()
            native.docker(['volume', 'rm', config['volume']])
            removed = native.docker(['volume', 'inspect', config['volume']], absent=True)
            assert removed['exit'] != 0
        request.node.user_properties.append(('native_receipt', str(root / 'observations.json')))
