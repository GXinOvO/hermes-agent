import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat


CATALOG_RECEIPT = Path('/local_data/huguangyao/workspace/report-automation/_catalog/generation.json')


def read_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('catalog_non_regular_artifact:' + str(path))
        return stream.read()


def raw_manifest(blobs):
    return {path: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
            for path, data in sorted(blobs.items())}


def manifest_sha256(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _archive_paths(root):
    result = []
    for week in sorted(root.iterdir()):
        if (re.fullmatch(r'\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}', week.name)
                and week.is_dir() and not week.is_symlink()):
            result.extend(path for path in sorted(week.rglob('*.html')) if path.is_file() and not path.is_symlink())
    return result


def snapshot_catalog_inputs(root):
    root = Path(root)
    paths = _archive_paths(root)
    appendices = {str(path): base64.b64encode(read_regular(path)).decode('ascii') for path in paths
                  if path.relative_to(root).parts[1] == '附页'}
    if paths != _archive_paths(root):
        raise ValueError('catalog_inputs_changed')
    return {'paths': [str(path) for path in paths], 'appendices': appendices}


def input_manifest(inputs):
    return {'paths': inputs['paths'], 'appendices': raw_manifest({
        path: base64.b64decode(data, validate=True) for path, data in inputs['appendices'].items()})}


def verify_inputs(root, expected):
    if expected != input_manifest(snapshot_catalog_inputs(root)):
        raise ValueError('catalog_inputs_changed')


def run_catalog(path, payload, dependencies, allow_outputs):
    root = Path(payload['root'])
    receipt = Path(payload['receipt'])
    inputs = payload['catalog_inputs']
    original_resources = dict(dependencies)
    dependencies.update({path: base64.b64decode(data, validate=True)
                         for path, data in inputs['appendices'].items()})
    allow_outputs(root / 'index.html', receipt, *[path for path in inputs['paths'] if path not in inputs['appendices']])
    spec = importlib.util.spec_from_file_location('memory_audit_catalog', path)
    if spec is None or spec.loader is None:
        raise ValueError('catalog_dependency_not_bound')
    catalog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(catalog)
    model = catalog.collect_reports(root)
    verify_inputs(root, input_manifest(inputs))
    catalog.__dict__['collect_reports'] = lambda directory: model
    saved = catalog.build(root, receipt)
    index = root / 'index.html'
    if saved['output_html'] != str(index) or saved['html_sha256'] != hashlib.sha256(read_regular(index)).hexdigest():
        raise ValueError('catalog_output_readback_mismatch')
    verify_inputs(root, input_manifest(inputs))
    return catalog_readback(root, receipt, payload['target'], raw_manifest(original_resources), input_manifest(inputs))


def catalog_readback(root, receipt_path, target, resources, inputs=None):
    root, receipt_path, target = Path(root), Path(receipt_path), Path(target)
    index = root / 'index.html'
    index_bytes = read_regular(index)
    receipt_bytes = read_regular(receipt_path)
    generation = json.loads(receipt_bytes)
    index_digest = hashlib.sha256(index_bytes).hexdigest()
    if generation['output_html'] != str(index) or generation['html_sha256'] != index_digest:
        raise ValueError('catalog_output_readback_mismatch')
    for path, digest in generation['sources'].items():
        if path not in resources or resources[path]['sha256'] != digest:
            raise ValueError('catalog_source_readback_mismatch:' + path)
    for font in generation.get('font', {}).get('files', []):
        matches = [record for path, record in resources.items() if path.endswith('/' + font['path'])]
        if len(matches) != 1 or matches[0]['sha256'] != font['sha256']:
            raise ValueError('catalog_font_readback_mismatch')
    if not isinstance(inputs, dict) or set(inputs) != {'paths', 'appendices'}:
        raise ValueError('catalog_inputs_changed')
    verify_inputs(root, inputs)
    return {
        'catalog_index': {'path': str(index), 'sha256': index_digest, 'readback_verified': True},
        'catalog_generation': {'path': str(receipt_path), 'sha256': hashlib.sha256(receipt_bytes).hexdigest(),
                               'readback_verified': True},
        'catalog_target_mapping': {'kind': 'memory', 'absolute_path': str(target),
            'href': target.relative_to(root).as_posix(), 'html_sha256': hashlib.sha256(read_regular(target)).hexdigest(),
            'bound_by_publisher': True},
        'resource_manifest': resources, 'resource_manifest_sha256': manifest_sha256(resources),
        'resource_readback_verified': True,
        'catalog_inputs': inputs,
    }
