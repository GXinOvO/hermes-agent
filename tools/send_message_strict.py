"""Verified standalone transport for a caller that already owns its durable claim."""
from pathlib import Path
from dataclasses import dataclass, field
from threading import Lock
from uuid import uuid4

TELEGRAM_API = 'https://api.telegram.org'


@dataclass(frozen=True)
class PlanBinding:
    home: str
    platform: str
    chat_id: str
    message: str
    media: tuple
    options: tuple
    execution_id: object
    claim_id: str


class _Consumption:
    def __init__(self, payload):
        self.lock = Lock()
        self.payload = payload

    def take(self):
        with self.lock:
            payload, self.payload = self.payload, None
            return payload


@dataclass(frozen=True)
class PreparedPlan:
    """One immutable binding and a private atomic, payload-releasing consume slot."""
    binding: PlanBinding
    _consumption: _Consumption = field(repr=False, compare=False)


def _binding(platform, chat_id, message, media_files, kwargs, execution_id, claim_id):
    from hermes_constants import get_hermes_home
    thread = kwargs.get('thread_id')
    return PlanBinding(str(get_hermes_home().resolve()), platform, str(chat_id), message,
        tuple((str(path), bool(voice)) for path, voice in media_files or ()),
        (str(thread) if thread else None, bool(kwargs.get('force_document')), kwargs.get('caption'), kwargs.get('notify')),
        execution_id, claim_id)


@dataclass(frozen=True)
class MediaFile:
    path: str
    voice: bool
    data: bytes = field(repr=False)
    filename: str
    kind: str = 'document'
    content_type: str = 'application/octet-stream'


def prepared_media_identity(plan):
    """Hash the actual frozen send bytes and route metadata, without reopening paths."""
    import hashlib
    with plan._consumption.lock:
        payload = plan._consumption.payload
        if payload is None:
            raise RuntimeError('strict standalone plan already attempted')
        return [[file.path, file.voice, hashlib.sha256(file.data).hexdigest(), file.filename]
                for file in payload.files]


def media_bytes(media_files):
    """Freeze all bytes before the first text/upload submission, including later files."""
    import mimetypes
    entries = []
    for path, voice in media_files or ():
        try:
            name = Path(path).name
            entries.append(MediaFile(str(path), bool(voice), Path(path).read_bytes(), name,
                content_type=mimetypes.guess_type(name)[0] or 'application/octet-stream'))
        except OSError as exc:
            raise RuntimeError('strict standalone media unavailable') from exc
    return tuple(entries)


def prepare(platform, pconfig, chat_id, message, *, media_files=None, execution_id=None, claim_id=None, **kwargs):
    name = getattr(platform, 'value', platform)
    if name in ('slack', 'discord', 'telegram'):
        import importlib
        ticket = uuid4().hex if claim_id is None else claim_id
        if not isinstance(ticket, str) or not ticket or (execution_id is not None and (not isinstance(execution_id, str) or not execution_id)):
            raise RuntimeError('strict standalone execution/claim binding unavailable')
        binding = _binding(name, chat_id, message, media_files, kwargs, execution_id, ticket)
        media_files = binding.media
        module = importlib.import_module('tools.send_message_strict_' + name)
        from tools.send_message_senders import _media_caption_split
        caption, text = (None, message) if name == 'telegram' else _media_caption_split(message, media_files,
            max_caption_len=2000 if name == 'discord' else 39000)
        options = dict(kwargs)
        if options.get('thread_id'):
            options['thread_id'] = str(options['thread_id'])
        if caption is not None:
            options['caption'] = caption
        plan = module.prepare(pconfig, chat_id, text, media_files=media_files, **options)
        return PreparedPlan(binding, _Consumption(plan))
    raise RuntimeError('strict standalone frozen transport unavailable: ' + name)


async def send(platform, pconfig, chat_id, message, *, media_files=None, thread_id=None,
               force_document=False, prepared_plan=None, execution_id=None, claim_id=None, **kwargs):
    name = getattr(platform, 'value', platform)
    if name in ('slack', 'discord', 'telegram'):
        import importlib
        if not isinstance(prepared_plan, PreparedPlan):
            return {'error': 'strict standalone prepared plan unavailable'}
        binding = prepared_plan.binding
        options = dict(kwargs, thread_id=thread_id, force_document=force_document)
        if binding.execution_id is not None and claim_id is None:
            return {'error': 'strict standalone claim binding unavailable'}
        expected = _binding(name, chat_id, message, media_files, options,
                            execution_id, binding.claim_id if claim_id is None else claim_id)
        if binding != expected:
            return {'error': 'strict standalone plan changed after prepare'}
        plan = prepared_plan._consumption.take()
        if plan is None:
            return {'error': 'strict standalone plan already attempted', 'ambiguous': True}
        module = importlib.import_module('tools.send_message_strict_' + name)
        return await module.send(plan)
    return {'error': 'strict standalone frozen transport unavailable: ' + name}
