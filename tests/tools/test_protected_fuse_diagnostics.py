import logging
from pathlib import Path
import subprocess
import sys

from tools.file_protected_fuse import ProtectedFuseEnvironment


def test_probe_preserves_unavailable_and_logs_traceback(monkeypatch, caplog):
    environment = object.__new__(ProtectedFuseEnvironment)

    def unavailable(path):
        raise RuntimeError('diagnostic probe control')

    monkeypatch.setattr(environment, 'target', unavailable)
    with caplog.at_level(logging.ERROR):
        result = environment.probe('/cas/diagnostic.txt')
    assert result.state == 'unavailable'
    assert 'diagnostic probe control' in result.error
    assert any(record.exc_info and 'diagnostic probe control' in str(record.exc_info[1])
               for record in caplog.records)


def test_helper_keeps_failure_exit_and_traceback():
    import tools.file_protected_fuse as subject

    command = [sys.executable, '-I', '-B', str(Path(subject.__file__).resolve()),
               'snapshot', '/unsupported/diagnostic.txt']
    result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 1
    assert not result.stdout
    assert 'ValueError: unsupported protected FUSE path' in result.stderr
    assert 'Traceback (most recent call last)' in result.stderr
