#!/usr/bin/python3
"""PID-namespace init for exactly one owned command (trusted, PID 1).

Only the ancestor shim holds the private control pipe. The payload receives no
control/report FDs and cannot address the ancestor PID namespace. Completion is
reported only after waitpid collected the root and returned ECHILD for the scope.
"""
import ctypes
import json
import os
import select
import signal
import subprocess
import sys
import threading

BASE_ENV={'PATH':'/usr/bin:/bin','HOME':'/home/sandbox','LANG':'C.UTF-8',
          'TMPDIR':'/state/tmp','PYTHONDONTWRITEBYTECODE':'1','HERMES_HOME':'/state/home'}
payload_options = {}
workspace = '/workspace'
def harden():
    # Python installs a SIGINT handler; PID 1 would otherwise accept it from
    # model code and raise KeyboardInterrupt in the trusted owner. Default
    # dispositions retain kernel PID-1 immunity; control uses the private pipe.
    signal.signal(signal.SIGINT,signal.SIG_DFL)
    libc=ctypes.CDLL(None,use_errno=True)
    # unshare has finished mounting proc; payload must not retain mount/signal caps.
    with open('/proc/self/status',encoding='utf-8-sig') as stream:
        bounding=int(next(line.split()[1] for line in stream if line.startswith('CapBnd:')),16)
    if bounding:
        for cap in range(64):
            rc=libc.prctl(24,cap,0,0,0)  # PR_CAPBSET_DROP; EINVAL beyond supported caps
            if rc and ctypes.get_errno()!=22:raise OSError(ctypes.get_errno(),'cap bounding drop')
    class Header(ctypes.Structure):_fields_=[('version',ctypes.c_uint32),('pid',ctypes.c_int)]
    class Data(ctypes.Structure):_fields_=[('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
    header=Header(0x20080522,0); data=(Data*2)()
    if libc.capset(ctypes.byref(header),ctypes.byref(data))!=0:raise OSError(ctypes.get_errno(),'capset')
    if libc.prctl(38,1,0,0,0)!=0:raise OSError(ctypes.get_errno(),'no_new_privs')
    if libc.prctl(4,0,0,0,0)!=0:raise OSError(ctypes.get_errno(),'dumpable')
def main():
    control,report=map(int,sys.argv[1:])
    require_pid=os.getpid()
    if require_pid!=1:raise RuntimeError('owned init must be PID 1')
    os.umask(0o077);harden()
    def emit(message):
        raw=(json.dumps(message,separators=(',',':'))+'\n').encode()
        while raw:raw=raw[os.write(report,raw):]
    buffered=b''
    while b'\n' not in buffered:
        chunk=os.read(control,65536)
        if not chunk:return 125
        buffered+=chunk
    line,buffered=buffered.split(b'\n',1);req=json.loads(line)
    emit({'event':'scope_ready','namespace':os.readlink('/proc/self/ns/pid')})
    root=subprocess.Popen(['/bin/bash','--noprofile','--norc','-c',req['command']],
        cwd=workspace,env=BASE_ENV,stdin=subprocess.PIPE if req.get('stdin') is not None else subprocess.DEVNULL,
        stdout=None,stderr=None,close_fds=True,start_new_session=True, **payload_options)  # detached-spawn: ok — PID 1 owns/reaps the complete invocation, including detached descendants
    if req.get('stdin') is not None:
        def feed():
            try:root.stdin.write(req['stdin'].encode());root.stdin.close()
            except (BrokenPipeError,OSError):pass
        threading.Thread(target=feed,daemon=True).start()
    root_exit=None;cancelled=False;cleaning=False;empty=False;reaped=0
    os.set_blocking(control,False)
    while not empty:
        try:
            while True:
                pid,status=os.waitpid(-1,os.WNOHANG)
                if pid==0:break
                reaped+=1
                if pid==root.pid:
                    root_exit=os.waitstatus_to_exitcode(status);root.returncode=root_exit
        except ChildProcessError:
            empty=True
        if root_exit is not None and req.get('lifetime')!='background' and not cleaning:
            cleaning=True
        if cleaning and not empty:
            # Linux kill(-1) is restricted to this PID namespace; PID 1 is excluded.
            try:os.kill(-1,signal.SIGKILL)  # windows-footgun: ok — Linux-only trusted PID-namespace init
            except ProcessLookupError:pass
        if empty:break
        while b'\n' in buffered:
            line,buffered=buffered.split(b'\n',1)
            msg=json.loads(line)
            if msg.get('op')=='cancel':cancelled=True;cleaning=True
        if cleaning:
            continue
        ready,_,_=select.select([control],[],[],.01)
        if ready:
            chunk=os.read(control,65536)
            if not chunk:
                cancelled=True;cleaning=True
            else:
                buffered+=chunk
    if root_exit is None:raise RuntimeError('scope empty without root wait receipt')
    emit({'event':'scope_empty','namespace':os.readlink('/proc/self/ns/pid'),
          'root_exit':root_exit,'descendant_empty':True,'waitpid_echild':True,'cancelled':cancelled,
          'root_pid':root.pid,'reaped_count':reaped})
    os.close(control);os.close(report)
    return 0
if __name__=='__main__':raise SystemExit(main())
