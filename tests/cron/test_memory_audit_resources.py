import hashlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cron import memory_audit_policy
from tests.cron.test_memory_audit_tool_scope import bind_report_store


@pytest.mark.parametrize('state', ['unchanged', 'replaced', 'omitted'])
def test_static_resource_is_consumed_from_verified_snapshot(tmp_path, monkeypatch, state):
    css = tmp_path / 'assets' / 'theme.css'
    css.parent.mkdir()
    css.write_text('approved-style')
    renderer = tmp_path / 'assets' / 'renderer.py'
    renderer.write_text('from pathlib import Path\n'
                        f'def render(): return Path({str(css)!r}).read_text()\n')
    marker = tmp_path / 'resource-output'
    publisher = tmp_path / 'publisher.py'
    publisher.write_text('import importlib.util\nfrom pathlib import Path\n'
                         f'spec=importlib.util.spec_from_file_location("renderer", {str(renderer)!r})\n'
                         'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
                         f'Path({str(marker)!r}).write_text(module.render())\nprint("{{}}")\n')
    source = tmp_path / 'report.md'
    source.write_text('MEMORY.md USER.md')
    policy = memory_audit_policy.RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.run_time = '2026-11-12T16:00:00+08:00'
    policy.publisher_dependencies = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (renderer, css)}
    if state == 'omitted':
        del policy.publisher_dependencies[str(css)]
    real_popen = subprocess.Popen
    replacements = []

    def launch(argv, **kwargs):
        if state == 'replaced' and str(publisher) in argv:
            css.write_text('untrusted-style')
            replacements.append(True)
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', launch)
    result = json.loads(policy.run_report_action('publish_report', {}))
    if state == 'omitted':
        assert result.get('exit_code') != 0, result
        assert 'memory_audit_unpinned_resource' in result.get('stderr', ''), result
        assert not marker.exists()
    else:
        assert result.get('exit_code') == 0, result
        assert marker.read_text() == 'approved-style'
        assert replacements == ([True] if state == 'replaced' else [])


@pytest.mark.parametrize('missing_resource', [False, 'style', 'font'])
def test_real_catalog_and_fonts_use_snapshot_bytes_without_external_writes(tmp_path, monkeypatch, missing_resource):
    root = Path(__file__).resolve().parents[2]
    internal = tmp_path / 'internal'
    reports = tmp_path / 'reports'
    internal.mkdir()
    reports.mkdir()
    generation = tmp_path / 'catalog/generation.json'
    source = internal / 'maintenance.md'
    source.write_text('# Result\n\n## Verification\nMEMORY.md USER.md unchanged.\n')
    stamp = '2026-11-12T16:00:00+08:00'
    target = reports / '2026-11-07_2026-11-13/2026-11-12/记忆维护-2026-11-12-160000.html'
    producer = root / 'scripts/publish-builtin-memory-report.py'
    artifact_helper = root / 'cron/memory_audit_artifacts.py'

    class Library(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({'sources': [{'kind': 'directory', 'path': str(reports), 'id': 'fixture'}],
                'documents': [{'sourceId': 'fixture', 'relativePath': target.relative_to(reports).as_posix(),
                               'id': 'document', 'filename': target.name}]}).encode())

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(target.read_bytes())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Library)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    publisher = tmp_path / 'wrapper.py'
    publisher.write_text(
        'import importlib.util\nfrom pathlib import Path\n'
        f'spec=importlib.util.spec_from_file_location("actual_publisher", {str(producer)!r})\n'
        'module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n'
        'for name in ("__memory_audit_dependencies__","__memory_audit_interpreter_fd__",'
        '"__memory_audit_catalog_bootstrap__","__memory_audit_artifacts__",'
        '"__memory_audit_artifact_path__","__memory_audit_allow_outputs__"):\n'
        '    setattr(module,name,globals()[name])\n'
        f'module.INTERNAL=Path({str(internal)!r});module.REPORTS=Path({str(reports)!r})\n'
        f'module.CATALOG_RECEIPT=Path({str(generation)!r})\n'
        f'module.LIBRARY_URL="http://127.0.0.1:{server.server_port}"\n'
        f'raise SystemExit(module.publish({str(source)!r},{stamp!r},__memory_audit_source_bytes__))\n')
    policy = memory_audit_policy.RunPolicy('partial', {}, None)
    bind_report_store(policy, tmp_path, monkeypatch)
    policy.publisher = publisher
    policy.publisher_sha256 = hashlib.sha256(publisher.read_bytes()).hexdigest()
    policy.report_source = source
    policy.report_source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    policy.run_time = stamp
    policy.publisher_dependencies = dict(memory_audit_policy.PUBLISHER_DEPENDENCY_SHA256)
    policy.publisher_dependencies[str(artifact_helper)] = hashlib.sha256(artifact_helper.read_bytes()).hexdigest()
    policy.publisher_dependencies[str(producer)] = hashlib.sha256(producer.read_bytes()).hexdigest()
    omitted = '/local_data/huguangyao/.hermes/skills/deep-tech-doc/pmos-report.css'
    generation_before = None
    if missing_resource == 'font':
        positive = json.loads(policy.run_report_action('publish_report', {}))
        assert positive.get('exit_code') == 0, positive
        generation_before = generation.read_bytes()
        selected_font = json.loads(generation_before)['font']['files'][0]['path']
        omitted = str(Path('/local_data/huguangyao/workspace/news-briefings/_automation/assets') / selected_font)
    if missing_resource:
        del policy.publisher_dependencies[omitted]
    original_hashes = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                       for path in policy.publisher_dependencies}
    try:
        result = json.loads(policy.run_report_action('publish_report', {}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in original_hashes} == original_hashes
    if missing_resource:
        assert result.get('exit_code') != 0, result
        error = 'memory_audit_unpinned_font:' if missing_resource == 'font' else 'memory_audit_unpinned_resource:'
        if missing_resource == 'font':
            failed_receipt = json.loads(result['output'])
            assert failed_receipt['library_indexed'] is False
            diagnostic = failed_receipt['catalog_error_tail']
        else:
            diagnostic = result.get('stderr', '')
        assert error + omitted in diagnostic, result
        assert policy.publication_receipt is None
        if generation_before is None:
            assert not generation.exists()
        else:
            assert generation.read_bytes() == generation_before
        return
    assert result.get('exit_code') == 0, result
    receipt = json.loads(result['output'])
    assert receipt['resource_readback_verified'] is True
    assert receipt['catalog_index']['sha256'] == hashlib.sha256((reports / 'index.html').read_bytes()).hexdigest()
    assert receipt['catalog_generation']['sha256'] == hashlib.sha256(generation.read_bytes()).hexdigest()
    catalog = json.loads(generation.read_bytes())
    assert catalog['font']['files']
    assert all(item['origin'] == 'assets' for item in catalog['font']['files'])
    assert receipt['library_indexed'] is True


def test_font_adapter_forces_offline_pinned_consumption():
    from types import SimpleNamespace

    namespace = {'_dependency_bytes': {'/fixture/assets/files/font.woff2': b'wOF2fixture'}}
    from unittest.mock import patch
    original_spec = __import__('importlib.util', fromlist=['spec_from_file_location']).spec_from_file_location
    with patch.object(Path, 'open', Path.open), patch.object(Path, 'is_file', Path.is_file), \
            patch.object(Path, 'resolve', Path.resolve), patch('importlib.util.spec_from_file_location', original_spec):
        exec(memory_audit_policy._FROZEN_DEPENDENCY_IMPORT, namespace)
        calls = []

        def prepare(document, **kwargs):
            calls.append(kwargs)
            return 'embedded', {'files': []}

        module = SimpleNamespace(ASSETS=Path('/fixture/assets'),
            _font_subsets=lambda document: ([('files/font.woff2', 'block')], {65}), prepare_fonts=prepare)
        namespace['_offline_fonts'](module)
        assert module.prepare_fonts('A', allow_download=True, allow_fetch=True, cache_dir='/foreign') == ('embedded', {'files': []})
        assert calls == [{'allow_download': False, 'allow_fetch': False,
                          'pins': {'files/font.woff2': hashlib.sha256(b'wOF2fixture').hexdigest()}}]
