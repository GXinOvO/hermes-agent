import errno
import inspect
import json
import logging
import math
import posixpath
import re
import shlex
from dataclasses import dataclass


class FileBackendUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    state: str
    data: dict | None = None
    error: str = ''


_IDENTITY = '''import hashlib,json,os,sys
p=os.path.realpath(sys.executable)
s=os.stat(p)
with open(p,'rb') as f: digest=hashlib.sha256(f.read()).hexdigest()
print(json.dumps({'python':p,'identity':[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,digest]}))
'''
_PROBE = '''import errno,hashlib,json,os,re,stat,sys
p=sys.argv[1]
if not os.path.isabs(p): raise ValueError('unbound backend path')
canonical=os.path.realpath(p)
def unescape(value):
 return re.sub(r'\\\\([0-7]{3})',lambda m:chr(int(m.group(1),8)),value)
def _storage(canonical):
 try:
  with open('/proc/self/mountinfo') as f: mounts=[line.split() for line in f]
  mounts=[m for m in mounts if canonical==unescape(m[4]) or canonical.startswith(unescape(m[4]).rstrip('/')+'/')]
  m=max(mounts,key=lambda m:len(unescape(m[4])))
  separator=m.index('-')
  fstype,source=m[separator+1:separator+3]
  with open('/proc/sys/kernel/random/boot_id') as f: host=f.read().strip()
  identity_source=[fstype,source] if fstype in ('nfs','nfs4','cifs') else [host,m[2],fstype,source]
  return (hashlib.sha256(json.dumps(identity_source,separators=(',',':')).encode()).hexdigest(),
          os.path.normpath(unescape(m[3]).rstrip('/')+'/'+os.path.relpath(canonical,unescape(m[4]))))
 except (OSError,ValueError):
  if sys.platform.startswith('linux'): raise
  # No /proc (macOS/BSD): identify the storage by the device number of the nearest existing
  # ancestor on this host, so two paths on one filesystem still agree and another mount,
  # host or filesystem does not.
  import platform
  ancestor=canonical
  while not os.path.exists(ancestor) and os.path.dirname(ancestor)!=ancestor: ancestor=os.path.dirname(ancestor)
  identity_source=[platform.system(),platform.node(),os.stat(ancestor).st_dev]
  return hashlib.sha256(json.dumps(identity_source,separators=(',',':')).encode()).hexdigest(), canonical
storage_id,storage_path=_storage(canonical)
python=os.path.realpath(sys.executable)
s=os.stat(python)
with open(python,'rb') as f: interpreter=hashlib.sha256(f.read()).hexdigest()
identity=[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,interpreter]
try:
 s=os.stat(p)
 data={'state':'present','metadata':[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns],
       'mtime':s.st_mtime,'regular':stat.S_ISREG(s.st_mode),'canonical_path':os.path.realpath(p)}
 if sys.argv[2] in ('hash','commit') and data['regular']:
  fd=os.open(p,os.O_RDONLY|getattr(os,'O_NONBLOCK',0))
  with os.fdopen(fd,'rb') as f:
   before=os.fstat(f.fileno())
   if not stat.S_ISREG(before.st_mode): raise OSError(errno.EAGAIN,'file type changed')
   h=hashlib.sha256()
   for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)
   end=os.fstat(f.fileno())
  after=os.stat(p)
  fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
  version=tuple(getattr(before,k) for k in fields)
  if version!=tuple(data['metadata']) or version!=tuple(getattr(after,k) for k in fields) or version!=tuple(getattr(end,k) for k in fields):
   raise OSError(errno.EAGAIN,'file changed while hashing')
  data['sha256']=h.hexdigest()
except OSError as e:
 data={'state':'absent' if e.errno in (errno.ENOENT,errno.ENOTDIR) else 'unavailable','errno':e.errno}
data.update(python=python,identity=identity,storage_id=storage_id,storage_path=storage_path)
if sys.argv[2]=='commit':
 target=target_snapshot(p)
 if target['canonical']!=canonical or target['version']!=data.get('metadata') or target['sha256']!=data.get('sha256'):
  raise OSError(errno.EAGAIN,'commit target changed during probe')
 data['target']=target
print(json.dumps(data))
'''


def environment_generation(env):
    try:
        from tools.environments.sandbox_scope import ScopedEnvironment
        if isinstance(env, ScopedEnvironment):
            getter = getattr(env, 'file_generation', None)
            try:
                generation = getter() if callable(getter) else None
            except (TimeoutError, OSError) as exc:
                raise FileBackendUnavailable('backend file-state identity is unavailable; refusing write') from exc
        else:
            generation = getattr(env, '_session_id', None)
        if not isinstance(generation, str) or not generation or getattr(env, 'closed', False):
            raise FileBackendUnavailable('backend file-state identity is unavailable; refusing write')
        from tools.file_protected_fuse import protected_fuse
        if protected_fuse(env) is not None:
            return env.file_generation()
        return generation
    except FileBackendUnavailable:
        from tools.file_backend_io import revoke_backend_reads
        revoke_backend_reads(env)
        raise


def _execute(env, command):
    result = env.execute_file_probe(command)
    if type(result.get('returncode')) is not int or result['returncode'] != 0:
        raise FileBackendUnavailable('backend file-state probe execution failed; refusing write')
    value = json.loads(result['output'])
    if not isinstance(value, dict):
        raise FileBackendUnavailable('malformed backend file-state probe; refusing write')
    return value


def _interpreter(value):
    python, identity = value.get('python'), value.get('identity')
    if not isinstance(python, str) or not posixpath.isabs(python):
        raise FileBackendUnavailable('unbound backend probe interpreter; refusing write')
    if (not isinstance(identity, list) or len(identity) != 6
            or any(type(x) is not int for x in identity[:5])
            or not isinstance(identity[5], str) or not re.fullmatch('[0-9a-f]{64}', identity[5])):
        raise FileBackendUnavailable('unconfirmed backend probe interpreter; refusing write')
    return python, identity


def _validate(value, hash_bytes):
    state = value.get('state')
    if 'sha256' in value and (not isinstance(value['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
        return ProbeResult('unavailable', error='malformed backend file hash')
    if (not isinstance(value.get('storage_id'), str) or not re.fullmatch('[0-9a-f]{64}', value['storage_id'])
            or not isinstance(value.get('storage_path'), str) or not posixpath.isabs(value['storage_path'])):
        return ProbeResult('unavailable', error='backend storage identity is unavailable')
    if state == 'absent' and type(value.get('errno')) is int and value['errno'] in (errno.ENOENT, errno.ENOTDIR):
        return ProbeResult('absent', value)
    if state != 'present':
        return ProbeResult('unavailable', error='backend could not establish file state')
    metadata, mtime = value.get('metadata'), value.get('mtime')
    if (not isinstance(metadata, list) or len(metadata) != 5 or any(type(x) is not int for x in metadata)
            or type(mtime) not in (int, float) or not math.isfinite(mtime)
            or type(value.get('regular')) is not bool
            or not isinstance(value.get('canonical_path'), str) or not posixpath.isabs(value['canonical_path'])):
        return ProbeResult('unavailable', error='malformed backend file metadata')
    if hash_bytes and (not value['regular'] or not isinstance(value.get('sha256'), str)
                       or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
        return ProbeResult('unavailable', error='backend file hash is unavailable')
    return ProbeResult('present', value)


def probe_environment(env, path, *, hash_bytes=False, commit=False):
    from tools.file_backend_io import checked_generation
    from tools.file_protected_fuse import protected_fuse
    if protected_fuse(env) is not None:
        return env.probe(path)
    try:
        generation = environment_generation(env)
        binding = getattr(env, '_file_probe_interpreter', None)
        if binding is None or binding[0] != generation:
            identity = _execute(env, 'command -p python3 -I -B -c ' + shlex.quote(_IDENTITY))
            python, signature = _interpreter(identity)
            binding = generation, python, signature
            checked_generation(env, generation)
            env._file_probe_interpreter = binding
        program = _PROBE
        if commit:
            from tools import file_operations_commit
            program = inspect.getsource(file_operations_commit) + program
        mode = 'commit' if commit else 'hash' if hash_bytes else 'metadata'
        command = ('exec ' + shlex.quote(binding[1]) + ' -I -B -c ' + shlex.quote(program)
                   + ' ' + shlex.quote(str(path)) + ' ' + mode)
        value = _execute(env, command)
        checked_generation(env, generation)
        if _interpreter(value) != (binding[1], binding[2]):
            raise FileBackendUnavailable('backend probe identity changed; refusing write')
        return _validate(value, hash_bytes)
    except Exception as exc:
        logging.getLogger(__name__).debug('backend file probe failed', exc_info=True)
        return ProbeResult('unavailable', error=f'backend file-state probe unavailable: {type(exc).__name__}: {exc}')


def execute_commit(env, command, content):
    result = env.execute_file_probe(command, stdin_data=content)
    if type(result.get('returncode')) is not int or result.get('stdin_error'):
        raise FileBackendUnavailable('backend commit execution failed; write outcome unknown')
    return result
