"""Runtime-only final delivery authority, bound to one committed automation claim.

Neither message metadata, prompt text nor agent result fields may mint this capability.
The ledger serializes its binding only after checking the exact admission in its own DB.
Historical ledger rows without this binding are never promoted by guessing their source.
"""
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import threading
import asyncio

_current: ContextVar[object] = ContextVar('automation_final_delivery', default=None)
_SEAL = object()
STRICT = 'strict_automation_v1'
HUMAN = 'human_reply_v1'
_WITNESS_PREFIX = 'gateway.automation.execution_witness.v1.'
_BOT_REQUEST = object()


def mark_bot_request(event):
    """The Bot ingress writer's typed runtime request; never serialized from caller fields."""
    event._automation_input_kind = _BOT_REQUEST


def recovery_descriptor(event):
    # All background result consumers default to consume-only. Ordinary Bot ingress
    # is the one runtime family retaining the observed-before-tool provider retry.
    retry = getattr(event, '_automation_input_kind', None) is _BOT_REQUEST
    return {'result_recovery_v1':'provider_before_execution' if retry else 'consume_only'}


class _ExecutionWitness:
    def __init__(self, claim):
        self.claim = claim
        self.observer_armed = False
        self.tool_started = False
        self.lock = threading.Lock()

    def payload(self):
        with self.lock:
            return {**self.claim.binding(), 'observer_armed':self.observer_armed,
                    'tool_started':self.tool_started}


_witness: ContextVar[object] = ContextVar('automation_execution_witness', default=None)


def arm_tool_witness(agent):
    """Observe actual runtime tool starts; no result/model field may certify no execution."""
    witness = _witness.get()
    if not isinstance(witness, _ExecutionWitness):
        return
    previous = agent.tool_start_callback
    with witness.lock:
        witness.observer_armed = True

    def started(*args, **kwargs):
        with witness.lock:
            witness.tool_started = True
        if previous is not None:
            return previous(*args, **kwargs)
    agent.tool_start_callback = started


def _persist_witness(witness):
    claim = witness.claim
    encoded = json.dumps(witness.payload(), sort_keys=True)
    def write(conn):
        # The witness is sealed before the result is settled; a stale owner cannot
        # authorize retry by overwriting a successor's execution evidence.
        row = conn.execute('SELECT status,generation FROM session_admissions WHERE admission_id=?',
                           (claim.admission,)).fetchone()
        if row is None or row[0] != 'started' or row[1] != claim.generation:
            raise RuntimeError('automation execution witness lost its generation')
        conn.execute('INSERT OR REPLACE INTO state_meta(key,value) VALUES(?,?)',
                     (_WITNESS_PREFIX + claim.admission, encoded))
    claim.db._execute_write(write)


def retry_execution_safe(authority, row):
    """True only for an observed runtime turn with no tool start. Missing evidence is unknown."""
    if consume_only(row):
        return False
    with authority.db._read_ctx() as conn:
        saved = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             (_WITNESS_PREFIX + row['admission_id'],)).fetchone()
    if saved is None:
        return False
    saved = json.loads(saved[0])
    return (saved.get('profile_home') == str(Path(authority.db.db_path).parent.resolve())
        and saved.get('session_id') == row['target_session_id']
        and saved.get('admission_id') == row['admission_id']
        and saved.get('generation') == row['generation']
        and saved.get('payload_sha256') == hashlib.sha256(json.dumps(row['payload'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest()
        and saved.get('observer_armed') is True and saved.get('tool_started') is False)


@dataclass(frozen=True)
class _Claim:
    db: object
    profile: str
    session: str
    admission: str
    generation: int
    request: str
    route: str
    payload_digest: str
    destination: str
    consume_only: bool
    seal: object

    def binding(self):
        return {'v':1, 'profile_home':self.profile, 'session_id':self.session,
                'admission_id':self.admission, 'generation':self.generation,
                'request_id':self.request, 'route':self.route,
                'payload_sha256':self.payload_digest, 'destination':json.loads(self.destination),
                'consume_only':self.consume_only}


def _descriptor(row):
    payload = row.get('payload') or {}
    return payload.get('local_automation_v1') or payload.get('native_text_v1', {}).get('automation')


def automation_row(row):
    descriptor = _descriptor(row)
    route = ((row.get('payload') or {}).get('native_text_v1') or {}).get('route')
    route = descriptor.get('route', route) if isinstance(descriptor, dict) else None
    return bool(route and row.get('principal_id') == 'automation:' + route
                and descriptor.get('owner') == row.get('target_session_id')
                and descriptor.get('identity') == row.get('request_id'))


def consume_only(row):
    """Only the admission writer's frozen descriptor, never the Bot message/result body."""
    return automation_row(row) and (_descriptor(row) or {}).get('result_recovery_v1') == 'consume_only'


def _row_policy(row):
    if automation_row(row):
        return STRICT
    env = (row.get('payload') or {}).get('native_text_v1') or {}
    source = env.get('source') or {}
    if (str(row.get('principal_id', '')).startswith('messaging:') and env.get('route')
            and 'automation' not in env and source.get('is_bot') is False):
        return HUMAN
    return 'legacy_unknown'


def claim_for_event(event):
    claim = getattr(event, '_automation_final_claim', None)
    return claim if type(claim) is _Claim and claim.seal is _SEAL else None


def native_claim_for_event(event):
    claim = getattr(event, '_native_final_claim', None)
    return claim if type(claim) is _Claim and claim.seal is _SEAL else None


def current_claim():
    return _current.get()


@asynccontextmanager
async def automation_delivery_scope(authority, row, event):
    policy = _row_policy(row)
    if policy == 'legacy_unknown':
        yield
        return
    from hermes_state_runtime import get_session_admission
    saved = await asyncio.to_thread(get_session_admission, authority.db, admission_id=row['admission_id'])
    keys = ('admission_id', 'request_id', 'target_session_id', 'principal_id', 'generation',
            'owner_epoch', 'status', 'payload')
    if (saved is None or any(saved.get(key) != row.get(key) for key in keys)
            or row['status'] != 'started' or type(row.get('generation')) is not int):
        raise RuntimeError('automation final claim does not match the committed admission')
    descriptor = _descriptor(row) or {}
    route = descriptor.get('route') or row['payload']['native_text_v1']['route']
    claim = _Claim(authority.db, str(Path(authority.db.db_path).parent.resolve()),
        row['target_session_id'], row['admission_id'], row['generation'], row['request_id'], route,
        hashlib.sha256(json.dumps(row['payload'], sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        json.dumps(event.source.to_dict(), sort_keys=True),
        consume_only(row), _SEAL)
    event._automation_final_claim = claim if policy == STRICT else None
    event._native_final_claim = claim
    token = _current.set(claim if policy == STRICT else None)
    witness = _ExecutionWitness(claim)
    witness_token = _witness.set(witness)
    try:
        yield
    finally:
        # This write may fail; that must not create a false no-execution certificate.
        # Result settlement may still preserve the failure, but retry will fail closed.
        try:
            if policy == STRICT:
                await asyncio.to_thread(_persist_witness, witness)
        finally:
            _witness.reset(witness_token)
            _current.reset(token)


def delivery_binding(event, route):
    """Require a durable, exact settled result before any final transport side effect."""
    claim = claim_for_event(event)
    if claim is None:
        return None
    if claim.route != route:
        raise RuntimeError('automation final route changed; reconcile before sending')
    if json.dumps(event.source.to_dict(), sort_keys=True) != claim.destination:
        raise RuntimeError('automation final destination changed; reconcile before sending')
    from hermes_state_runtime import get_session_admission
    from gateway.session_results import admission_result
    row = get_session_admission(claim.db, admission_id=claim.admission)
    if (row is None or row['target_session_id'] != claim.session or row['generation'] != claim.generation
            or row['status'] != 'terminal' or row['outcome'] not in {'completed', 'failed', 'interrupted'}
            or not automation_row(row)
            or hashlib.sha256(json.dumps(row['payload'], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                != claim.payload_digest
            or admission_result(claim.db, claim.admission) is None):
        raise RuntimeError('automation final result is not durably settled; reconcile before sending')
    return claim.binding()


def strict_event(event):
    return claim_for_event(event) is not None
