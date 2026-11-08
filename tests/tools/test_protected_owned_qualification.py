"""Real config-to-consumer qualification of the opt-in protected owned domain.

These tests require task-authorized offline Docker resources. Expected boundary
negatives are EPERM for UID/capability/signal/ptrace and EACCES for masked trusted
paths; absent host sockets/proc are visibility checks, not permission evidence.
"""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading

import pytest

pytest_plugins = ['tests.tools.protected_owned_fixture']


def test_native_nested_utf8_continuous_writes_and_stdin(protected_native):
    n = protected_native
    path = '/cas/p/sub/utf8.txt'
    text = ('中文🙂 lawful text\n' * 3000)
    created = n.tool('write_file', {'path': path, 'content': text})
    assert created.get('verified') is True, created
    second = n.tool('write_file', {'path': path, 'content': text + 'tail\n'})
    assert second.get('verified') is True, second
    patched = n.tool('patch', {'mode': 'replace', 'path': path, 'old_string': 'tail', 'new_string': 'final'})
    assert patched.get('success') is True, patched
    import hashlib
    check = n.python('import hashlib,json;from pathlib import Path;data=Path(' + repr(path) + ').read_bytes();print(json.dumps({"length":len(data),"sha256":hashlib.sha256(data).hexdigest()}))')
    expected = (text + 'final\n').encode()
    assert check == {'length': len(expected), 'sha256': hashlib.sha256(expected).hexdigest()}
    first = n.terminal('cd /cas/p/sub; export LAWFUL_EXPORT=value; printf "$LAWFUL_EXPORT:"; pwd')
    assert first.get('exit_code') == 0 and 'value:/cas/p/sub' in first['output'], first
    second = n.terminal('printf "$LAWFUL_EXPORT:"; pwd')
    assert second.get('exit_code') == 0 and 'value:/cas/p/sub' in second['output'], second
    value = n.env.execute('python3 -B -c ' + shlex.quote('import sys; print(sys.stdin.read())'),
                          cwd='/cas', stdin_data='stdin中文🙂', timeout=5)
    n.record('stdin', value)
    assert value['returncode'] == 0 and 'stdin中文🙂' in value['output']
    assert value['owned_lifecycle']['waitpid_echild'] is True


@pytest.mark.parametrize('mutation', ['replace', 'pwrite'])
def test_final_publish_rpc_conflicts_with_same_authority_terminal(protected_native, mutation):
    n = protected_native
    path = '/cas/race-' + mutation + '.txt'
    initial = b'initial lawful read\n'
    external = b'external\x00\xff preserved\n'
    seed = n.python('import json;from pathlib import Path;Path(' + repr(path) + ').write_bytes(' + repr(initial) + ');print(json.dumps(True))')
    assert seed is True
    read = n.tool('read_file', {'path': path})
    assert not read.get('error'), read
    events, submitted, changed = [], [], []
    observed = {'_check_sensitive_path', '_check_binary_document_write', '_check_protected_instruction_write',
        '_check_approval_required_write', '_check_cross_profile_path', 'capture_write_binding',
        '_stale_overwrite_blocker', 'authorized_write', '_edit_warnings', '_whole_file_rewrite_hint',
        'get_write_denied_error', '_reject_unencodable', '_fail_closed_syntax_error', '_probe_write_target'}
    def trace(frame, event, arg):
        if event == 'return' and frame.f_code.co_name in observed:
            events.append(frame.f_code.co_name)
        if event == 'call' and frame.f_code.co_name == 'publish' and frame.f_locals.get('self') is n.env:
            events.append('native-publish-entry')
        if event == 'call' and frame.f_code.co_name == '_run' and frame.f_locals.get('self') is n.env:
            argv = frame.f_locals.get('argv', [])
            if argv[-2:] == ['publish', path]:
                assert not submitted
                submitted.append(json.loads(frame.f_locals['data']))
                code = ("q=p.with_suffix('.external');q.write_bytes(data);os.replace(q,p)" if mutation == 'replace' else
                        'fd=os.open(p,os.O_WRONLY);os.pwrite(fd,data,0);os.ftruncate(fd,len(data));os.close(fd)')
                value = n.python('import os,json;from pathlib import Path;p=Path(' + repr(path) + ');data=' + repr(external) + ';before=p.stat();' + code +
                    ";after=p.stat();print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'before':[before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns],'after':[after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns],'hex':p.read_bytes().hex()}))")
                changed.append(value)
                events.append('ordinary-mutation-after-host-checks-before-real-RPC')
        return trace
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        write = n.tool('write_file', {'path': path, 'content': 'candidate must conflict\n'})
    finally:
        sys.settrace(previous)
    n.record('late_mutation_trace', {'events': events, 'submitted': submitted, 'external': changed, 'write': write})
    assert len(submitted) == len(changed) == 1
    assert events.index('native-publish-entry') < events.index('ordinary-mutation-after-host-checks-before-real-RPC')
    assert observed <= set(events[:events.index('native-publish-entry')]), events
    assert write.get('error') and 'conflict' in write['error'] and not write.get('verified'), write
    assert changed[0]['uid'] == 127776 and changed[0]['gid'] == 456597
    assert n.bytes(path) == external
    actual = [r for r in n.env.receipts if r['argv'][-2:] == ['publish', path]]
    assert len(actual) == 1 and json.loads(actual[0]['output'])['state'] == 'conflict'
    if mutation == 'pwrite':
        assert changed[0]['before'][0] == changed[0]['after'][0]
        assert changed[0]['before'][2:] != changed[0]['after'][2:]


BOUNDARY = r'''import ctypes,json,os,pathlib,signal,socket
libc=ctypes.CDLL(None,use_errno=True)
status=lambda p:dict(line.split(':',1) for line in pathlib.Path(p).read_text().splitlines() if ':' in line)
def attempt(fn):
 try: return {'return':fn(),'errno':0}
 except OSError as e:return {'return':-1,'errno':e.errno}
def opening(p,flags):
 fd=os.open(p,flags);os.close(fd);return 0
def connect(p):
 s=socket.socket(socket.AF_UNIX);s.connect(p);s.close();return 0
s=status('/proc/self/status');one=status('/proc/1/status')
fds={}
for p in pathlib.Path('/proc/self/fd').iterdir():
 try:fds[p.name]=os.readlink(p)
 except FileNotFoundError:pass
out={'status':s,'pid1':one,'securebits':libc.prctl(27,0,0,0,0),
 'namespaces':{k:os.readlink('/proc/self/ns/'+k) for k in ['mnt','pid','user','net']},
 'mountinfo':pathlib.Path('/proc/self/mountinfo').read_text(),'fds':fds,
 'trusted_mask_stat':list(os.stat('/authority')),'positive':pathlib.Path('/cas/lawful.txt').read_text()}
ops={'setuid0':lambda:os.setuid(0),'setgid0':lambda:os.setgid(0),
 'TERM_pid1':lambda:os.kill(1,signal.SIGTERM),'INT_pid1':lambda:os.kill(1,signal.SIGINT),'KILL_pid1':lambda:os.kill(1,signal.SIGKILL),
 'authority_state_read':lambda:opening('/authority/state/authority.sqlite',os.O_RDONLY),
 'authority_state_write':lambda:opening('/authority/state/authority.sqlite',os.O_WRONLY),
 'helper_write':lambda:opening('/authority/code/worker.py',os.O_WRONLY),
 'controlFD':lambda:opening('/proc/1/fd/0',os.O_RDONLY),
 'authority_root':lambda:opening('/proc/1/root/authority',os.O_RDONLY),
 'hostproc':lambda:opening('/host/proc/1/status',os.O_RDONLY),
 'docker_socket':lambda:connect('/var/run/docker.sock')}
out['operations']={k:attempt(fn) for k,fn in ops.items()}
ctypes.set_errno(0);ret=libc.ptrace(16,1,None,None)
out['operations']['ptrace_attach_pid1']={'return':ret,'errno':ctypes.get_errno()}
class H(ctypes.Structure):_fields_=[('version',ctypes.c_uint32),('pid',ctypes.c_int)]
class D(ctypes.Structure):_fields_=[('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
h=H(0x20080522,0);d=(D*2)();d[0].effective=d[0].permitted=1
ctypes.set_errno(0);ret=libc.capset(ctypes.byref(h),ctypes.byref(d))
out['operations']['capset']={'return':ret,'errno':ctypes.get_errno()}
pathlib.Path('/cas/paired.txt').write_text('paired lawful')
out['paired']=pathlib.Path('/cas/paired.txt').read_text()
print(json.dumps(out))
'''


def test_payload_caps_fd_errno_and_live_trusted_pid1(protected_native):
    n = protected_native
    trusted = n.docker(['container', 'exec', '--user=0:0', n.env.service.active, '/bin/sh', '-ceu',
        'test -f /authority/code/worker.py; test -f /authority/state/authority.sqlite; printf TRUSTED_PATHS_EXIST'])
    assert trusted['stdout'] == 'TRUSTED_PATHS_EXIST'
    value = n.python(BOUNDARY)
    for key, expected in [('Uid', [127776] * 4), ('Gid', [456597] * 4)]:
        assert [int(x) for x in value['status'][key].split()] == expected
    assert value['status']['Groups'].split() == []
    for key in ('CapEff', 'CapPrm', 'CapInh', 'CapBnd', 'CapAmb'):
        assert int(value['status'][key], 16) == 0
    assert int(value['status']['NoNewPrivs']) == 1 and value['securebits'] == 0
    assert int(value['pid1']['CapEff'], 16) == int(value['pid1']['CapPrm'], 16) == 0xe0
    for key in ('CapInh', 'CapBnd', 'CapAmb'):
        assert int(value['pid1'][key], 16) == 0
    assert int(value['pid1']['NoNewPrivs']) == 1
    assert int(value['pid1']['SigIgn'], 16) & 2 == 0
    assert value['positive'] == 'lawful' and value['paired'] == 'paired lawful'
    for key in ('setuid0', 'setgid0', 'TERM_pid1', 'INT_pid1', 'KILL_pid1', 'ptrace_attach_pid1', 'capset'):
        assert value['operations'][key] == {'return': -1, 'errno': 1}, (key, value['operations'][key])
    for key in ('authority_state_read', 'authority_state_write', 'helper_write', 'controlFD', 'authority_root'):
        assert value['operations'][key] == {'return': -1, 'errno': 13}, (key, value['operations'][key])
    for key in ('hostproc', 'docker_socket'):
        assert value['operations'][key] == {'return': -1, 'errno': 2}
    host = {k: os.readlink('/proc/self/ns/' + k) for k in ('mnt', 'pid', 'net')}
    n.record('host_namespace_positive', host)
    assert all(value['namespaces'][k] != host[k] for k in host)
    assert set(value['fds']) <= {'0', '1', '2'}
    assert ' /authority ' in value['mountinfo'] and ' - tmpfs ' in value['mountinfo']


def test_native_process_manage_large_utf8_nonzero_and_durable_reader(protected_native):
    from hermes_constants import get_hermes_home
    n = protected_native
    command = "python3 -B -c " + shlex.quote("import sys;from pathlib import Path;p=Path('/cas/executions');p.write_text(p.read_text()+'one' if p.exists() else 'one');print('中🙂'*90000);print('STDERR_FINAL',file=sys.stderr,flush=True);print('STDOUT_FINAL',flush=True);sys.exit(23)")
    started = n.terminal(command, background=True)
    sid = started['session_id']
    assert n.process('poll', sid)['status'] in ('running', 'exited')
    final = n.process('wait', sid, timeout=15)
    session = n.registry.get(sid)
    assert final['exit_code'] == 23 and final['result_durability'] == 'persisted', final
    assert session.pid is None and session.owned_handle is not None
    assert session._completion_event.is_set()
    assert 'STDERR_FINAL' in session.output_buffer and 'STDOUT_FINAL' in session.output_buffer
    assert '\ufffd' not in session.output_buffer and session.total_output_chars >= 180000
    assert n.process('log', sid, limit=200)['exit_code'] == 23
    receipt = session.owned_lifecycle
    assert receipt['waitpid_echild'] and receipt['descendant_empty'] and receipt['launcher_reaped']
    path = get_hermes_home() / 'logs/process-results' / (sid + '.json')
    original = json.loads(path.read_text())
    script = '''import json,sys
from gateway.session_context import scoped_current_session_id
from tools import process_registry as pr
from tools.registry import registry as tools
pr.process_registry=pr.ProcessRegistry()
with scoped_current_session_id('protected-r19-owner'):
 poll=json.loads(tools.get_entry('process_manage').handler({'action':'poll','session_id':sys.argv[1]}))
 log=json.loads(tools.get_entry('process_manage').handler({'action':'log','session_id':sys.argv[1]}))
 final=json.loads(tools.get_entry('process_manage').handler({'action':'wait','session_id':sys.argv[1],'timeout':1}))
with scoped_current_session_id('wrong-owner'):
 wrong=pr.process_registry.poll(sys.argv[1])
print(json.dumps({'poll':poll,'log':log,'final':final,'wrong':wrong,'running':len(pr.process_registry._running)}))
'''
    argv = [sys.executable, '-B', '-c', script, sid]
    p = subprocess.run(argv, capture_output=True, text=True, env=os.environ.copy(), timeout=20, check=False)
    n.record('independent_native_reader', {'argv': argv, 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr,
        'receipt_before': original, 'receipt_after': json.loads(path.read_text())})
    assert p.returncode == 0, p.stderr
    recovered = json.loads(p.stdout)
    assert recovered['running'] == 0 and recovered['wrong']['status'] == 'not_found'
    assert recovered['poll']['result_verification'] == 'verified'
    assert recovered['final']['result_fingerprint'] == original['result_fingerprint']
    assert recovered['final']['owned_generation'] == original['owned_generation'] == session.owned_handle.identifier
    assert recovered['final']['owned_lifecycle'] == receipt
    assert recovered['final']['exit_code'] == 23 and 'STDERR_FINAL' in recovered['final']['output']
    assert n.bytes('/cas/executions') == b'one'


DESCENDANTS = '''import os,pathlib,signal,time
signal.signal(signal.SIGTERM,signal.SIG_IGN)
pid=os.fork()
if pid==0:
 os.setsid()
 pid=os.fork()
 if pid:os._exit(0)
 pathlib.Path('/cas/descendant-ready').write_text(str(os.getpid()))
 while True:time.sleep(.1)
while True:time.sleep(.1)
'''


@pytest.mark.parametrize('ending', ['kill', 'timeout', 'rootexit'])
def test_protected_fork_setsid_exact_scope_reaping(protected_native, ending):
    from tests.waiting import wait_until
    n = protected_native
    if ending == 'rootexit':
        code = "import os,time;pid=os.fork();\nif pid==0:\n os.setsid();print('CHILD_READY',flush=True);time.sleep(60)\nelse:\n time.sleep(.2);os._exit(7)"
        result = n.terminal('python3 -B -c ' + shlex.quote(code), timeout=5)
        assert result['exit_code'] == 7, result
        receipts = [h.lifecycle for h in n.env.all_handles if h.lifecycle and h.lifecycle['root_exit'] == 7]
    elif ending == 'timeout':
        result = n.terminal('python3 -B -c ' + shlex.quote(DESCENDANTS), timeout=1)
        assert result['exit_code'] == 124, result
        receipts = [h.lifecycle for h in n.env.all_handles if h.lifecycle and h.lifecycle.get('timed_out')]
        if not receipts:
            receipts = [h.lifecycle for h in n.env.all_handles if h.lifecycle and h.lifecycle.get('cancelled')]
    else:
        started = n.terminal('python3 -B -c ' + shlex.quote(DESCENDANTS), background=True)
        sid = started['session_id']
        assert wait_until(lambda: n.python("import pathlib,json;print(json.dumps(pathlib.Path('/cas/descendant-ready').exists()))"), timeout=10)
        result = n.process('kill', sid)
        assert result['status'] == 'killed', result
        receipts = [result['owned_lifecycle']]
        assert n.process('kill', sid)['status'] == 'already_exited'
    if not receipts:
        receipts = [v['value']['owned_lifecycle'] for v in n.events if v['kind'] == 'wire_finish' and v['value'].get('owned_lifecycle', {}).get('reaped_count', 0) >= 2]
    n.record('exact_descendant_cleanup', {'ending': ending, 'receipts': receipts})
    assert receipts
    assert any(r['reaped_count'] >= 2 for r in receipts)
    assert all(r['waitpid_echild'] and r['descendant_empty'] and r['launcher_reaped'] for r in receipts)


def test_protected_output_pending_then_durable_drain(protected_native, monkeypatch):
    n = protected_native
    entered, release = threading.Event(), threading.Event()
    original = n.registry._ingest_output
    def delayed(session, text, **kwargs):
        if 'READY_PENDING' in text and not entered.is_set():
            entered.set()
            assert release.wait(20), 'missing pending-output release'
        return original(session, text, **kwargs)
    monkeypatch.setattr(n.registry, '_ingest_output', delayed)
    try:
        sid = n.terminal('printf READY_PENDING; sleep 60', background=True)['session_id']
        session = n.registry.get(sid)
        session.notify_on_complete = True
        assert entered.wait(5)
        killed = n.process('kill', sid)
        assert killed['output_pending'] is True and killed['process_running'] is False
        assert not session.exited and not session._completion_event.is_set()
        for result in (n.process('poll', sid), n.process('log', sid), n.process('wait', sid, timeout=1)):
            assert result['output_pending'] is True and result['process_running'] is False, result
        listed = n.process('list')
        assert any(s.get('output_pending') and s['process_running'] is False for s in listed['processes'])
        assert not n.registry.is_completion_consumed(sid)
        release.set()
        assert session._completion_event.wait(5)
        final = n.process('wait', sid, timeout=1)
        assert final['result_durability'] == 'persisted' and 'READY_PENDING' in final['output']
        notices = [v for v in n.registry.completion_queue.queue if v['session_id'] == sid]
        assert len(notices) == 1
        assert n.process('kill', sid)['status'] == 'already_exited'
    finally:
        release.set()


def test_volume_close_idle_crash_incarnation_and_owner_refusal(protected_native):
    from tools.terminal_tool_backends import _create_environment
    from tools import terminal_tool as terminal
    from tools.file_backend_probe import FileBackendUnavailable
    n = protected_native
    value = n.tool('write_file', {'path': '/cas/persistent.txt', 'content': 'durable-before-crash\n'})
    assert value.get('verified') is True, value
    original = n.env.snapshot('/cas/persistent.txt')
    n.env.cleanup()
    reopened = _create_environment('protected_owned', image='', cwd='/cas', timeout=10)
    try:
        result = reopened.execute('cat /cas/persistent.txt', timeout=5)
        n.record('normal_volume_reopen', {'result': result, 'generation': reopened._session_id})
        assert result['returncode'] == 0 and result['output'] == 'durable-before-crash\n'
        ticket = reopened.snapshot('/cas/persistent.txt')
        assert ticket.incarnation != original.incarnation
        reopened._stop_launcher()
        cid = reopened.service.active
        inspected = reopened.service.inspect(cid)
        assert inspected['State']['Running']
        n.docker(['container', 'kill', '--signal=KILL', cid])
        crashed = json.loads(n.docker(['container', 'inspect', cid])['stdout'])[0]
        assert crashed['State']['Running'] is False and crashed['State']['ExitCode'] == 137
        reopened.cleanup()
    finally:
        reopened.cleanup()
    recovered = _create_environment('protected_owned', image='', cwd='/cas', timeout=10)
    try:
        fresh = recovered.snapshot('/cas/persistent.txt')
        n.record('idle_crash_recovery', {'old_incarnation': ticket.incarnation, 'fresh_incarnation': fresh.incarnation,
            'content_hex': fresh.content.hex(), 'old_generation': ticket.generation, 'fresh_generation': fresh.generation,
            'window': 'idle-after-committed-write; no transaction-preCOMMIT or response-lost claim'})
        assert fresh.content == b'durable-before-crash\n' and fresh.incarnation != ticket.incarnation
        with pytest.raises(FileBackendUnavailable, match='generation|changed'):
            recovered.publish(ticket, 'stale must refuse')
        fresh = recovered.snapshot('/cas/persistent.txt')
        with pytest.raises(FileBackendUnavailable, match='unknown|unavailable'):
            recovered._rpc('publish', ticket.path, {'path': ticket.path,
                'packet': base64.b64encode(ticket.packet).decode(), 'domain': json.loads(fresh.domain_json),
                'candidate': base64.b64encode(b'old incarnation must refuse').decode(), 'operation': 'stale-incarnation-negative'})
        raw = recovered.receipts[-1]
        n.record('stale_incarnation_authority_refusal', raw)
        assert raw['returncode'] != 0 and '[Errno 22]' in raw['stderr']
        current = recovered.snapshot('/cas/persistent.txt')
        updated = recovered.publish(current, 'fresh recovered lawful\n')
        assert updated.exit_code == 0
        assert recovered.execute('cat /cas/persistent.txt', timeout=5)['output'] == 'fresh recovered lawful\n'
    finally:
        recovered.cleanup()
    from agent.terminal_env_registry import get_provider
    provider = get_provider('protected_owned')
    plugin = sys.modules[provider.__class__.__module__]
    foreign = n.root / 'foreign-state'
    foreign.mkdir(mode=0o700)
    other = {**n.config, 'state_root': str(foreign)}
    with pytest.raises(RuntimeError, match='belongs to another owner'):
        plugin.ProtectedOwnedEnvironment(other)
    final_env = _create_environment('protected_owned', image='', cwd='/cas', timeout=10)
    try:
        assert final_env.execute('cat /cas/persistent.txt', timeout=5)['output'] == 'fresh recovered lawful\n'
        n.record('foreign_owner_data_preserved', {'generation': final_env._session_id})
    finally:
        final_env.cleanup()
    assert not terminal._active_environments or all(e.closed for e in terminal._active_environments.values())


def test_protected_constructor_post_acquisition_rollback_and_retry(protected_native, monkeypatch):
    from agent.terminal_env_registry import get_provider
    from tools.terminal_tool_backends import _create_environment
    n = protected_native
    provider = get_provider('protected_owned')
    plugin = sys.modules[provider.__class__.__module__]
    with pytest.raises(BlockingIOError) as locked:
        _create_environment('protected_owned', image='', cwd='/cas', timeout=10)
    assert locked.value.errno == 11
    assert not n.env.service.lock.closed and n.env.service.inspect(n.env.service.active)['State']['Running']
    n.record('state_root_lock_refusal', {'errno': locked.value.errno, 'original_authority_running': True})
    n.env.cleanup()
    acquired = []
    def fail_after_service(self, argv, cwd, timeout):
        assert self.service.inspect(self.service.active)['State']['Running']
        acquired.append(self)
        raise RuntimeError('controlled post-acquisition transport fault')
    with monkeypatch.context() as fault:
        fault.setattr(plugin.ProtectedOwnedEnvironment, '_initialize_transport', fail_after_service)
        with pytest.raises(RuntimeError, match='controlled post-acquisition'):
            _create_environment('protected_owned', image='', cwd='/cas', timeout=10)
    assert len(acquired) == 1
    failed = acquired[0]
    assert failed.closed and failed.service.closed and failed.service.lock.closed
    for cid in failed.service.ids:
        absent = n.docker(['container', 'inspect', cid], absent=True)
        assert absent['exit'] != 0
    n.record('post_acquisition_before_rescue', {'closed': failed.closed, 'service_closed': failed.service.closed,
        'lock_closed': failed.service.lock.closed, 'cleanup': json.loads((failed.state / 'cleanup.json').read_text())})
    failed.cleanup()
    retry = _create_environment('protected_owned', image='', cwd='/cas', timeout=10)
    try:
        result = retry.execute('printf RETRY; cat /cas/lawful.txt', timeout=5)
        n.record('constructor_retry', result)
        assert result['returncode'] == 0 and result['output'] == 'RETRYlawful'
    finally:
        retry.cleanup()
        retry.cleanup()


def test_invalid_configuration_and_authority_down_never_fall_back(protected_native):
    from agent.terminal_env_registry import get_provider
    from tools.file_backend_probe import FileBackendUnavailable
    n = protected_native
    provider = get_provider('protected_owned')
    plugin = sys.modules[provider.__class__.__module__]
    for change in ({'network': 'hostnet'}, {'image': 'latest'}, {'volume': 'invalid/name'}):
        with pytest.raises(ValueError):
            plugin.ProtectedOwnedEnvironment({**n.config, **change})
    live = n.docker(['container', 'inspect', n.env.service.active])
    assert json.loads(live['stdout'])[0]['State']['Running']
    n.env._stop_launcher()
    n.docker(['container', 'kill', '--signal=KILL', n.env.service.active])
    with pytest.raises(FileBackendUnavailable, match='unavailable|fallback'):
        n.env.snapshot('/cas/lawful.txt')
    with pytest.raises(RuntimeError, match='closed|fallback|lifecycle'):
        n.env.execute('printf MUST_NOT_RUN', timeout=2)
    n.record('authority_down_fail_closed', {'host_fallback': False, 'active': n.env.service.active})


RAW_CONTROL = r'''import ctypes as C,fcntl,json,os
class A(C.Structure):_fields_=[('inode',C.c_uint64),('name_revision',C.c_uint64),('object_revision',C.c_uint64)]
class E(C.Structure):
 _fields_=[('incarnation',C.c_uint64),('root_id',C.c_uint64),('parent_id',C.c_uint64),('root_revision',C.c_uint64),('name',C.c_char*1024),('snapshot_id',C.c_char*40)]+[(k,C.c_uint64) for k in ['inode','name_revision','object_revision','mode','uid','gid','nlink','size','mtime_ns','ctime_ns','depth']]+[('ancestors',A*24)]
class R(C.Structure):
 _fields_=[('abi',C.c_uint64),('expected',E),('new_size',C.c_uint64),('offset',C.c_uint64),('length',C.c_uint64),('chunk',C.c_ubyte*4096),('operation',C.c_char*64),('upload_id',C.c_char*40),('status',C.c_uint64)]
fd=os.open('/cas/.control',os.O_RDONLY)
def snap(name):
 r=R(abi=2);r.expected.name=name.encode();buf=bytearray(bytes(r))
 try:
  ret=fcntl.ioctl(fd,(3<<30)|(C.sizeof(R)<<16)|(ord('Q')<<8)|1,buf,True)
  return {'return':ret,'errno':0,'status':R.from_buffer_copy(buf).status,'bytes':C.sizeof(R)}
 except OSError as e:return {'return':-1,'errno':e.errno}
print(json.dumps({'lawful':snap('lawful.txt'),'denied':snap('unreadable.txt')}))
'''


def test_raw_control_permission_and_task_fullread_are_not_bypassed(protected_native):
    from tools.registry import registry
    from tools import terminal_tool as terminal
    from gateway.session_context import scoped_current_session_id
    n = protected_native
    positive = n.python("import os,json;fd=os.open('/cas/unreadable.txt',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0);os.write(fd,b'unread secret');os.close(fd);print(json.dumps(True))")
    assert positive is True
    raw = n.python(RAW_CONTROL)
    assert raw['lawful']['errno'] == 0 and raw['lawful']['status'] == 1
    assert raw['denied'] == {'return': -1, 'errno': 13}
    terminal.register_container_alias(n.task + '-other', n.task)
    try:
        with scoped_current_session_id(n.owner):
            other = json.loads(registry.get_entry('write_file').handler({'path': '/cas/lawful.txt', 'content': 'no other read'}, task_id=n.task + '-other'))
        n.record('other_task_without_fullread', other)
        assert other.get('error') and not other.get('verified'), other
        assert n.bytes('/cas/lawful.txt') == b'lawful'
        read = n.tool('read_file', {'path': '/cas/lawful.txt'})
        assert not read.get('error'), read
        own = n.tool('write_file', {'path': '/cas/lawful.txt', 'content': 'lawful own update'})
        assert own.get('verified'), own
    finally:
        with terminal._container_alias_lock:
            terminal._container_aliases.pop(n.task + '-other')


def test_real_publish_response_loss_is_unknown_without_retry(protected_native, monkeypatch):
    n = protected_native
    path = '/cas/response-lost.txt'
    seed = n.tool('write_file', {'path': path, 'content': 'before response loss\n'})
    assert seed.get('verified'), seed
    actual_run = subprocess.run
    injected = []
    def lose_response(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[-2:] == ['publish', path]:
            tail = argv.index('/usr/bin/python3.10')
            command = shlex.join(argv[tail:]) + ' > /dev/null; rc=$?; [ "$rc" -eq 0 ] || exit "$rc"; exit 71'
            argv = argv[:tail] + ['/bin/sh', '-c', command]
            injected.append({'actual_argv': argv})
        return actual_run(argv, *args, **kwargs)
    with monkeypatch.context() as fault:
        fault.setattr(subprocess, 'run', lose_response)
        value = n.tool('write_file', {'path': path, 'content': 'committed response lost\n'})
    n.record('response_lost_after_real_commit', {'fault': injected, 'result': value})
    assert len(injected) == 1
    assert value.get('error') and 'unknown' in value['error'] and not value.get('verified'), value
    assert n.bytes(path) == b'committed response lost\n'
    unread = n.tool('write_file', {'path': path, 'content': 'must reread first'})
    assert unread.get('error') and not unread.get('verified'), unread
    assert not n.tool('read_file', {'path': path}).get('error')
    assert n.tool('write_file', {'path': path, 'content': 'lawful after fresh read\n'}).get('verified')
    assert n.bytes(path) == b'lawful after fresh read\n'


def test_transaction_precommit_crash_rolls_back_unpublished_bytes(protected_native):
    import select
    from tools.terminal_tool_backends import _create_environment
    from tools.file_backend_probe import FileBackendUnavailable
    from tests.waiting import wait_until
    n = protected_native
    path = '/cas/precommit.txt'
    assert n.tool('write_file', {'path': path, 'content': 'committed baseline\n'}).get('verified')
    ticket = n.env.snapshot(path)
    n.env._stop_launcher()
    cid = n.env.service.active
    script = '''import json,pathlib,sqlite3,sys,time
connection=sqlite3.connect('/authority/state/authority.sqlite')
connection.execute('BEGIN')
connection.execute('SELECT count(*) FROM objects').fetchone()
print('READER_SHARED_LOCK_READY',flush=True)
journal=pathlib.Path('/authority/state/authority.sqlite-journal')
end=time.monotonic()+4
while time.monotonic()<end:
 if journal.exists() and journal.stat().st_size>0:
  print('PRECOMMIT_JOURNAL '+str(journal.stat().st_size),flush=True)
  break
 time.sleep(.01)
sys.stdin.read()
'''
    argv = ['/usr/bin/sudo', '-n', '/usr/bin/docker', 'container', 'exec', '-i', '--user=0:0',
            '--env=LD_LIBRARY_PATH=/opt/runtime/lib', '--env=PYTHONDONTWRITEBYTECODE=1', cid,
            '/usr/bin/python3', '-B', '-c', script]
    helper_script = '''import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('qualification_cas','/authority/code/file_protected_fuse.py')
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
original=module._send
def barrier(fd,request,number):
 if number==2:
  print('UPLOAD_STAGED_FINAL_RPC_READY',flush=True)
  if sys.stdin.readline().strip()!='PUBLISH':raise RuntimeError('missing explicit final-RPC release')
 return original(fd,request,number)
module._send=barrier
print(json.dumps(module._publish(json.loads(sys.argv[1]))),flush=True)
'''
    payload = {'path': path, 'packet': base64.b64encode(ticket.packet).decode(),
        'domain': json.loads(ticket.domain_json), 'candidate': base64.b64encode(b'uncommitted must roll back\n').decode(),
        'operation': 'precommit-crash-negative'}
    helper_argv = ['/usr/bin/sudo', '-n', '/usr/bin/docker', 'container', 'exec', '-i', '--user=127776:456597',
        '--workdir=/cas', '--env=LD_LIBRARY_PATH=/opt/runtime/lib', '--env=PYTHONDONTWRITEBYTECODE=1', cid,
        '/usr/bin/python3.10', '-I', '-S', '-B', '-c', helper_script, json.dumps(payload)]
    helper = subprocess.Popen(helper_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    reader = None
    try:
        assert select.select([helper.stdout], [], [], 5)[0]
        staged = helper.stdout.readline().strip()
        assert staged == 'UPLOAD_STAGED_FINAL_RPC_READY', staged
        reader = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert select.select([reader.stdout], [], [], 5)[0]
        ready = reader.stdout.readline().strip()
        assert ready == 'READER_SHARED_LOCK_READY', ready
        helper.stdin.write('PUBLISH\n')
        helper.stdin.flush()
        assert select.select([reader.stdout], [], [], 5)[0]
        marker = reader.stdout.readline().strip()
        assert marker.startswith('PRECOMMIT_JOURNAL '), marker
        assert helper.poll() is None, 'publication must still be waiting before COMMIT'
        n.record('precommit_barrier', {'argv': argv, 'helper_argv': helper_argv, 'reader_ready': ready, 'journal_marker': marker,
            'publish_pending': helper.poll() is None, 'operation': 'precommit-crash-negative', 'upload_staged': staged})
        n.docker(['container', 'kill', '--signal=KILL', cid])
        assert helper.wait(10) != 0
        assert reader.wait(10) != 0
        n.record('precommit_transport', {'helper_exit': helper.returncode, 'helper_stderr': helper.stderr.read(),
            'reader_exit': reader.returncode, 'reader_stderr': reader.stderr.read()})
    finally:
        for proc in (helper, reader):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.stdin.close()
                proc.wait(10)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if not stream.closed:
                    stream.close()
        n.env.cleanup()
    reopened = _create_environment('protected_owned', image='', cwd='/cas', timeout=10)
    try:
        fresh = reopened.snapshot(path)
        assert fresh.content == b'committed baseline\n' and fresh.incarnation != ticket.incarnation
        assert reopened.publish(fresh, 'lawful after rollback\n').exit_code == 0
        assert reopened.execute('cat ' + path, timeout=5)['output'] == 'lawful after rollback\n'
        n.record('precommit_recovered', {'baseline_hex': fresh.content.hex(), 'fresh_incarnation': fresh.incarnation,
            'old_incarnation': ticket.incarnation})
    finally:
        reopened.cleanup()
