"""Owned Docker/FUSE resource lifecycle. No downloads or host filesystem fallback."""

import hashlib
import io
import json
import logging

from pathlib import Path
import re
import subprocess
import tarfile
import time
import uuid

from .environment import WORKER_ROOT, canonical_directory

IMAGE = 'sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3'
BINARY = 'eead80e189d117e23b7565b6b616fca76ae9b762d8dc5c69e84603df6e24bd40'
DOCKER = ['/usr/bin/sudo', '-n', '/usr/bin/docker']


def settings(value):
    required = ('authority_root', 'state_root', 'volume', 'label', 'image')
    if not isinstance(value, dict) or any(not value.get(k) for k in required):
        raise ValueError('protected settings require authority_root/state_root/volume/label/image')
    result = dict(value)
    result['authority_root'] = canonical_directory(value['authority_root'])
    result['state_root'] = canonical_directory(value['state_root'])
    for key in ('volume', 'label'):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{2,100}', value[key]):
            raise ValueError('invalid protected resource ' + key)
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', value['image']):
        raise ValueError('protected image must be an existing immutable image ID')
    if value.get('network', 'private') != 'private':
        raise ValueError('protected backend currently qualifies only private offline networking')
    authority = result['authority_root']
    if hashlib.sha256((authority / 'daemon-protected').read_bytes()).hexdigest() != BINARY:
        raise ValueError('protected authority binary identity mismatch')
    if hashlib.sha256((authority / 'daemon-protected.c').read_bytes()).hexdigest() != '928d50ff5af14a783a50efc1249c8596e2600a3306c762d6a78f26f0c0c6bcdc':
        raise ValueError('protected authority source identity mismatch')
    return result


class ProtectedService:
    def __init__(self, config):
        self.config = settings(config)
        self.state = self.config['state_root'] / uuid.uuid4().hex
        self.state.mkdir(mode=0o700)
        self.ids = []
        self.records = []
        self.active = None
        self.closed = False
        self.lock = None
        self.owner = hashlib.sha256(str(self.config['state_root']).encode()).hexdigest()
        try:
            import fcntl
            self.lock = (self.config['state_root'] / 'volume.lock').open('a+')
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._start()
        except BaseException as exc:
            try:
                self.close()
            except Exception as cleanup_error:
                logging.getLogger(__name__).exception('Protected constructor cleanup failed')
                exc.add_note('protected constructor cleanup: ' + repr(cleanup_error))
            raise

    def command(self, args, *, data=None, timeout=30, allow_error=False):
        argv = DOCKER + list(args)
        result = subprocess.run(argv, input=data, capture_output=True, timeout=timeout, check=False)
        record = {'argv': argv, 'exit': result.returncode,
                  'stdout': result.stdout.decode('utf-8', 'replace'), 'stderr': result.stderr.decode('utf-8', 'replace')}
        self.records.append(record)
        (self.state / 'commands.json').write_text(json.dumps(self.records, indent=2), encoding='utf-8')
        if result.returncode and not allow_error:
            raise RuntimeError('protected Docker operation failed: ' + json.dumps(record))
        return record

    def inspect(self, identity):
        obj = json.loads(self.command(['container', 'inspect', identity])['stdout'])[0]
        if obj['Id'] != identity or obj['Config']['Labels'].get('hermes.task') != self.config['label']:
            raise RuntimeError('protected exact resource identity changed')
        return obj

    def _create(self, suffix, mounts, entry, caps=()):
        name = 'hermes-protected-' + self.state.name + '-' + suffix
        absent = self.command(['container', 'inspect', name], allow_error=True)
        if absent['exit'] == 0 or 'no such' not in absent['stderr'].lower():
            raise RuntimeError('protected resource absence unverified')
        args = ['container', 'create', '--name=' + name, '--pull=never', '--network=none', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--user=0:0', '--pids-limit=128',
                '--memory=512m', '--label=hermes.task=' + self.config['label'],
                '--mount=type=volume,src=' + self.config['volume'] + ',dst=/authority,volume-nocopy',
                *mounts, *caps, '--entrypoint=/bin/sh', self.config['image'], '-ceu', entry]
        identity = self.command(args)['stdout'].strip()
        if not re.fullmatch('[0-9a-f]{64}', identity):
            raise RuntimeError('protected create returned no exact ID; cleanup outcome unknown')
        self.ids.append(identity)
        obj = self.inspect(identity)
        if obj['Image'] != self.config['image'] or obj['HostConfig']['NetworkMode'] != 'none' or obj['HostConfig']['Privileged']:
            raise RuntimeError('protected launch policy readback failed')
        return identity, name

    def _volume(self):
        name = self.config['volume']
        record = self.command(['volume', 'inspect', name], allow_error=True)
        if record['exit']:
            if 'no such' not in record['stderr'].lower():
                raise RuntimeError('protected volume state unknown')
            self.command(['volume', 'create', '--label=hermes.task=' + self.config['label'],
                          '--label=hermes.owner=' + self.owner, name])
        obj = json.loads(self.command(['volume', 'inspect', name])['stdout'])[0]
        if obj['Name'] != name or obj['Labels'].get('hermes.owner') != self.owner or obj['Labels'].get('hermes.task') != self.config['label']:
            raise RuntimeError('protected volume belongs to another owner; refusing initialization')

    def _bundle(self):
        authority = self.config['authority_root']
        files = {name: authority / name for name in ('daemon-protected', 'daemon-protected.c', 'protocol.h')}
        files.update({name: WORKER_ROOT / name for name in ('worker.py', 'scope_init.py', 'protected_init.py')})
        # The helper is copied and rechecked in a root-owned code directory before execution.
        from tools.file_protected_fuse import runtime_helper_path
        files['file_protected_fuse.py'] = Path(runtime_helper_path())
        hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
        bundle = self.state / 'bootstrap.tar'
        with tarfile.open(bundle, 'w') as archive:
            for name, path in files.items():
                data = path.read_bytes()
                info = tarfile.TarInfo('code/' + name)
                info.uid = info.gid = 0
                info.mode = 0o555 if name == 'daemon-protected' else 0o444
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        (self.state / 'code-inputs.json').write_text(json.dumps(hashes, indent=2), encoding='utf-8')
        checks = '; '.join('test "$(sha256sum /authority/code/' + k + ' | cut -d" " -f1)" = ' + v for k, v in hashes.items())
        return bundle, checks

    def _mounts(self):
        runtime = json.loads((self.config['authority_root'] / 'runtime-inputs.json').read_text(encoding='utf-8-sig'))
        mounts = []
        for item in runtime:
            path = Path(item['source'])
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o022 or hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise RuntimeError('protected trusted runtime identity changed')
            mounts.append('--mount=type=bind,src=' + str(path) + ',dst=' + item['destination'] + ',readonly')
        for path in Path('/usr/lib/python3.10').rglob('*'):
            info = path.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('protected stdlib must be system owned and immutable to ordinary users')
        mounts += ['--mount=type=bind,src=/usr/bin/python3.10,dst=/usr/bin/python3,readonly',
                   '--mount=type=bind,src=/usr/lib/python3.10,dst=/usr/lib/python3.10,readonly']
        return mounts

    def _start(self):
        found = json.loads(self.command(['image', 'inspect', '--format={{json .Id}}', self.config['image']])['stdout'])
        if found != self.config['image']:
            raise RuntimeError('protected image identity mismatch')
        self._volume()
        bundle, checks = self._bundle()
        boot, _ = self._create('bootstrap', ['--mount=type=bind,src=' + str(bundle) + ',dst=/bootstrap.tar,readonly'],
            'umask 077; mkdir -p /authority/state /authority/code; chmod 0755 /authority /authority/code; '
            'chmod 0700 /authority/state; tar -xf /bootstrap.tar --no-same-owner --no-same-permissions -C /authority; '
            'chmod 0555 /authority/code/daemon-protected; chmod 0444 /authority/code/*.py /authority/code/*.c /authority/code/*.h; '
            + checks + '; chmod 0555 /authority/code')
        self.command(['container', 'start', '--attach', boot])
        if self.inspect(boot)['State']['ExitCode']:
            raise RuntimeError('protected bootstrap failed')
        self.active, self.name = self._create('authority', self._mounts() + [
            '--tmpfs=/cas:rw,nosuid,nodev,noexec,size=1m,mode=0755',
            '--tmpfs=/state:rw,nosuid,nodev,size=64m,mode=0777',
            '--tmpfs=/home/sandbox:rw,nosuid,nodev,size=16m,mode=0700,uid=127776,gid=456597',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=32m,mode=1777'],  # no-tmp: ok — container-only tmpfs
            checks + '; mkdir -p /state/tmp /state/home; chmod 0777 /state/tmp /state/home; export LD_LIBRARY_PATH=/opt/runtime/lib; '
            'exec /authority/code/daemon-protected /authority/state/authority.sqlite /cas',
            ['--cap-add=SYS_ADMIN', '--cap-add=SETUID', '--cap-add=SETGID', '--cap-add=KILL',
             '--cap-add=SETPCAP', '--device=/dev/fuse', '--security-opt=apparmor=unconfined'])
        self.command(['container', 'start', self.active])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            obj = self.inspect(self.active)
            if not obj['State']['Running']:
                raise RuntimeError('protected authority did not stay running: ' + self.command(['container', 'logs', self.active])['stdout'])
            record = self.command(['container', 'exec', self.active, '/bin/cat', '/proc/1/mountinfo'])
            if any(' /cas ' in line and ' - fuse' in line for line in record['stdout'].splitlines()):
                return
            time.sleep(.05)
        raise RuntimeError('protected authority mount unavailable')

    def transport(self):
        self.inspect(self.active)
        return DOCKER + ['container', 'exec', '-i', '--user=0:0', '--env=LD_LIBRARY_PATH=/opt/runtime/lib',
                         '--env=PYTHONDONTWRITEBYTECODE=1', self.active,
                         '/usr/bin/python3', '-I', '-B', '/authority/code/worker.py', '--protected']

    def close(self):
        if self.closed:
            return
        failures = []
        for identity in reversed(self.ids):
            try:
                obj = self.inspect(identity)
                if obj['State']['Running']:
                    self.command(['container', 'stop', '--time=2', identity])
                self.command(['container', 'logs', identity])
                self.command(['container', 'rm', identity])
                absent = self.command(['container', 'inspect', identity], allow_error=True)
                if absent['exit'] == 0 or 'no such' not in absent['stderr'].lower():
                    raise RuntimeError('protected exact container absence unknown')
            except Exception as exc:
                logging.getLogger(__name__).exception('Protected exact resource cleanup failed')
                failures.append(repr(exc))
        if self.lock is not None and not self.lock.closed:
            self.lock.close()
        self.closed = not failures
        (self.state / 'cleanup.json').write_text(json.dumps({'closed': self.closed, 'errors': failures,
            'retained_volume': self.config['volume'], 'ids': self.ids}, indent=2), encoding='utf-8')
        if failures:
            raise RuntimeError('protected cleanup incomplete: ' + repr(failures))
