#!/usr/bin/python3
"""Trusted transport shim inside one untrusted fixed-filesystem namespace.

Only JSON messages traverse stdio; command stdout never becomes control frames.
The shim does not hold a host directory FD, credential, bus or outside capability.
"""
import base64
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

BASE_ENV = {'PATH':'/usr/bin:/bin','HOME':'/home/sandbox','LANG':'C.UTF-8',
            'TMPDIR':'/state/tmp','PYTHONDONTWRITEBYTECODE':'1','HERMES_HOME':'/state/home'}
write_lock = threading.Lock()
jobs_lock = threading.RLock()
jobs = {}


def emit(message):
    with write_lock:
        sys.stdout.write(json.dumps(message,separators=(',',':')) + '\n')
        sys.stdout.flush()


def proc_table():
    result = {}
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            try:
                raw = (path / 'stat').read_bytes()
                fields = raw[raw.rfind(b')')+2:].split()
                result[int(path.name)] = (int(fields[1]),fields[19])
            except (OSError,ValueError,IndexError):
                continue
    return result


def cancel(identifier):
    with jobs_lock:
        job = jobs.get(identifier)
        if job is None:
            return
        job['cancelled'] = True
        proc = job.get('proc')
        if proc is None:
            return
        # Snapshot descendants before signalling the parent; detached sessions
        # remain children while the foreground root is alive.
        table = proc_table()
        owned = {proc.pid}
        while True:
            add = {pid for pid,(ppid,_) in table.items() if ppid in owned}
            if add <= owned:
                break
            owned |= add
        try:
            os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError:
            pass
        for pid in sorted(owned,reverse=True):
            if pid in (1,os.getpid()):
                continue
            try:
                current = proc_table().get(pid)
                # Reparenting after SIGTERM changes PPID; creation time is the
                # ownership guard, not the mutable parent pointer.
                if current is not None and current[1] == table[pid][1]:
                    os.kill(pid,signal.SIGKILL)
            except ProcessLookupError:
                pass


def execute(request):
    identifier = request['id']
    proc = None
    try:
        with jobs_lock:
            job = jobs[identifier]
            if job['cancelled']:
                emit({'id':identifier,'event':'exit','code':130})
                return
            proc = subprocess.Popen(['/bin/bash','--noprofile','--norc','-c',request['command']],
                cwd='/workspace',env=BASE_ENV,stdin=subprocess.PIPE if request.get('stdin') is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,close_fds=True,start_new_session=True)
            job['proc'] = proc
        def feed():
            try:
                proc.stdin.write(request['stdin'].encode('utf-8'))
                proc.stdin.close()
            except (BrokenPipeError,OSError):
                pass
        if request.get('stdin') is not None:
            threading.Thread(target=feed,daemon=True).start()
        while True:
            chunk = proc.stdout.read1(32768)
            if not chunk:
                break
            emit({'id':identifier,'event':'output','data':base64.b64encode(chunk).decode('ascii')})
        code = proc.wait()
        emit({'id':identifier,'event':'exit','code':code})
    except Exception as exc:
        emit({'id':identifier,'event':'output','data':base64.b64encode(str(exc).encode()).decode('ascii')})
        emit({'id':identifier,'event':'exit','code':125})
    finally:
        with jobs_lock:
            jobs.pop(identifier,None)


def main():
    os.umask(0o077)
    # Payload children share this PID namespace. Deny their /proc/<shim>/fd
    # and ptrace access to the shim's control pipe, even under this userns UID.
    libc = ctypes.CDLL(None,use_errno=True)
    if libc.prctl(4,0,0,0,0) != 0:  # PR_SET_DUMPABLE=0
        raise OSError(ctypes.get_errno(),'cannot protect worker control FDs')
    Path('/state/tmp').mkdir(exist_ok=True)
    Path('/state/home').mkdir(exist_ok=True)
    emit({'event':'ready','pid':os.getpid(),
          'namespaces':{n:os.readlink('/proc/self/ns/' + n) for n in ('mnt','pid','user','net','ipc')},
          'fds':{p.name:os.readlink(p) for p in Path('/proc/self/fd').iterdir() if p.exists()}})
    for line in sys.stdin:
        if len(line) > 8_000_000:
            return 65
        request = json.loads(line)
        if request.get('op') == 'shutdown':
            return 0
        if request.get('op') == 'cancel':
            cancel(request['id'])
        elif request.get('op') == 'run':
            with jobs_lock:
                if request['id'] in jobs:
                    return 66
                jobs[request['id']] = {'cancelled':False}
            threading.Thread(target=execute,args=(request,),daemon=True).start()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
