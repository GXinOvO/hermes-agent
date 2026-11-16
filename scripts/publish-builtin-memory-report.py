#!/usr/bin/env python3
import argparse
import base64
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

INTERNAL = Path('/local_data/huguangyao/.hermes/reports/builtin-memory-audit-trial-20261110')
REPORTS = Path('/local_data/huguangyao/workspace/weekly-reports')
RENDERER = Path('/local_data/huguangyao/.hermes/skills/deep-tech-doc/md2html.py')
CATALOG = Path('/local_data/huguangyao/workspace/report-automation/render_catalog.py')
CATALOG_RECEIPT = CATALOG.parent / '_catalog/generation.json'
LIBRARY_URL = 'http://127.0.0.1:4173'


def _decode_process_output(value):
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value


def publish(source, run_time, source_bytes=None):
    interpreter_fd = globals().get('__memory_audit_interpreter_fd__')
    dependencies = globals().get('__memory_audit_dependencies__')
    bootstrap = globals().get('__memory_audit_catalog_bootstrap__')
    artifacts = globals().get('__memory_audit_artifacts__')
    helper_path = globals().get('__memory_audit_artifact_path__')
    catalog_path = os.path.abspath(CATALOG)
    if (not isinstance(dependencies, dict) or catalog_path not in dependencies
            or type(interpreter_fd) is not int or interpreter_fd < 0
            or not isinstance(bootstrap, str) or not bootstrap or artifacts is None
            or helper_path not in dependencies):
        raise ValueError('catalog_dependency_not_bound')
    allow_outputs = globals().get('__memory_audit_allow_outputs__')
    if callable(allow_outputs):
        allow_outputs(source, CATALOG_RECEIPT, REPORTS / 'index.html')
    source = Path(source).resolve()
    if source.parent != INTERNAL or source.suffix != '.md' or not source.is_file():
        raise ValueError('report_source_outside_authorized_directory')
    stamp = datetime.fromisoformat(run_time).astimezone(ZoneInfo('Asia/Shanghai'))
    day = stamp.date()
    start = day - timedelta(days=(day.weekday() - 5) % 7)
    end = start + timedelta(days=6)
    destination = REPORTS / f'{start}_{end}' / f'{day}' / f'记忆维护-{stamp:%Y-%m-%d-%H%M%S}.html'
    receipt_path = INTERNAL / f'publication-{stamp:%Y%m%d-%H%M%S}.json'
    if callable(allow_outputs):
        allow_outputs(destination, receipt_path)
    if source_bytes is None:
        source_bytes = source.read_bytes()
    front = source_bytes.decode('utf-8')
    spec = importlib.util.spec_from_file_location('builtin_memory_report_renderer', RENDERER)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    meta, body = renderer.split_front_matter(front)
    if not meta.get('title'):
        meta['title'] = f'记忆维护报告 · {stamp:%Y-%m-%d %H:%M}'
    meta.setdefault('logo', '记忆维护')
    meta.setdefault('subtitle', 'default Profile · MEMORY.md 与 USER.md')
    rendered, toc = renderer.render_sections(body.splitlines())
    document = renderer.build_page(meta, rendered, toc)
    if not toc or 'MEMORY.md' not in document or 'USER.md' not in document:
        raise ValueError('report_missing_required_content')
    if re.search(r'<(?:script|link|img|iframe)[^>]+(?:src|href)=[\"\x27]https?://', document, re.IGNORECASE):
        raise ValueError('report_has_remote_resource_dependency')
    digest = hashlib.sha256(document.encode()).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_text(encoding='utf-8-sig') != document:
            raise FileExistsError('refusing_to_replace_an_existing_different_report')
    else:
        fd, temporary_name = tempfile.mkstemp(prefix='memory-report-', suffix='.html', dir=INTERNAL)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise RuntimeError('published_report_readback_mismatch')
    inputs = artifacts.snapshot_catalog_inputs(REPORTS)
    resources = artifacts.raw_manifest(dependencies)
    payload = json.dumps({'catalog': base64.b64encode(dependencies[catalog_path]).decode('ascii'),
                          'artifact_helper': helper_path, 'root': str(REPORTS), 'receipt': str(CATALOG_RECEIPT),
                          'target': str(destination),
                          'catalog_inputs': inputs,
                          'dependencies': {path: base64.b64encode(data).decode('ascii')
                                           for path, data in dependencies.items()}}).encode('utf-8')
    catalog = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', bootstrap, catalog_path], input=payload,
                             capture_output=True, check=False, timeout=180,
                             pass_fds=(interpreter_fd,))
    catalog_stderr = _decode_process_output(catalog.stderr)
    receipt = {'html': str(destination), 'html_sha256': digest,
               'creation_baseline': globals().get('__memory_audit_creation_baseline__'),
               'source': str(source), 'source_sha256': hashlib.sha256(source_bytes).hexdigest(),
               'run_time': stamp.isoformat(), 'section_count': len(toc),
               'catalog_exit_code': catalog.returncode, 'report_readback_verified': True}
    receipt['library_indexed'] = False
    if catalog.returncode == 0:
        try:
            actual_catalog = artifacts.catalog_readback(REPORTS, CATALOG_RECEIPT, destination,
                                                        resources, artifacts.input_manifest(inputs))
            if json.loads(_decode_process_output(catalog.stdout)) != actual_catalog:
                raise ValueError('catalog_generation_not_confirmed')
            receipt.update(actual_catalog)
        except (OSError, ValueError, KeyError, TypeError) as error:
            receipt['catalog_error'] = str(error)
    if catalog.returncode == 0 and 'catalog_error' not in receipt:
        try:
            request = Request(LIBRARY_URL + '/api/rescan', data=b'{}', method='POST',
                              headers={'Content-Type': 'application/json', 'Origin': LIBRARY_URL})
            with urlopen(request, timeout=120) as response:
                library = json.load(response)
            sources = [s for s in library['sources']
                       if s['kind'] == 'directory' and Path(s['path']).resolve() == REPORTS.resolve()]
            if len(sources) != 1:
                raise RuntimeError('weekly_reports_source_not_unique')
            source_id = sources[0]['id']
            relative = destination.relative_to(REPORTS).as_posix()
            documents = [d for d in library['documents']
                         if d['sourceId'] == source_id and d['relativePath'] == relative]
            if len(documents) != 1:
                raise RuntimeError('published_report_not_uniquely_indexed')
            document = documents[0]
            preview_url = LIBRARY_URL + '/preview/' + quote(document['id'], safe='') + '/' + quote(document['filename'], safe='')
            with urlopen(preview_url, timeout=30) as response:
                preview_hash = hashlib.sha256(response.read()).hexdigest()
            if preview_hash != digest:
                raise RuntimeError('library_preview_content_mismatch')
            receipt['library_indexed'] = True
            receipt['library_document_id'] = document['id']
            receipt['library_preview_sha256'] = preview_hash
            receipt['library_scan_warnings'] = library.get('scanWarnings', [])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            receipt['library_error'] = str(error)
    else:
        receipt['catalog_error_tail'] = catalog_stderr[-1200:]
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    receipt_path.chmod(0o600)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if catalog.returncode == 0 and receipt['library_indexed'] else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--run-time', required=True)
    args = parser.parse_args()
    raise SystemExit(publish(args.source, args.run_time, globals().get('__memory_audit_source_bytes__')))
