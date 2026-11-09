import base64
import ctypes as C
from dataclasses import dataclass
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import logging
import os
import posixpath
import re
import stat
import subprocess
import sys
import uuid

CHUNK = 4096
PATH_BYTES = 1024
DEPTH = 24


class Ancestor(C.Structure):
    _fields_ = [('inode', C.c_uint64), ('name_revision', C.c_uint64), ('object_revision', C.c_uint64)]


class Expected(C.Structure):
    _fields_ = [('incarnation', C.c_uint64), ('root_id', C.c_uint64),
                ('parent_id', C.c_uint64), ('root_revision', C.c_uint64), ('name', C.c_char * PATH_BYTES),
                ('snapshot_id', C.c_char * 40)] + [
                (k, C.c_uint64) for k in ('inode', 'name_revision', 'object_revision',
                'mode', 'uid', 'gid', 'nlink', 'size', 'mtime_ns', 'ctime_ns', 'depth')] + [
                ('ancestors', Ancestor * DEPTH)]


class Request(C.Structure):
    _fields_ = [('abi', C.c_uint64), ('expected', Expected), ('new_size', C.c_uint64),
                ('offset', C.c_uint64), ('length', C.c_uint64), ('chunk', C.c_ubyte * CHUNK),
                ('operation', C.c_char * 64), ('upload_id', C.c_char * 40), ('status', C.c_uint64)]


def _send(fd, request, number):
    packet = bytearray(bytes(request))
    command = (3 << 30) | (C.sizeof(Request) << 16) | (ord('Q') << 8) | number
    result = fcntl.ioctl(fd, command, packet, True)
    if result != 0:
        raise RuntimeError('protected FUSE ioctl returned nonzero')
    return Request.from_buffer_copy(packet)


def _path(path):
    if not isinstance(path, str) or not path.startswith('/cas/'):
        raise ValueError('unsupported protected FUSE path')
    relative = path[len('/cas/'):]
    parts = relative.split('/')
    if (path != posixpath.normpath(path) or '\0' in path or len(relative.encode()) >= PATH_BYTES
            or len(parts) > DEPTH + 1 or any(p in ('', '.', '..', '.control') or len(p.encode()) > 255 for p in parts)):
        raise ValueError('unsupported protected FUSE path; invalid component or path escape')
    return path


def _metadata(s):
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def _domain(fd):
    with open('/proc/self/mountinfo', encoding='utf-8') as stream:
        mounts = [line.split() for line in stream]
    mounts = [m for m in mounts if m[4] == '/cas']
    m = mounts[-1]
    if not m[m.index('-') + 1].startswith('fuse'):
        raise RuntimeError('protected FUSE mount unavailable')
    root, control = os.stat('/cas'), os.fstat(fd)
    with open('/proc/sys/kernel/random/boot_id', encoding='utf-8') as stream:
        boot = stream.read().strip()
    identity = [boot, m[:6], m[m.index('-') + 1:],
                [root.st_dev, root.st_ino, root.st_mode, root.st_uid, root.st_gid],
                [control.st_dev, control.st_ino, control.st_mode, control.st_uid, control.st_gid]]
    return identity


def _version_metadata(s, expected):
    version = Expected.from_buffer_copy(bytes(expected))
    C.memset(C.addressof(version) + Expected.snapshot_id.offset, 0, 40)
    return _metadata(s) + [int.from_bytes(hashlib.sha256(bytes(version)).digest(), 'big')]


def _snapshot_bytes(fd, reply):
    expected = bytes(reply.expected)
    parts = []
    for offset in range(0, reply.expected.size, CHUNK):
        request = Request(abi=2, expected=reply.expected, offset=offset,
                          length=min(CHUNK, reply.expected.size - offset))
        chunk = _send(fd, request, 3)
        if (chunk.status != 1 or bytes(chunk.expected) != expected
                or chunk.offset != offset or chunk.length != request.length):
            raise RuntimeError('protected FUSE snapshot chunk identity invalid')
        parts.append(bytes(chunk.chunk[:chunk.length]))
    data = b''.join(parts)
    if len(data) != reply.expected.size:
        raise RuntimeError('protected FUSE snapshot incomplete')
    return data


def _snapshot(path):
    _path(path)
    # Real target open enforces payload path permissions before control disclosure.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb', buffering=0) as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('unsupported protected FUSE file type')
        ordinary = stream.read()
        end = os.fstat(stream.fileno())
    with open('/cas/.control', 'rb', buffering=0) as control:
        identity = _domain(control.fileno())
        request = Request(abi=2)
        request.expected.name = path[len('/cas/'):].encode()
        reply = _send(control.fileno(), request, 1)
        if reply.status != 1:
            raise RuntimeError('protected FUSE snapshot unavailable')
        data = _snapshot_bytes(control.fileno(), reply)
        after = os.stat(path)
        if identity != _domain(control.fileno()):
            raise RuntimeError('protected FUSE root/control changed during read')
    e = reply.expected
    fields = (e.inode, e.size, e.mtime_ns, e.ctime_ns, e.mode, e.uid, e.gid, e.nlink)
    attrs = lambda s: (s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_mode, s.st_uid, s.st_gid, s.st_nlink)
    if not (attrs(before) == attrs(end) == attrs(after) == fields and ordinary == data):
        raise RuntimeError('protected FUSE ordinary read differs from service snapshot')
    return {'packet': base64.b64encode(bytes(reply)).decode(), 'domain': identity,
            'metadata': _version_metadata(before, e), 'data': base64.b64encode(data).decode(),
            'sha256': hashlib.sha256(data).hexdigest(),
            'storage_id': hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest(),
            'storage_path': path, 'incarnation': e.incarnation}


def _target(path):
    _path(path)
    with open('/cas/.control', 'rb', buffering=0) as control:
        identity = _domain(control.fileno())
        request = Request(abi=2)
        request.expected.name = path[len('/cas/'):].encode()
        reply = _send(control.fileno(), request, 6)
        if reply.status != 1:
            raise RuntimeError('protected FUSE target unavailable')
        if reply.expected.inode:
            return _snapshot(path)
        return {'packet': base64.b64encode(bytes(reply)).decode(), 'domain': identity,
                'storage_id': hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest(),
                'absent': True}


def _rebase_parents(value):
    request = Request.from_buffer_copy(base64.b64decode(value['packet'], validate=True))
    proof = Request.from_buffer_copy(base64.b64decode(value['proof'], validate=True))
    request.length = C.sizeof(Expected)
    C.memmove(C.addressof(request) + Request.chunk.offset, bytes(proof.expected), C.sizeof(Expected))
    with open('/cas/.control', 'rb', buffering=0) as control:
        if _domain(control.fileno()) != value['domain']:
            raise RuntimeError('protected FUSE rebase domain changed')
        reply = _send(control.fileno(), request, 10)
        return {'status': int(reply.status), 'packet': base64.b64encode(bytes(reply)).decode()}


def _namespace(value):
    request = Request.from_buffer_copy(base64.b64decode(value['packet'], validate=True))
    destination = value.get('destination')
    request.operation = value['operation'].encode()
    request.status = 0
    mask = os.umask(0)
    os.umask(mask)
    request.offset = 0o777 & ~mask
    if destination:
        target = Request.from_buffer_copy(base64.b64decode(destination, validate=True))
        request.length = C.sizeof(Expected)
        C.memmove(C.addressof(request) + Request.chunk.offset, bytes(target.expected), request.length)
    else:
        request.length = 0
    with open('/cas/.control', 'rb', buffering=0) as control:
        identity = _domain(control.fileno())
        if identity != value['domain']:
            raise RuntimeError('protected FUSE namespace domain changed; refusing mutation')
        reply = _send(control.fileno(), request, 9 if destination else 8)
        packets = []
        if reply.status == 1:
            count = 2 if destination else 1
            if reply.length != count * C.sizeof(Expected):
                raise RuntimeError('protected FUSE namespace identity missing; outcome unknown')
            for index in range(count):
                result = Request(abi=2, status=1)
                C.memmove(C.addressof(result.expected), bytes(reply.chunk[index*C.sizeof(Expected):(index+1)*C.sizeof(Expected)]), C.sizeof(Expected))
                packets.append(base64.b64encode(bytes(result)).decode())
        return {'status': int(reply.status), 'domain': identity, 'packets': packets}


def _publish(value):
    path = _path(value['path'])
    raw = base64.b64decode(value['packet'], validate=True)
    if len(raw) != C.sizeof(Request):
        raise ValueError('unsupported protected FUSE commit packet length')
    request = Request.from_buffer_copy(raw)
    candidate = base64.b64decode(value['candidate'], validate=True)
    if request.abi != 2 or request.expected.name.decode() != path[len('/cas/'):]:
        raise ValueError('unsupported protected FUSE commit packet')
    operation = value['operation'].encode()
    if not operation or len(operation) >= 64 or b'\0' in operation:
        raise ValueError('unsupported protected FUSE operation')
    with open('/cas/.control', 'rb', buffering=0) as control:
        if _domain(control.fileno()) != value['domain']:
            raise RuntimeError('protected FUSE root/control identity changed; refusing write')
        request.status = 0
        request.new_size = len(candidate)
        request.operation = operation
        request = _send(control.fileno(), request, 4)
        if request.status != 1 or not request.upload_id:
            raise RuntimeError('protected FUSE upload unavailable')
        for offset in range(0, len(candidate), CHUNK):
            part = candidate[offset:offset + CHUNK]
            request.offset, request.length = offset, len(part)
            request.chunk[:] = b'\0' * CHUNK
            request.chunk[:len(part)] = part
            request = _send(control.fileno(), request, 5)
            if request.status != 1:
                raise RuntimeError('protected FUSE upload incomplete')
        if not request.expected.inode:
            mask = os.umask(0)
            os.umask(mask)
            request.offset, request.length = 0o777 & ~mask, 0o666 & ~mask
        reply = _send(control.fileno(), request, 2 if request.expected.inode else 7)
        published = None
        if reply.status == 1:
            if reply.length != C.sizeof(Expected):
                raise RuntimeError('protected FUSE published identity missing; write outcome unknown')
            expected = Expected.from_buffer_copy(bytes(reply.chunk[:reply.length]))
            published = Request(abi=2, expected=expected, status=1)
            if _snapshot_bytes(control.fileno(), published) != candidate:
                raise RuntimeError('protected FUSE published snapshot differs from candidate; write outcome unknown')
    return {'state': {1: 'committed', 2: 'conflict', 3: 'already_used'}.get(reply.status, 'unknown'),
            'protocol_status': reply.status, 'operation': value['operation'],
            'published_packet': base64.b64encode(bytes(published)).decode() if published is not None else None}


@dataclass(frozen=True)
class ReadTicket:
    path: str
    generation: str
    task_id: str | None
    packet: bytes
    domain_json: str
    metadata: tuple
    storage_id: str
    sha256: str
    incarnation: int
    content: bytes

    @property
    def absent(self):
        return not Request.from_buffer_copy(self.packet).expected.inode

    @property
    def version(self):
        return (*self.metadata, bytes.fromhex(self.sha256))

    @property
    def storage_key(self):
        return 'hermes-storage://' + self.storage_id + self.path


def runtime_helper_path():
    """Path to the standalone, dependency-free protected authority RPC helper."""
    return __file__


class ProtectedFuseEnvironment:
    uses_host_filesystem = False
    is_local = False
    env_type = 'docker'
    _hermes_backend_name = 'docker'
    cwd = '/cas'

    def __init__(self, container_id, task_label, *, generation, container_name=None,
                 payload_user='127776:456597', python='/usr/bin/python3.10'):
        if not re.fullmatch('[0-9a-f]{64}', container_id) or not task_label or not generation:
            raise ValueError('explicit exact container, label and generation required')
        self.container_id = container_id
        self.task_label = task_label
        self.container_name = container_name
        self.payload_user = payload_user
        self.python = python
        self._session_id = generation
        self._read_authorization_epoch = 0
        self.closed = False
        self.receipts = []
        self._docker = ['/usr/bin/sudo', '-n', '/usr/bin/docker']
        self._inspect()

    def _run(self, argv, data=None, timeout=20):
        try:
            p = subprocess.run(argv, input=data, capture_output=True, timeout=timeout, check=False)
            result = {'returncode': p.returncode, 'output': p.stdout.decode('utf-8', 'replace'),
                      'stderr': p.stderr.decode('utf-8', 'replace')}
        except subprocess.TimeoutExpired as exc:
            result = {'returncode': None, 'output': (exc.stdout or b'').decode('utf-8', 'replace'),
                      'stderr': (exc.stderr or b'').decode('utf-8', 'replace'), 'timeout': True}
        self.receipts.append({'argv': argv, **result})
        return result

    def file_generation(self):
        state = self._inspect()['State']
        return self._session_id + ':' + state['StartedAt'] + ':' + str(getattr(self, '_read_authorization_epoch', 0))

    def _inspect(self):
        from tools.file_backend_probe import FileBackendUnavailable
        r = self._run(self._docker + ['container', 'inspect', self.container_id])
        try:
            obj = json.loads(r['output'])[0]
            if (self.closed or r['returncode'] != 0 or obj['Id'] != self.container_id
                    or obj['Config']['Labels'].get('hermes.task') != self.task_label
                    or (obj['Name'] != '/' + self.container_name if self.container_name else
                        not obj['Name'].startswith('/hermes-cas-r6-r13-')) or not obj['State']['Running']):
                raise ValueError('unavailable exact task container')
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise FileBackendUnavailable('protected FUSE task container unavailable; refusing host fallback') from exc
        return obj

    def _exec(self, argv, data=None, cwd='/cas', timeout=20):
        self._inspect()
        return self._run(self._docker + ['container', 'exec', '-i', '--user=' + self.payload_user,
            '--workdir=' + cwd, '--env=LD_LIBRARY_PATH=/opt/runtime/lib', '--env=PYTHONDONTWRITEBYTECODE=1',
            self.container_id] + argv, data, timeout)

    def execute(self, command, cwd=None, timeout=20, stdin_data=None, **kwargs):
        data = None if stdin_data is None else stdin_data.encode('utf-8', 'surrogateescape')
        r = self._exec(['/bin/sh', '-c', command], data, cwd or self.cwd, timeout)
        return {**r, 'output': r['output'] + r.get('stderr', '')}

    def execute_file_probe(self, command, stdin_data=None):
        return self.execute(command, stdin_data=stdin_data)

    def _rpc(self, action, path, value=None):
        from tools.file_backend_probe import FileBackendUnavailable
        _path(path)
        argv = [self.python, '-I', '-S', '-B', '/authority/code/file_protected_fuse.py', action, path]
        r = self._exec(argv, None if value is None else json.dumps(value).encode())
        try:
            if type(r['returncode']) is not int or r['returncode'] != 0:
                raise ValueError(r.get('stderr') or 'transport failed')
            return json.loads(r['output'])
        except (ValueError, TypeError) as exc:
            raise FileBackendUnavailable('protected FUSE result unavailable; write outcome unknown; no retry') from exc

    def _ticket(self, path, value, generation):
        from tools.file_backend_probe import FileBackendUnavailable
        try:
            if not isinstance(value, dict):
                raise ValueError('invalid reply envelope')
            _path(path)
            raw = base64.b64decode(value['packet'], validate=True)
            if len(raw) != C.sizeof(Request):
                raise ValueError('invalid reply packet size')
            req = Request.from_buffer_copy(raw)
            e = req.expected
            data = base64.b64decode(value.get('data', ''), validate=True)
            absent = value.get('absent', False)
            metadata = value.get('metadata')
            domain = value['domain']
            storage_id = value['storage_id']
            domain_ok = (
                isinstance(domain, list) and len(domain) == 5
                and isinstance(domain[0], str) and str(uuid.UUID(domain[0])) == domain[0]
                and isinstance(domain[1], list) and len(domain[1]) == 6
                and all(isinstance(part, str) for part in domain[1])
                and domain[1][0].isdecimal() and domain[1][1].isdecimal()
                and re.fullmatch(r'[0-9]+:[0-9]+', domain[1][2])
                and domain[1][3].startswith('/') and domain[1][4] == '/cas'
                and bool(domain[1][5])
                and isinstance(domain[2], list) and len(domain[2]) >= 3
                and all(isinstance(part, str) for part in domain[2])
                and domain[2][0].startswith('fuse') and domain[2][1]
                and isinstance(domain[3], list) and len(domain[3]) == 5
                and isinstance(domain[4], list) and len(domain[4]) == 5
                and all(type(part) is int and part >= 0 for part in domain[3] + domain[4])
                and domain[3][1] > 0 and domain[3][2] <= 0o177777 and stat.S_ISDIR(domain[3][2])
                and domain[4][1] > 0 and domain[4][2] <= 0o177777 and stat.S_ISREG(domain[4][2]))
            expected_storage_id = hashlib.sha256(
                json.dumps(domain, separators=(',', ':')).encode()).hexdigest() if domain_ok else None
            if (type(absent) is not bool or not domain_ok
                    or not isinstance(storage_id, str) or storage_id != expected_storage_id
                    or req.abi != 2 or req.status != 1 or e.depth > DEPTH
                    or e.name.decode() != path[len('/cas/'):]
                    or absent != (e.inode == 0)):
                raise ValueError('invalid ticket authority or CAS identity')
            if e.inode:
                if (not isinstance(metadata, list) or len(metadata) != 6
                        or e.size != len(data) or metadata[1:5] != [e.inode, e.size, e.mtime_ns, e.ctime_ns]
                        or not isinstance(value.get('sha256'), str)
                        or not re.fullmatch('[0-9a-f]{64}', value['sha256'])
                        or hashlib.sha256(data).hexdigest() != value['sha256']):
                    raise ValueError('invalid target snapshot')
                version = Expected.from_buffer_copy(bytes(e))
                C.memset(C.addressof(version) + Expected.snapshot_id.offset, 0, 40)
                if metadata[5] != int.from_bytes(hashlib.sha256(bytes(version)).digest(), 'big'):
                    raise ValueError('invalid target version')
            elif data:
                raise ValueError('absent target carries snapshot data')
            domain_json = json.dumps(domain, separators=(',', ':'))
            return ReadTicket(path, generation, None, raw, domain_json, tuple(metadata or ()), storage_id,
                              hashlib.sha256(data).hexdigest(), e.incarnation, data)
        except (KeyError, OverflowError, TypeError, ValueError) as exc:
            raise FileBackendUnavailable('protected FUSE target reply invalid; refusing ticket') from exc

    def snapshot(self, path):
        from tools.file_backend_probe import FileBackendUnavailable
        from tools.file_backend_io import checked_generation
        generation = checked_generation(self)
        value = self._rpc('snapshot', path)
        checked_generation(self, generation)
        return self._ticket(path, value, generation)

    def target(self, path):
        from tools.file_backend_io import checked_generation
        generation = checked_generation(self)
        value = self._rpc('target', path)
        checked_generation(self, generation)
        return self._ticket(path, value, generation)

    def probe(self, path):
        from tools.file_backend_probe import ProbeResult
        try:
            ticket = self.target(str(path))
            if ticket.absent:
                return ProbeResult('absent', {'storage_id': ticket.storage_id, 'storage_path': ticket.path, 'errno': 2, 'ticket': ticket})
            return ProbeResult('present', {'storage_id': ticket.storage_id, 'storage_path': ticket.path,
                'canonical_path': ticket.path, 'metadata': list(ticket.metadata), 'mtime': ticket.metadata[3] / 1e9,
                'regular': True, 'sha256': ticket.sha256, 'ticket': ticket})
        except Exception as exc:
            logging.getLogger(__name__).exception('Protected FUSE probe unavailable')
            return ProbeResult('unavailable', error=f'protected FUSE probe unavailable: {exc}')

    def read_file(self, operations, path, offset, limit, *, ticket=None):
        from tools.file_backend_io import checked_generation
        from tools.file_operations_common import ReadResult
        from tools.file_backend_probe import FileBackendUnavailable
        from tools.tool_output_limits import get_max_line_length
        if ticket is None:
            ticket = self.snapshot(path)
        if (not isinstance(ticket, ReadTicket) or ticket.path != path
                or ticket.generation != checked_generation(self)):
            raise FileBackendUnavailable('protected FUSE read ticket is invalid; refusing read')
        data = ticket.content
        try:
            text = data.decode('utf-8')
        except UnicodeError:
            return ReadResult(error='unsupported protected FUSE encoding')
        if '\0' in text:
            return ReadResult(error='unsupported protected FUSE binary content', is_binary=True)
        text = text.removeprefix('\ufeff').replace('\r\n', '\n')
        lines = text.splitlines()
        selected = lines[offset - 1:offset - 1 + limit]
        result = ReadResult(content=operations._add_line_numbers('\n'.join(selected), offset),
            file_size=len(data), total_lines=len(lines), truncated=len(lines) > offset + limit - 1,
            truncated_lines=True if any(len(line) > get_max_line_length() for line in selected) else None,
            _snapshot=ticket.version)
        result._protected_ticket = ticket
        return result

    def publish(self, ticket, content):
        from tools.file_backend_probe import FileBackendUnavailable
        from tools.file_backend_io import checked_generation
        from tools.file_operations_common import ExecuteResult
        data = content.encode('utf-8', 'surrogateescape')
        if not isinstance(ticket, ReadTicket):
            raise FileBackendUnavailable('protected FUSE read generation changed; refusing write')
        original = Request.from_buffer_copy(ticket.packet).expected
        if original.name.decode() != ticket.path[5:]:
            raise FileBackendUnavailable('protected FUSE publish path differs from ticket; refusing write')
        checked_generation(self, ticket.generation)
        try:
            result = self._rpc('publish', ticket.path, {'path': ticket.path, 'packet': base64.b64encode(ticket.packet).decode(),
                'domain': json.loads(ticket.domain_json), 'candidate': base64.b64encode(data).decode(), 'operation': uuid.uuid4().hex})
            checked_generation(self, ticket.generation)
        except Exception as exc:
            from tools.file_backend_io import revoke_backend_reads
            revoke_backend_reads(self)
            raise FileBackendUnavailable('protected FUSE publish outcome unknown; no retry or fallback') from exc
        from tools.file_backend_io import mutation_boundary
        generation = mutation_boundary(self)
        state = result.get('state')
        response = ExecuteResult(stdout='' if state == 'committed' else 'protected FUSE ' + str(state) + '; no retry',
                                 exit_code=0 if state == 'committed' else 1)
        if state == 'committed':
            raw = base64.b64decode(result.get('published_packet', ''), validate=True)
            if len(raw) != C.sizeof(Request):
                raise FileBackendUnavailable('protected FUSE commit identity missing; write outcome unknown')
            packet = Request.from_buffer_copy(raw)
            expected = packet.expected
            original = Request.from_buffer_copy(ticket.packet).expected
            fields = ('incarnation', 'root_id', 'name') if ticket.absent else ('incarnation', 'root_id', 'parent_id', 'name', 'depth', 'mode', 'uid', 'gid')
            if (packet.abi != 2 or packet.status != 1 or expected.size != len(data)
                    or not expected.snapshot_id or expected.nlink != 1
                    or any(getattr(expected, key) != getattr(original, key) for key in fields)
                    or (not ticket.absent and bytes(expected.ancestors) != bytes(original.ancestors))):
                raise FileBackendUnavailable('protected FUSE commit identity invalid; write outcome unknown')
            version = Expected.from_buffer_copy(bytes(expected))
            C.memset(C.addressof(version) + Expected.snapshot_id.offset, 0, 40)
            metadata = (ticket.metadata[0] if ticket.metadata else json.loads(ticket.domain_json)[3][0], expected.inode, expected.size, expected.mtime_ns, expected.ctime_ns,
                        int.from_bytes(hashlib.sha256(bytes(version)).digest(), 'big'))
            response._protected_ticket = ReadTicket(ticket.path, generation, ticket.task_id, raw,
                ticket.domain_json, metadata, ticket.storage_id, hashlib.sha256(data).hexdigest(),
                expected.incarnation, data)
        return response

    def rebase_parents(self, ticket, proof):
        from tools.file_backend_io import checked_generation
        from tools.file_backend_probe import FileBackendUnavailable
        checked_generation(self, ticket.generation)
        value = self._rpc('rebase_parents', ticket.path, {'packet': base64.b64encode(ticket.packet).decode(),
            'proof': base64.b64encode(proof.packet).decode(), 'domain': json.loads(ticket.domain_json)})
        checked_generation(self, ticket.generation)
        if value.get('status') != 1:
            raise FileBackendUnavailable('protected FUSE parent proof conflict; refusing mutation')
        raw = base64.b64decode(value['packet'], validate=True)
        packet = Request.from_buffer_copy(raw)
        if (packet.abi != 2 or packet.status != 1 or packet.expected.inode or not packet.expected.snapshot_id
                or packet.expected.name.decode() != ticket.path[5:] or packet.expected.incarnation != ticket.incarnation):
            raise FileBackendUnavailable('protected FUSE parent proof identity invalid')
        from dataclasses import replace
        return replace(ticket, packet=raw)

    def namespace(self, ticket, destination=None):
        from tools.file_backend_io import checked_generation, revoke_backend_reads, mutation_boundary
        from tools.file_backend_probe import FileBackendUnavailable
        from tools.file_operations_common import ExecuteResult
        try:
            checked_generation(self, ticket.generation)
            if ticket.absent or (destination is not None and (not destination.absent or
                    destination.generation != ticket.generation or destination.domain_json != ticket.domain_json)):
                raise FileBackendUnavailable('protected FUSE namespace authorization invalid')
            value = self._rpc('namespace', ticket.path, {'packet': base64.b64encode(ticket.packet).decode(),
                'destination': base64.b64encode(destination.packet).decode() if destination else None,
                'operation': 'r13-' + uuid.uuid4().hex, 'domain': json.loads(ticket.domain_json)})
            checked_generation(self, ticket.generation)
        except Exception as exc:
            revoke_backend_reads(self)
            raise FileBackendUnavailable('protected FUSE namespace outcome unknown; no retry or fallback') from exc
        generation = mutation_boundary(self)
        if value.get('status') != 1:
            return ExecuteResult(stdout='protected FUSE conflict; mutation NOT committed', exit_code=1)
        try:
            if value.get('domain') != json.loads(ticket.domain_json):
                raise ValueError('namespace domain changed')
            packets = value['packets']
            if len(packets) != (2 if destination else 1):
                raise ValueError('namespace identity missing')
            def result_ticket(encoded, path, content, absent):
                raw = base64.b64decode(encoded, validate=True)
                packet = Request.from_buffer_copy(raw)
                expected = packet.expected
                if (packet.abi != 2 or packet.status != 1 or expected.name.decode() != path[5:]
                        or expected.incarnation != ticket.incarnation or bool(expected.inode) == absent
                        or expected.size != len(content) or not expected.snapshot_id):
                    raise ValueError('namespace identity invalid')
                version = Expected.from_buffer_copy(bytes(expected))
                C.memset(C.addressof(version) + Expected.snapshot_id.offset, 0, 40)
                metadata = () if absent else (ticket.metadata[0], expected.inode, expected.size,
                    expected.mtime_ns, expected.ctime_ns, int.from_bytes(hashlib.sha256(bytes(version)).digest(), 'big'))
                return ReadTicket(path, generation, ticket.task_id, raw, ticket.domain_json,
                    metadata, ticket.storage_id, hashlib.sha256(content).hexdigest(), ticket.incarnation, content)
            result = ExecuteResult()
            result._source_ticket = result_ticket(packets[0], ticket.path, b'', True)
            if destination:
                result._protected_ticket = result_ticket(packets[1], destination.path, ticket.content, False)
                previous = Request.from_buffer_copy(ticket.packet).expected
                current = Request.from_buffer_copy(result._protected_ticket.packet).expected
                if any(getattr(previous, key) != getattr(current, key) for key in ('inode', 'mode', 'uid', 'gid', 'size', 'mtime_ns', 'nlink')):
                    raise ValueError('move did not preserve source metadata')
            return result
        except Exception as exc:
            raise FileBackendUnavailable('protected FUSE namespace identity invalid; outcome unknown') from exc

    def cleanup(self):
        self.closed = True


def protected_fuse(env):
    return env if isinstance(env, ProtectedFuseEnvironment) else None


if __name__ == '__main__':
    try:
        handlers = {'snapshot': lambda: _snapshot(sys.argv[2]),
                    'target': lambda: _target(sys.argv[2]),
                    'namespace': lambda: _namespace(json.load(sys.stdin)),
                    'rebase_parents': lambda: _rebase_parents(json.load(sys.stdin)),
                    'publish': lambda: _publish(json.load(sys.stdin))}
        if sys.argv[1] not in handlers:
            raise ValueError('unsupported protected FUSE operation')
        result = handlers[sys.argv[1]]()
        print(json.dumps(result))
    except Exception:
        logging.getLogger(__name__).exception('Protected FUSE helper failed')
        sys.exit(1)
