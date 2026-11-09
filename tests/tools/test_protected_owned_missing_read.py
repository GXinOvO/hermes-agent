import base64
import copy
from dataclasses import replace
import hashlib
import json

import pytest

pytest_plugins = ['tests.tools.protected_owned_fixture']


def test_target_ticket_rejects_unbound_authority_identity(protected_native):
    from tools.file_backend_probe import FileBackendUnavailable

    environment = protected_native.env
    path = '/cas/lawful.txt'
    response = environment._rpc('target', path)
    generation = environment.file_generation()
    ticket = environment._ticket(path, response, generation)
    assert ticket.content == b'lawful'
    absent_path = '/cas/ticket-absent.txt'
    absent = environment._rpc('target', absent_path)
    assert environment._ticket(absent_path, absent, generation).absent
    invalid_replies = []
    for valid_path, valid_reply in ((path, response), (absent_path, absent)):
        invalid_replies.append((valid_path, 'non-object reply', []))
        for key in ('domain', 'packet', 'storage_id'):
            invalid = copy.deepcopy(valid_reply)
            invalid.pop(key)
            invalid_replies.append((valid_path, 'missing ' + key, invalid))
        for key, bad in (('storage_id', '0' * 64), ('packet', 'AA=='),
                         ('packet', 'invalid!'), ('packet', None), ('absent', 1)):
            invalid = copy.deepcopy(valid_reply)
            invalid[key] = bad
            invalid_replies.append((valid_path, repr((key, bad)), invalid))
        invalid = copy.deepcopy(valid_reply)
        invalid['packet'] = base64.b64encode(base64.b64decode(invalid['packet']) + b'\0').decode()
        invalid_replies.append((valid_path, 'oversized packet', invalid))
        domains = [None, {}, [], ['wrong-shape'], copy.deepcopy(valid_reply['domain'])]
        domains[-1][0] = 'not-a-boot-id'
        for bad in domains:
            invalid = copy.deepcopy(valid_reply)
            invalid['domain'] = bad
            invalid['storage_id'] = hashlib.sha256(json.dumps(bad, separators=(',', ':')).encode()).hexdigest()
            invalid_replies.append((valid_path, 'invalid domain ' + repr(bad), invalid))
        for index, name in ((3, 'root mode'), (4, 'control mode')):
            for mode in (-1, True, 1.5, 'invalid', 0o200000 | valid_reply['domain'][index][2], 1 << 64):
                invalid = copy.deepcopy(valid_reply)
                invalid['domain'][index][2] = mode
                invalid['storage_id'] = hashlib.sha256(
                    json.dumps(invalid['domain'], separators=(',', ':')).encode()).hexdigest()
                invalid_replies.append((valid_path, name + ' ' + repr(mode), invalid))
    invalid = copy.deepcopy(response)
    invalid['data'] = 'invalid!'
    invalid_replies.append((path, 'invalid data base64', invalid))
    invalid = copy.deepcopy(response)
    invalid['data'] = base64.b64encode(b'broken').decode()
    invalid_replies.append((path, 'corrupted content', invalid))
    outcomes = []
    for valid_path, name, invalid in invalid_replies:
        try:
            environment._ticket(valid_path, invalid, generation)
        except FileBackendUnavailable:
            result = 'refused'
        except Exception as exc:
            result = type(exc).__name__
        else:
            result = 'accepted'
        outcomes.append({'path': valid_path, 'case': name, 'result': result})
    protected_native.record('invalid_ticket_replies', outcomes)
    assert all(item['result'] == 'refused' for item in outcomes), outcomes


def test_missing_read_allows_confirmed_create(protected_native, monkeypatch):
    n = protected_native
    path = '/cas/missing-read.json'
    observed = n.python('import errno,json,os; p=' + repr(path) + '\ntry: os.stat(p)\nexcept OSError as exc: print(json.dumps({"errno":exc.errno}))')
    assert observed == {'errno': 2}
    value = n.tool('read_file', {'path': path})
    assert value.get('not_found') is True, value
    assert value['error'] == 'File not found: ' + path, value
    assert not value.get('content')
    created = n.tool('write_file', {'path': path, 'content': '{"phase":"created"}\n'})
    assert created.get('verified') is True, created
    reads = []
    original_read = n.env.read_file
    def observed_read(operations, target_path, offset, limit, *, ticket=None):
        if target_path == path and ticket is not None:
            reads.append(ticket)
        return original_read(operations, target_path, offset, limit, ticket=ticket)
    monkeypatch.setattr(n.env, 'read_file', observed_read)
    read = n.tool('read_file', {'path': path})
    assert not read.get('error') and not read.get('not_found'), read
    assert read['content'] == '1|{"phase":"created"}'
    assert len(reads) == 1 and reads[0].content == b'{"phase":"created"}\n'
    assert reads[0].sha256
    patched = n.tool('patch', {'mode': 'replace', 'path': path,
        'old_string': 'created', 'new_string': 'verified'})
    assert patched.get('success') is True, patched
    assert json.loads(n.bytes(path)) == {'phase': 'verified'}


def test_permission_denied_read_is_not_absence(protected_native):
    n = protected_native
    path = '/cas/read-denied.txt'
    created = n.python('import json,os;from pathlib import Path;p=' + repr(path) + ';fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0);os.write(fd,b"preserve");os.close(fd);print(json.dumps({"mode":os.stat(p).st_mode&511,"size":os.stat(p).st_size}))')
    assert created == {'mode': 0, 'size': 8}, created
    direct = n.python('import json,os\ntry: os.open(' + repr(path) + ',os.O_RDONLY)\nexcept OSError as exc: print(json.dumps({"errno":exc.errno}))')
    assert direct == {'errno': 13}, direct
    value = n.tool('read_file', {'path': path})
    assert value.get('error') and not value.get('not_found'), value
    assert n.bytes('/cas/lawful.txt') == b'lawful'
    value = n.tool('read_file', {'path': '/cas/lawful.txt'})
    assert value.get('content') == '1|lawful' and not value.get('error'), value


def test_unavailable_authority_read_is_not_absence(protected_native):
    n = protected_native
    n.env._stop_launcher()
    killed = n.docker(['container', 'kill', '--signal=KILL', n.env.service.active])
    assert killed['exit'] == 0
    value = n.tool('read_file', {'path': '/cas/unavailable-read.txt'})
    assert value.get('error') and not value.get('not_found'), value


def test_permission_revoked_after_metadata_is_not_a_read(protected_native, monkeypatch):
    from tools import file_tools
    from tools.file_tools_read_tracking import _read_tracker, _read_tracker_lock

    n = protected_native
    path = '/cas/revoked-after-metadata.txt'
    n.python('import json;from pathlib import Path;Path(' + repr(path) + ').write_text("secret");print(json.dumps(True))')
    original = file_tools._file_metadata
    original_target = n.env.target
    reached = []
    targets_after_metadata = []
    def observe_target(target_path):
        if reached and target_path == path:
            targets_after_metadata.append(target_path)
        return original_target(target_path)
    def revoke_after_metadata(target):
        result = original(target)
        if str(target) == path and not reached:
            assert result is not None
            n.python('import json,os;fd=os.open("/cas/revoked-replacement.txt",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0);os.write(fd,b"secret");os.close(fd);os.replace("/cas/revoked-replacement.txt",' + repr(path) + ');print(json.dumps(True))')
            reached.append(True)
        return result
    monkeypatch.setattr(file_tools, '_file_metadata', revoke_after_metadata)
    monkeypatch.setattr(n.env, 'target', observe_target)
    value = n.tool('read_file', {'path': path})
    assert reached == [True] and targets_after_metadata == [path]
    assert value.get('error') and not value.get('not_found') and not value.get('content'), value
    with _read_tracker_lock:
        data = _read_tracker[n.task]
        assert path not in data['full_write_baselines']
        assert path not in data['protected_tickets']


def test_read_rejects_invalid_path_and_generation_tickets(protected_native):
    from tools import file_tools
    from tools.file_backend_probe import FileBackendUnavailable

    n = protected_native
    path = '/cas/lawful.txt'
    ticket = n.env.target(path)
    operations = file_tools._get_file_ops(n.task)
    assert n.env.read_file(operations, path, 1, 2000, ticket=ticket).content == '1|lawful'
    for invalid in (object(), replace(ticket, path='/cas/other.txt'),
                    replace(ticket, generation='stale-generation')):
        with pytest.raises(FileBackendUnavailable, match='ticket is invalid'):
            n.env.read_file(operations, path, 1, 2000, ticket=invalid)


def test_missing_read_in_inaccessible_parent_is_not_absence(protected_native):
    n = protected_native
    path = '/cas/locked/missing.txt'
    n.python('import json,os;os.mkdir("/cas/locked",0);print(json.dumps(True))')
    direct = n.python('import json,os\ntry: os.stat(' + repr(path) + ')\nexcept OSError as exc: print(json.dumps({"errno":exc.errno}))')
    assert direct == {'errno': 13}, direct
    value = n.tool('read_file', {'path': path})
    assert value.get('error') and not value.get('not_found') and not value.get('content'), value


def test_absent_ticket_refuses_concurrent_creation(protected_native):
    n = protected_native
    path = '/cas/created-concurrently.txt'
    ticket = n.env.target(path)
    assert ticket.absent
    n.python('import json;from pathlib import Path;Path(' + repr(path) + ').write_text("concurrent");print(json.dumps(True))')
    result = n.env.publish(ticket, 'must not overwrite')
    assert result.exit_code != 0
    assert n.bytes(path) == b'concurrent'
