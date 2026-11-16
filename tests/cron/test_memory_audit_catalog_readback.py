import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def publisher_run(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    path = root / 'scripts/publish-builtin-memory-report.py'
    spec = importlib.util.spec_from_file_location('catalog_readback_publisher', path)
    assert spec is not None and spec.loader is not None
    publisher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publisher)
    internal = tmp_path / 'internal'
    reports = tmp_path / 'reports'
    internal.mkdir()
    reports.mkdir()
    renderer = tmp_path / 'renderer.py'
    renderer.write_text('def split_front_matter(front): return ({}, front)\n'
                        'def render_sections(lines): return ("\\n".join(lines), ["result"])\n'
                        'def build_page(meta, body, toc): return "<html>"+body+"</html>"\n')
    catalog = tmp_path / 'catalog.py'
    catalog.write_text('print("catalog fixture")')
    generation = tmp_path / '_catalog/generation.json'
    source = internal / 'maintenance.md'
    source.write_text('# Report\n## Result\nMEMORY.md USER.md')
    stamp = '2026-11-12T16:00:00+08:00'
    target = reports / '2026-11-07_2026-11-13/2026-11-12/记忆维护-2026-11-12-160000.html'
    dependencies = {str(catalog): catalog.read_bytes()}
    from cron import memory_audit_artifacts as artifacts
    helper = str(Path(artifacts.__file__).resolve())
    dependencies[helper] = Path(helper).read_bytes()
    monkeypatch.setattr(publisher, 'INTERNAL', internal)
    monkeypatch.setattr(publisher, 'REPORTS', reports)
    monkeypatch.setattr(publisher, 'RENDERER', renderer)
    monkeypatch.setattr(publisher, 'CATALOG', catalog)
    monkeypatch.setattr(publisher, 'CATALOG_RECEIPT', generation, raising=False)
    monkeypatch.setattr(publisher, '__memory_audit_dependencies__', dependencies, raising=False)
    monkeypatch.setattr(publisher, '__memory_audit_interpreter_fd__', 0, raising=False)
    monkeypatch.setattr(publisher, '__memory_audit_catalog_bootstrap__', 'fixture', raising=False)
    monkeypatch.setattr(publisher, '__memory_audit_artifacts__', artifacts, raising=False)
    monkeypatch.setattr(publisher, '__memory_audit_artifact_path__', helper, raising=False)
    requests = []

    def library_response(request, timeout):
        requests.append(request)
        if isinstance(request, str):
            return io.BytesIO(target.read_bytes())
        return io.BytesIO(json.dumps({'sources': [{'kind': 'directory', 'path': str(reports), 'id': 'local'}],
            'documents': [{'sourceId': 'local', 'relativePath': target.relative_to(reports).as_posix(),
                           'id': 'fixture', 'filename': target.name}]}).encode())

    monkeypatch.setattr(publisher, 'urlopen', library_response)

    def run(state):
        def catalog_run(*args, **kwargs):
            index = reports / 'index.html'
            index.write_text('<html>catalog fixture</html>')
            generation.parent.mkdir(exist_ok=True)
            result = {'output_html': str(index), 'html_sha256': hashlib.sha256(index.read_bytes()).hexdigest(),
                      'reports': [], 'sources': {}}
            if state == 'wrong_hash':
                result['html_sha256'] = '0' * 64
            elif state == 'wrong_path':
                result['output_html'] = str(reports / 'wrong.html')
            if state != 'missing':
                generation.write_text(json.dumps(result))
            output = artifacts.catalog_readback(reports, generation, target,
                artifacts.raw_manifest(dependencies), artifacts.input_manifest(artifacts.snapshot_catalog_inputs(reports))) if state in ('valid', 'wrong_stdout') else {}
            if state == 'wrong_stdout':
                output['catalog_index']['sha256'] = '0' * 64
            return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps(output))

        monkeypatch.setattr(publisher.subprocess, 'run', catalog_run)
        return publisher.publish(str(source), stamp, source.read_bytes())

    return publisher, run, reports, generation, target, requests


@pytest.mark.parametrize('state', ['wrong_hash', 'wrong_path', 'missing', 'wrong_stdout'])
def test_zero_catalog_exit_cannot_hide_invalid_artifacts(publisher_run, capsys, state):
    _, run, _, _, _, requests = publisher_run
    assert run(state) == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['library_indexed'] is False
    assert 'catalog_error' in receipt
    assert requests == []


def test_publication_receipt_binds_catalog_outputs_and_target(publisher_run, capsys):
    _, run, reports, generation, target, _ = publisher_run
    assert run('valid') == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt['catalog_index'] == {'path': str(reports / 'index.html'),
        'sha256': hashlib.sha256((reports / 'index.html').read_bytes()).hexdigest(), 'readback_verified': True}
    assert receipt['catalog_generation']['path'] == str(generation)
    assert receipt['catalog_generation']['sha256'] == hashlib.sha256(generation.read_bytes()).hexdigest()
    assert receipt['catalog_target_mapping'] == {'kind': 'memory', 'absolute_path': str(target),
        'href': target.relative_to(reports).as_posix(), 'html_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'bound_by_publisher': True}


def test_catalog_readback_cannot_omit_input_binding(publisher_run, capsys):
    from cron import memory_audit_artifacts as artifacts

    _, run, reports, generation, target, _ = publisher_run
    assert run('valid') == 0
    receipt = json.loads(capsys.readouterr().out)
    with pytest.raises(ValueError, match='catalog_inputs_changed'):
        artifacts.catalog_readback(reports, generation, target, receipt['resource_manifest'], None)


@pytest.mark.parametrize('change', ['appendix', 'membership'])
def test_catalog_input_drift_is_rejected_before_build(tmp_path, change):
    from cron import memory_audit_artifacts as artifacts

    root = tmp_path / 'reports'
    appendix = root / '2026-11-07_2026-11-13/附页/input.html'
    appendix.parent.mkdir(parents=True)
    appendix.write_text('<title>Approved appendix</title>')
    inputs = artifacts.input_manifest(artifacts.snapshot_catalog_inputs(root))
    artifacts.verify_inputs(root, inputs)
    if change == 'appendix':
        appendix.write_text('<title>Replaced appendix</title>')
    else:
        appendix.with_name('added.html').write_text('<title>New appendix</title>')
    with pytest.raises(ValueError, match='catalog_inputs_changed'):
        artifacts.verify_inputs(root, inputs)
