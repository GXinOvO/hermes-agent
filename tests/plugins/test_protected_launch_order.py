"""Trusted launch order; OS permission and namespace controls run separately."""
import importlib.util
from pathlib import Path
import sys
import threading

import pytest


def load_helper(name):
    path = Path(__file__).resolve().parents[2] / 'plugins' / 'owned-process' / (name + '.py')
    spec = importlib.util.spec_from_file_location('launch_test_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('protected, expected', [(True, '/'), (False, '/workspace')])
def test_worker_launches_from_trusted_accessible_cwd(monkeypatch, protected, expected):
    worker = load_helper('worker')
    request = {'id': 'launch-order', 'command': 'business-command'}
    worker.jobs[request['id']] = {'control_lock': threading.Lock(), 'cancelled': False}
    calls = []
    class StopBeforeLaunch(Exception):
        pass
    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        raise StopBeforeLaunch('captured trusted launcher before execution')
    monkeypatch.setattr(worker.subprocess, 'Popen', popen)
    monkeypatch.setattr(worker, 'emit', lambda message: None)
    monkeypatch.setattr(sys, 'argv', ['worker.py', '--protected'] if protected else ['worker.py'])
    worker.execute(request)
    assert len(calls) == 1
    argv, options = calls[0]
    assert options['cwd'] == expected
    assert 'preexec_fn' not in options
    assert options['close_fds'] is True
    assert '-B' in argv
    assert options['env'].get('LD_LIBRARY_PATH') == ('/opt/runtime/lib' if protected else None)
    assert request['id'] not in worker.jobs


def test_protected_init_import_does_not_load_authority_scope(monkeypatch):
    from importlib import util
    real_spec = util.spec_from_file_location
    def spec_for(name, location):
        assert location != '/authority/code/scope_init.py'
        return real_spec(name, location)
    monkeypatch.setattr(util, 'spec_from_file_location', spec_for)
    module = load_helper('protected_init')
    monkeypatch.setattr(module.os, 'getpid', lambda: 2)
    with pytest.raises(RuntimeError, match='protected init requires an owned PID namespace'):
        module.main()


def test_protected_scope_loads_before_authority_is_hidden(monkeypatch):
    from importlib import util
    from importlib.abc import Loader
    from types import SimpleNamespace
    events = []
    real_spec = util.spec_from_file_location
    class ScopeLoader(Loader):
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            events.append('load')
            module.__dict__.update(BASE_ENV={}, main=lambda: events.append('main'))
    def spec_for(name, location):
        if location == '/authority/code/scope_init.py':
            return util.spec_from_loader(name, ScopeLoader())
        return real_spec(name, location)
    monkeypatch.setattr(util, 'spec_from_file_location', spec_for)
    module = load_helper('protected_init')
    assert events == []
    monkeypatch.setattr(module.os, 'getpid', lambda: 1)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: events.append('mount') or SimpleNamespace(returncode=0))
    module.main()
    assert events == ['load', 'mount', 'mount', 'main']


def protected_helper(monkeypatch):
    from importlib import util
    from types import SimpleNamespace
    real_spec = util.spec_from_file_location
    class ScopeLoader:
        def create_module(self, spec):
            return None
        def exec_module(self, module):
            module.BASE_ENV = {}
            module.main = lambda: {'cwd': module.workspace, 'options': module.payload_options}
    def spec_for(name, location):
        if location == '/authority/code/scope_init.py':
            return util.spec_from_loader(name, ScopeLoader())
        return real_spec(name, location)
    monkeypatch.setattr(util, 'spec_from_file_location', spec_for)
    module = load_helper('protected_init')
    monkeypatch.setattr(module.os, 'getpid', lambda: 1)
    monkeypatch.setattr(module.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=0))
    return module


def test_protected_init_enters_cas_only_after_identity_and_zero_caps(monkeypatch):
    import io
    module = protected_helper(monkeypatch)
    state = {'uid': 0, 'gid': 0, 'nnp': 0, 'caps': 0xe0}
    events = []
    monkeypatch.setattr(module.os, 'setgroups', lambda groups: events.append(('groups', groups)))
    def gid(value):
        state['gid'] = value
        events.append(('gid', value))
    def uid(value):
        state['uid'] = value
        state['caps'] = 0
        events.append(('uid', value))
    monkeypatch.setattr(module.os, 'setgid', gid)
    monkeypatch.setattr(module.os, 'setuid', uid)
    def chdir(value):
        assert state == {'uid': 127776, 'gid': 456597, 'nnp': 1, 'caps': 0}
        events.append(('cwd', value))
    monkeypatch.setattr(module.os, 'chdir', chdir)
    class Libc:
        def prctl(self, operation, value, *args):
            assert operation == 38 and value == 1
            state['nnp'] = 1
            events.append(('nnp', 1))
            return 0
    monkeypatch.setattr(module.ctypes, 'CDLL', lambda *args, **kwargs: Libc())
    def status(*args, **kwargs):
        assert args[0] == '/proc/self/status'
        events.append(('status', 0))
        return io.StringIO(''.join(k + ':\t0\n' for k in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')))
    monkeypatch.setitem(module.payload_identity.__globals__, 'open', status)
    result = module.main()
    assert result['cwd'] == '/'
    assert result['options']['preexec_fn'] is module.payload_identity
    module.payload_identity()
    assert events == [('groups', []), ('gid', 456597), ('uid', 127776), ('nnp', 1), ('status', 0), ('cwd', '/cas')]


def test_protected_init_retains_sigint_default(monkeypatch):
    import signal
    from types import SimpleNamespace
    module = protected_helper(monkeypatch)
    signals = []
    monkeypatch.setattr(signal, 'signal', lambda sig, disposition: signals.append((sig, disposition)))
    libc = SimpleNamespace(prctl=lambda *args: 0, capset=lambda *args: 0)
    monkeypatch.setattr(module.ctypes, 'CDLL', lambda *args, **kwargs: libc)
    module.protect_init()
    assert signals == [(signal.SIGINT, signal.SIG_DFL)]
