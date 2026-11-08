#!/usr/bin/python3
"""Trusted ancestor shim: every payload receives a child PID/mount/user scope."""
import base64
import ctypes
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

write_lock=threading.Lock()
jobs_lock=threading.RLock()
jobs={}
MAX_JOBS=32

def emit(message):
    with write_lock:
        sys.stdout.write(json.dumps(message,separators=(',',':'))+'\n');sys.stdout.flush()

def send_control(job,message):
    raw=(json.dumps(message,separators=(',',':'))+'\n').encode()
    with job['control_lock']:
        fd=job.get('control')
        if fd is None:return
        try:
            while raw:raw=raw[os.write(fd,raw):]
        except BrokenPipeError:pass

def cancel(identifier):
    with jobs_lock:
        job=jobs.get(identifier)
        if job is None:return
        job['cancelled']=True
    # The endpoint terminates in PID 1 of this invocation, with no numeric PID
    # lookup or reuse window, including cancellation before payload readiness.
    send_control(job,{'op':'cancel'})

def execute(request):
    identifier=request['id'];proc=None;control_r=control_w=report_r=report_w=None
    scope_receipt=None;code=125
    with jobs_lock:job=jobs[identifier]
    try:
        control_r,control_w=os.pipe();report_r,report_w=os.pipe()
        argv=['/usr/bin/unshare','--user','--map-root-user','--mount','--pid','--fork',
              '--kill-child=KILL','--mount-proc','/usr/bin/python3','-I','-B','/boundary/scope_init.py',str(control_r),str(report_w)]
        proc=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},cwd='/workspace',
            close_fds=True,pass_fds=(control_r,report_w),start_new_session=True)  # detached-spawn: ok — retained launcher, private cancel pipe and namespace reaping
        launcher_identity={'pid':proc.pid,
            'start_ticks':int(Path('/proc/'+str(proc.pid)+'/stat').read_text(encoding='utf-8-sig').rsplit(')',1)[1].split()[19]),
            'pid_namespace':os.readlink('/proc/self/ns/pid')}
        os.close(control_r);control_r=None;os.close(report_w);report_w=None
        send_control({'control':control_w,'control_lock':job['control_lock']},
                     {'command':request['command'],'stdin':request.get('stdin'),'lifetime':request.get('lifetime','command')})
        with jobs_lock:
            job.update(proc=proc,control=control_w)
            cancelled=job['cancelled']
        if cancelled:send_control(job,{'op':'cancel'})
        def reports():
            nonlocal scope_receipt
            with os.fdopen(report_r,'r',encoding='utf-8-sig') as stream:
                for line in stream:
                    msg=json.loads(line)
                    if msg.get('event')=='scope_ready':
                        emit({'id':identifier,'event':'started','scope':msg,'owner_generation':identifier})
                    elif msg.get('event')=='scope_empty':scope_receipt=msg
        reporter=threading.Thread(target=reports,daemon=True);reporter.start()
        while True:
            chunk=proc.stdout.read1(32768)
            if not chunk:break
            emit({'id':identifier,'event':'output','data':base64.b64encode(chunk).decode('ascii')})
        launcher_exit=proc.wait(timeout=5);reporter.join(2)
        if scope_receipt is not None and scope_receipt.get('descendant_empty') is True and launcher_exit==0:
            code=scope_receipt['root_exit']
            scope_receipt.update(owner_generation=identifier,launcher_exit=launcher_exit,
                launcher_identity=launcher_identity,launcher_reaped=proc.poll() is not None)
        else:
            scope_receipt={'owner_generation':identifier,'root_exit':None,'descendant_empty':False,
                           'launcher_exit':launcher_exit,'diagnostic':'owned scope termination unverified'}
        emit({'id':identifier,'event':'exit','code':code,'lifecycle':scope_receipt})
    except BaseException as exc:
        logging.getLogger(__name__).exception('Owned invocation transport failed')
        emit({'id':identifier,'event':'output','data':base64.b64encode(str(exc).encode()).decode('ascii')})
        emit({'id':identifier,'event':'exit','code':125,'lifecycle':{'root_exit':None,'descendant_empty':False,'diagnostic':repr(exc)}})
    finally:
        with jobs_lock:
            jobs.pop(identifier,None)
        with job['control_lock']:
            job['control']=None
            if control_w is not None:
                os.close(control_w)
                control_w=None
        for fd in (control_r,control_w,report_w):
            if fd is not None:
                try:os.close(fd)
                except OSError:pass
        if proc is not None:proc.stdout.close()

def main():
    os.umask(0o077)
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(4,0,0,0,0)!=0:raise OSError(ctypes.get_errno(),'protect worker control FDs')
    Path('/state/tmp').mkdir(exist_ok=True);Path('/state/home').mkdir(exist_ok=True)
    emit({'event':'ready','pid':os.getpid(),
          'namespaces':{n:os.readlink('/proc/self/ns/'+n) for n in ('mnt','pid','user','net','ipc')},
          'fds':{p.name:os.readlink(p) for p in Path('/proc/self/fd').iterdir() if p.exists()},
          'protocol':'owned-scope.v2','max_jobs':MAX_JOBS})
    for line in sys.stdin:
        if len(line)>8_000_000:return 65
        req=json.loads(line);op=req.get('op')
        if op=='shutdown':return 0
        if op=='cancel':cancel(req['id'])
        elif op=='run':
            with jobs_lock:
                if req['id'] in jobs:return 66
                if len(jobs)>=MAX_JOBS:
                    emit({'id':req['id'],'event':'exit','code':125,'lifecycle':{'descendant_empty':True,'root_exit':None,'diagnostic':'active call limit 32'}})
                    continue
                jobs[req['id']]={'cancelled':False,'control_lock':threading.Lock()}
            threading.Thread(target=execute,args=(req,),daemon=True).start()
    return 0
if __name__=='__main__':raise SystemExit(main())
