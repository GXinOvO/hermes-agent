"""Local preflight and ACK checks for audited Weixin delivery methods.

A batch is not atomic: earlier acknowledged items remain delivered when a later
item fails. Never replay an item whose acknowledgement is unknown.
"""
from pathlib import Path

from gateway.strict_transport import require_strict_transport, strict_transport


def post_options():
    # aiohttp does not retry POST on a lost connection, but follows 307/308 by
    # default. A redirect would submit the same delivery body a second time.
    return {"allow_redirects": False} if strict_transport() else {}


def validate_ack(response):
    if not strict_transport():
        return
    if not isinstance(response, dict):
        raise RuntimeError("iLink ACK unconfirmed: expected an object")
    codes = [response[key] for key in ("ret", "errcode") if key in response]
    if not codes or any(type(code) is not int for code in codes):
        raise RuntimeError("iLink ACK unconfirmed: missing or invalid result code")


def preflight_file(path):
    candidate = Path(path)
    if not candidate.is_file():
        raise ValueError(f"strict media preflight: not a file: {path}")
    with candidate.open("rb") as stream:
        stream.read(1)


def preflight_media(adapter, plan):
    for path, method in plan:
        require_strict_transport(adapter, method)
        if method in ("send_image", "send_image_file"):
            require_strict_transport(adapter, "send_document")
        preflight_file(path)
