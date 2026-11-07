"""Feishu SDK request/auth/response models with a single HTTP submission.

The SDK's default requests transport follows redirects. A sealed final must
never replay a POST through that implicit path. Keep this transport per call.
"""
from urllib.parse import quote


def submit(method, request):
    import requests
    from lark_oapi.api.im.v1 import CreateFileResponse, CreateImageResponse, CreateMessageResponse, ReplyMessageResponse
    from lark_oapi.core import JSON
    from lark_oapi.core.http.transport import _build_header
    from lark_oapi.core.model import RequestOption, RawResponse
    from lark_oapi.core.token import verify
    from lark_oapi.core.utils import Files
    from requests_toolbelt import MultipartEncoder
    responses = {
        'lark_oapi.api.im.v1.resource.message.Message.create': CreateMessageResponse,
        'lark_oapi.api.im.v1.resource.message.Message.reply': ReplyMessageResponse,
        'lark_oapi.api.im.v1.resource.image.Image.create': CreateImageResponse,
        'lark_oapi.api.im.v1.resource.file.File.create': CreateFileResponse,
    }
    identity = method.__module__ + '.' + method.__qualname__
    if identity not in responses:
        raise RuntimeError('strict_transport_unverified: Feishu SDK ' + identity)
    config = method.__self__.config
    option = RequestOption()
    verify(config, request, option)
    body = request.body
    if identity.endswith(('Image.create', 'File.create')):
        body = MultipartEncoder(Files.parse_form_data(body))
        option.headers['Content-Type'] = body.content_type
    else:
        body = JSON.marshal(body).encode('utf-8')
        option.headers['Content-Type'] = 'application/json; charset=utf-8'
    _build_header(request, option, config)
    uri = request.uri
    for key, value in (request.paths or {}).items():
        uri = uri.replace(':' + key, quote(str(value or ''), safe=''))
    response = requests.request(request.http_method.name, config.domain + uri, headers=request.headers,
                                params=request.queries, data=body, timeout=config.timeout or 30, allow_redirects=False)
    response.raise_for_status()
    if 300 <= response.status_code < 400:
        raise RuntimeError('Strict Feishu submission refused redirect')
    raw = RawResponse()
    raw.status_code, raw.headers, raw.content = response.status_code, dict(response.headers), response.content
    result = JSON.unmarshal(response.text, responses[identity])
    result.raw = raw
    return result
