#!/usr/bin/python3
"""Trusted protected-mode PID 1; the payload never owns the control plane."""
import ctypes
import importlib.util
import os
import signal

import subprocess
import sys

def payload_identity():
    os.setgroups([])
    os.setgid(456597)
    os.setuid(127776)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'payload no_new_privs')
    with open('/proc/self/status', encoding='utf-8-sig') as stream:
        status = dict(line.split(':', 1) for line in stream if ':' in line)
    if any(int(status[name].strip(), 16) for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')):
        raise RuntimeError('protected payload retained capabilities')
    # Popen applies its cwd before preexec_fn. Only the final payload owner
    # may enter the authority's mode-0700 root; PID 1 stays at a trusted cwd.
    os.chdir('/cas')


def protect_init():
    """Keep KILL/SETUID/SETGID only in trusted PID 1, zero in the exec child."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    libc = ctypes.CDLL(None, use_errno=True)
    for cap in range(64):
        if libc.prctl(24, cap, 0, 0, 0) and ctypes.get_errno() != 22:
            raise OSError(ctypes.get_errno(), 'protected bounding drop')
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]
    class Data(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32), ('inheritable', ctypes.c_uint32)]
    header, data = Header(0x20080522, 0), (Data * 2)()
    data[0].effective = data[0].permitted = (1 << 5) | (1 << 6) | (1 << 7)
    if libc.capset(ctypes.byref(header), ctypes.byref(data)):
        raise OSError(ctypes.get_errno(), 'protected init caps')
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(4, 0, 0, 0, 0):
        raise OSError(ctypes.get_errno(), 'protected init hardening')


def main():
    if os.getpid() != 1:
        raise RuntimeError('protected init requires an owned PID namespace')
    spec = importlib.util.spec_from_file_location('protected_scope_init', '/authority/code/scope_init.py')
    scope_init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scope_init)
    subprocess.run(['/bin/mount', '--make-rprivate', '/'], stdin=subprocess.DEVNULL, check=True, timeout=5)
    subprocess.run(['/bin/mount', '-t', 'tmpfs', '-o', 'mode=000,nosuid,nodev,noexec', 'tmpfs', '/authority'], stdin=subprocess.DEVNULL, check=True, timeout=5)
    scope_init.harden = protect_init
    scope_init.BASE_ENV.update(LD_LIBRARY_PATH='/opt/runtime/lib')
    scope_init.workspace = '/'
    scope_init.payload_options = {'preexec_fn': payload_identity}
    return scope_init.main()


if __name__ == '__main__':
    raise SystemExit(main())
