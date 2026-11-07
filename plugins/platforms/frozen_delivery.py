"""Pure immutable request values shared by the native frozen delivery helpers.

No adapter, client, credentials object or open media handle survives preparation.
The caller persists only ``binding``; the private byte strings are process local.
"""
from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit


UPLOAD_REF = '__HERMES_FROZEN_UPLOAD_REFERENCE__'
PATH_KEYS = {'send_document': 'file_path', 'send_image_file': 'image_path',
             'send_voice': 'audio_path', 'send_video': 'video_path'}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def validate_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment):
        raise ValueError('frozen transport requires an explicit HTTP(S) URL without userinfo')
    return url


@dataclass(frozen=True)
class Request:
    url: str
    method: str
    headers: tuple
    body: bytes
    ack: str
    account: str = ''
    timeout: float = 30

    def public(self, secrets=()):
        body = self.body
        for secret in secrets:
            if secret:
                escaped = json.dumps(secret, ensure_ascii=False)[1:-1].encode('utf-8')
                body = body.replace(escaped, ('sha256:' + digest(secret.encode('utf-8'))).encode('ascii'))
        headers = {k: ('sha256:' + digest(v.encode('utf-8')) if k.lower() in
                   {'authorization', 'cookie', 'x-goog-user-project'} else v) for k, v in self.headers}
        return {'url': self.url, 'method': self.method, 'headers': headers,
                'body_b64': base64.b64encode(body).decode('ascii'), 'wire_body_sha256': digest(self.body),
                'response_slot': UPLOAD_REF if UPLOAD_REF.encode('ascii') in self.body else None,
                'ack': self.ack, 'account': self.account, 'timeout': self.timeout,
                'redirects': False, 'retries': 0, 'auth_replay': False, 'proxy': None}


class RuntimePlan:
    __slots__ = ('_binding', '_requests', '_used')

    def __init__(self, requests, binding):
        object.__setattr__(self, '_requests', tuple(requests))
        object.__setattr__(self, '_binding', encode(binding))
        object.__setattr__(self, '_used', False)

    def __setattr__(self, key, value):
        raise AttributeError('frozen runtime plan')

    def __contains__(self, key):
        return key in {'binding', 'media'}

    def __getitem__(self, key):
        if key == 'binding':
            return json.loads(self._binding)
        if key == 'media':
            return json.loads(self._binding)['media']
        raise KeyError(key)

    def consume(self):
        # No await between testing and setting: one event-loop owner, including
        # simultaneous async callers. Durable cross-process CAS belongs to Cron.
        if self._used:
            raise RuntimeError('frozen plan already attempted')
        object.__setattr__(self, '_used', True)
        return self._requests


def plan(platform, state, requests, media, *, account, parents=(), secrets=()):
    if not requests:
        raise ValueError('empty frozen delivery')
    prepared = [request.public(secrets) for request in requests]
    binding = {'version': 1, 'platform': platform, 'actualdestination': str(state.chat_id),
               'parent': list(parents), 'thread': getattr(state, 'thread_id', None), 'account': account,
               'scope': str(getattr(state, 'profile', '') or ''), 'prepared': prepared, 'media': media}
    binding['transport_sha256'] = digest(encode(binding))
    return RuntimePlan(requests, binding)


def snapshot_media(calls, retained):
    """Read each local attachment once; its fingerprint covers those same bytes.

    Compare a descriptor fingerprint with the actual frozen bytes as well as
    file identity. Later path mutations cannot affect the frozen bytes.
    """
    if UPLOAD_REF in encode(calls).decode('utf-8'):
        raise ValueError('reserved response marker in delivery input')
    needed = []
    for method, kwargs in calls:
        if method in PATH_KEYS:
            needed.append(str(kwargs[PATH_KEYS[method]]))
        elif method != 'send':
            raise ValueError('unverified frozen delivery method: ' + method)
    retained = [(str(path), voice) for path, voice in retained]
    if any(type(voice) is not bool for _, voice in retained):
        raise ValueError('retained voice intent must be boolean')
    if set(needed) != {path for path, _ in retained}:
        raise ValueError('retained media and prepared calls differ')
    loaded = {}
    for path in dict.fromkeys(needed):
        candidate = Path(path)
        data, after = _read_media(candidate)
        current = candidate.stat()
        if _file_identity(after) != _file_identity(current):
            raise ValueError('media changed during freeze')
        loaded[path] = data
    media = [[path, voice, digest(loaded[path]), Path(path).name] for path, voice in retained]
    return loaded, media


def _file_identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_media(candidate):
    with candidate.open('rb') as stream:
        before = os.fstat(stream.fileno())
        # Some filesystems coalesce sub-tick mtime/ctime changes. Fingerprint
        # through the descriptor before the materializing read as well.
        expected = hashlib.sha256()
        for chunk in iter(lambda: os.read(stream.fileno(), 1024 * 1024), b''):
            expected.update(chunk)
        os.lseek(stream.fileno(), 0, os.SEEK_SET)
        data = stream.read()
        after = os.fstat(stream.fileno())
    if _file_identity(before) != _file_identity(after) or expected.hexdigest() != digest(data):
        raise ValueError('media changed during freeze')
    return data, after


def request(url, body, headers, ack, *, account='', timeout=30, method='POST'):
    return Request(validate_url(url), method, tuple(sorted((str(k), str(v)) for k, v in headers.items())),
                   bytes(body), ack, account, timeout)


def json_request(url, payload, headers, ack, **kwargs):
    return request(url, encode(payload), dict(headers, **{'Content-Type': 'application/json; charset=utf-8'}), ack, **kwargs)


def multipart(fields, files):
    """Deterministic boundary makes a fresh-owner binding byte-identical."""
    import requests
    prepared = requests.Request('POST', 'http://localhost/', data=fields, files=files).prepare()
    body = prepared.body
    if not isinstance(body, bytes):
        raise ValueError('multipart preparation did not return bytes')
    marker = prepared.headers['Content-Type'].split('boundary=', 1)[1].encode('ascii')
    boundary = ('hermes-' + digest(body.replace(marker, b'BOUNDARY'))).encode('ascii')
    if any(boundary in data for _, (_, data, _) in files):
        raise ValueError('multipart boundary collides with media')
    return body.replace(marker, boundary), 'multipart/form-data; boundary=' + boundary.decode('ascii')


def require_id(value, label):
    if type(value) is not str or not value.strip():
        raise RuntimeError('frozen ACK missing ' + label)
    return value


def acknowledge(kind, payload):
    if not isinstance(payload, dict):
        raise RuntimeError('frozen ACK must be an object')
    if kind == 'mattermost-upload':
        infos = payload.get('file_infos')
        if not isinstance(infos, list) or len(infos) != 1 or not isinstance(infos[0], dict):
            raise RuntimeError('frozen ACK missing file_infos')
        return require_id(infos[0].get('id'), 'file id')
    if kind == 'mattermost-message':
        return require_id(payload.get('id'), 'post id')
    if kind.startswith('feishu-'):
        if type(payload.get('code')) is not int or payload['code'] != 0:
            raise RuntimeError('frozen Feishu ACK rejected')
        key = kind.split('-', 1)[1]
        data = payload.get('data')
        if not isinstance(data, dict):
            raise RuntimeError('frozen ACK missing Feishu data')
        return require_id(data.get(key), key)
    if kind == 'google-upload':
        reference = payload.get('attachmentDataRef')
        if not isinstance(reference, dict):
            raise RuntimeError('frozen ACK missing attachmentDataRef')
        require_id(reference.get('resourceName'), 'resourceName')
        return reference
    if kind == 'google-message':
        name = require_id(payload.get('name'), 'message name')
        if not name.startswith('spaces/') or '/messages/' not in name:
            raise RuntimeError('frozen ACK invalid Google message name')
        return name
    if kind == 'weixin-message':
        codes = [payload[k] for k in ('ret', 'errcode') if k in payload]
        if not codes or any(type(code) is not int or code != 0 for code in codes):
            raise RuntimeError('frozen iLink ACK unconfirmed')
        return None
    raise ValueError('unverified ACK type')


def resolve_upload(body, uploaded):
    if UPLOAD_REF.encode('ascii') not in body:
        return body
    if uploaded is None:
        raise RuntimeError('missing prior upload ACK')

    def replace(value):
        if value == UPLOAD_REF:
            return uploaded
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str) and UPLOAD_REF in value:
            # Lark content is itself JSON inside the outer request. Decode both
            # levels so a returned key cannot inject JSON through string replace.
            return json.dumps(replace(json.loads(value)), ensure_ascii=False)
        return value
    return encode(replace(json.loads(body)))
