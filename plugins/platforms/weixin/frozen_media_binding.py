import base64
from pathlib import Path
import re
from urllib.parse import urlsplit

from plugins.platforms import frozen_delivery as frozen


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _media(entries):
    if not isinstance(entries, list) or not entries:
        raise ValueError('Weixin media binding missing attachments')
    for entry in entries:
        if (not isinstance(entry, list) or len(entry) != 4 or not isinstance(entry[0], str) or not entry[0]
                or type(entry[1]) is not bool or not _hash(entry[2]) or entry[3] != Path(entry[0]).name):
            raise ValueError('Weixin media binding invalid attachment')


def _request(request, identity):
    from .frozen_media import _origin
    if (not isinstance(request, dict) or request.get('method') != 'POST'
            or not isinstance(request.get('headers'), dict)
            or request.get('redirects') is not False or type(request.get('retries')) is not int or request['retries'] != 0
            or request.get('auth_replay') is not False or request.get('proxy') is not None):
        raise ValueError('Weixin media binding invalid request policy')
    if request.get('url_response_slot') == 'upload_url':
        if (set(request) != {'method', 'url_response_slot', 'body_sha256', 'bytes', 'headers', 'timeout',
                            'response_slot', 'ack', 'redirects', 'retries', 'auth_replay', 'proxy'}
                or not _hash(request['body_sha256']) or type(request['bytes']) is not int or request['bytes'] <= 0
                or request['headers'] != {'Content-Type': 'application/octet-stream'} or request['timeout'] != 120
                or request['response_slot'] != 'encrypted_query_param' or request['ack'] != 'weixin-cdn-reference'):
            raise ValueError('Weixin media binding invalid CDN request')
        return
    expected = {'url', 'method', 'headers', 'body_b64', 'wire_body_sha256', 'response_slot', 'ack',
                'account', 'timeout', 'redirects', 'retries', 'auth_replay', 'proxy'}
    if (set(request) != expected or not isinstance(request['body_b64'], str) or not _hash(request['wire_body_sha256'])
            or request['account'] != identity['account'] or type(request['timeout']) not in (int, float)
            or not 0 < request['timeout'] <= 120):
        raise ValueError('Weixin media binding invalid API request')
    base64.b64decode(request['body_b64'], validate=True)
    if request['ack'] == 'weixin-upload-allocation':
        slot = request['response_slot']
        parsed = urlsplit(identity['cdn_base'])
        if (request['url'] != identity['api_base'] + '/ilink/bot/getuploadurl'
                or not isinstance(slot, dict) or set(slot) != {'name', 'origin', 'path', 'filekey_sha256', 'loopback_direct_path'}
                or slot['name'] != 'upload_url' or slot['origin'] != _origin(identity['cdn_base'])
                or slot['path'] != parsed.path + '/upload' or not _hash(slot['filekey_sha256'])
                or slot['loopback_direct_path'] != ('/cdn-upload' if not parsed.path else None)):
            raise ValueError('Weixin media binding invalid allocation dependency')
    elif (request['ack'] != 'weixin-message' or request['url'] != identity['api_base'] + '/ilink/bot/sendmessage'
            or request['response_slot'] not in (None, frozen.UPLOAD_REF)):
        raise ValueError('Weixin media binding invalid publication')


def validate(binding, route):
    from .frozen_media import _base_url
    required = {'version', 'platform', 'actualdestination', 'parent', 'thread', 'account', 'scope',
                'media', 'identity', 'crypto_context', 'prepared', 'transport_sha256'}
    if (set(binding) != required or type(binding['version']) is not int or binding['version'] != 2
            or binding['platform'] != 'weixin' or route.get('platform') != 'weixin'
            or binding['actualdestination'] != str(route.get('chat_id')) or binding['parent'] != []
            or binding['thread'] != route.get('thread_id') or not isinstance(binding['scope'], str)
            or not binding['scope'] or not isinstance(binding['account'], str) or not binding['account']):
        raise ValueError('Weixin media binding invalid target')
    body = {k: v for k, v in binding.items() if k != 'transport_sha256'}
    if binding['transport_sha256'] != frozen.digest(frozen.encode(body)):
        raise ValueError('Weixin media binding fingerprint changed')
    _media(binding['media'])
    identity = binding['identity']
    fields = {'home', 'scope', 'execution', 'destination', 'target', 'thread', 'account', 'api_base', 'cdn_base',
              'credential_sha256', 'context_sha256', 'calls_sha256', 'media'}
    if (not isinstance(identity, dict) or set(identity) != fields or identity['scope'] != binding['scope']
            or identity['home'] != binding['scope'] or not Path(identity['home']).is_absolute()
            or not isinstance(identity['execution'], str) or not identity['execution']
            or identity['destination'] != binding['actualdestination'] or identity['target'] != route
            or identity['thread'] != binding['thread'] or identity['account'] != binding['account']
            or identity['media'] != binding['media']
            or any(not _hash(identity[key]) for key in ('credential_sha256', 'context_sha256', 'calls_sha256'))):
        raise ValueError('Weixin media binding invalid identity')
    for key in ('api_base', 'cdn_base'):
        if _base_url(identity[key]) != identity[key]:
            raise ValueError('Weixin media binding noncanonical origin')
    envelope = binding['crypto_context']
    if (not isinstance(envelope, dict) or set(envelope) != {'version', 'algorithm', 'nonce', 'ciphertext'}
            or type(envelope['version']) is not int or envelope['version'] != 1
            or envelope['algorithm'] != 'HKDF-SHA256/AES-256-GCM'
            or not isinstance(envelope['nonce'], str) or not isinstance(envelope['ciphertext'], str)
            or len(base64.b64decode(envelope['nonce'], validate=True)) != 12
            or not 16 < len(base64.b64decode(envelope['ciphertext'], validate=True)) <= 128 * len(binding['media']) + 1024):
        raise ValueError('Weixin media binding invalid crypto envelope')
    if not isinstance(binding['prepared'], list) or not binding['prepared']:
        raise ValueError('Weixin media binding missing requests')
    for request in binding['prepared']:
        _request(request, identity)
