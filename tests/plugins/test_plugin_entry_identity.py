import hashlib
import importlib.util
import os
from pathlib import Path
import py_compile

from hermes_cli.plugins import PluginManager, PluginManifest


def test_directory_reload_uses_current_source_when_bytecode_is_stale(tmp_path):
    plugin = tmp_path / "identity"
    plugin.mkdir()
    target = plugin / "__init__.py"
    old, new = b"LABEL = 'old'\n", b"LABEL = 'new'\n"
    target.write_bytes(old)
    stamp = target.stat()
    py_compile.compile(str(target), doraise=True)
    target.write_bytes(new)
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    manager = PluginManager()
    manifest = PluginManifest(name="identity", version="0.1", description="", path=plugin, source="user")
    loaded = manager._load_directory_module(manifest)
    assert loaded.LABEL == "new"
    assert loaded.__dict__["__hermes_entry_sha256__"] == hashlib.sha256(new).hexdigest()
