import json
import re

from gateway.session_task_analysis_input import _text

_SESSION = re.compile(r'@session:([A-Za-z0-9][A-Za-z0-9_-]{0,63})/([A-Za-z0-9][A-Za-z0-9_-]{0,255})(?![A-Za-z0-9_/-])')
_BARE_SESSION = re.compile(r'@session:([A-Za-z0-9][A-Za-z0-9_-]{0,255})(?![A-Za-z0-9_/-])')
_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,255}\Z')
_PROFILE = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _requested(args, profile):
    value = args.get('session_id')
    if not isinstance(value, str):
        return None
    explicit = args.get('profile')
    if explicit is not None:
        if not isinstance(explicit, str):
            return None
        if explicit.strip():
            from hermes_cli.profiles import normalize_profile_name, validate_profile_name
            try:
                explicit = normalize_profile_name(explicit)
                validate_profile_name(explicit)
            except ValueError:
                return None
        else:
            explicit = None
    if value.startswith('@session:'):
        match = _SESSION.fullmatch(value)
        if match:
            return (explicit or match.group(1).lower(), match.group(2))
        bare = _BARE_SESSION.fullmatch(value)
        return (explicit or profile, bare.group(1)) if bare else None
    if '/' in value:
        label, _, sid = value.partition('/')
        if explicit:
            label = explicit
        else:
            from hermes_cli.profiles import normalize_profile_name, validate_profile_name
            try:
                label = normalize_profile_name(label)
                validate_profile_name(label)
            except ValueError:
                return None
    else:
        label, sid = explicit or profile, value
    if isinstance(label, str) and _PROFILE.fullmatch(label) and _TOKEN.fullmatch(sid):
        return label, sid
    return None


def _unwrap(name, args):
    if name != 'tool_call':
        return name, args
    from tools.tool_search_validation import normalize_tool_call_entries
    entries, error = normalize_tool_call_entries(args)
    if error or len(entries) != 1:
        return None, {}
    return entries[0]['name'], entries[0]['arguments']


def _calls(row, registry):
    if not isinstance(row.get('tool_calls'), list):
        if not isinstance(row.get('tool_calls'), str):
            return
        try:
            parsed = json.loads(row['tool_calls'])
        except ValueError:
            return
    else:
        parsed = row['tool_calls']
    for call in parsed or []:
        function = call.get('function') if isinstance(call, dict) else None
        if isinstance(call, dict) and call.get('id') and isinstance(function, dict):
            registry[call['id']] = function


def _read_call(db, row, function, profile):
    """``(profile, stored_id)`` a completed native read reached, else None."""
    name = function.get('name') or row.get('tool_name')
    args = _object(function.get('arguments')) or {}
    result = _object(_text(row.get('content')))
    if not result:
        return None
    if name == 'execute_code':
        if result.get('status') != 'success':
            return None
        payload, link = _object(result.get('output')), None
        if not payload or payload.get('mode') not in ('read', 'scroll') or payload.get('success') is not True:
            return None
        link, result = payload.get('link'), payload
        requested = _requested({'session_id': link}, profile) if isinstance(link, str) else None
        return requested if requested and result.get('session_id') == requested[1] else None
    if result.get('success') is not True:
        return None
    name, args = _unwrap(name, args)
    if name != 'session_search' or result.get('mode') not in ('read', 'scroll'):
        return None
    requested = _requested(args, profile)
    if not requested or not isinstance(result.get('session_id'), str) or not _TOKEN.fullmatch(result['session_id']):
        return None
    try:
        anchor = int(args.get('around_message_id'))
    except (TypeError, ValueError):
        anchor = None
    if (result['mode'] == 'scroll' and anchor is not None
            and result.get('around_message_id') == anchor and result.get('messages')):
        return requested[0], result['session_id']
    return requested if result['session_id'] == requested[1] or db.get_compression_tip(requested[1]) == result['session_id'] else None


def _unresolved_read(row, function):
    name = function.get('name') or row.get('tool_name')
    result = _object(_text(row.get('content')))
    if name == 'execute_code':
        if not result:
            return False
        payload = _object(result.get('output'))
        return payload is not None and payload.get('mode') in ('read', 'scroll')
    else:
        name, _ = _unwrap(name, _object(function.get('arguments')) or {})
    return name in ('session_search', 'execute_code') and result is not None and result.get('mode') in ('read', 'scroll')


def _discovered(row, function, profile):
    name = function.get('name') or row.get('tool_name')
    args = _object(function.get('arguments')) or {}
    result = _object(_text(row.get('content')))
    if name == 'execute_code':
        if not result or result.get('status') != 'success':
            return []
        result, args = _object(result.get('output')), {}
    else:
        name, args = _unwrap(name, args)
    if name not in ('session_search', 'execute_code') or not result or result.get('success') is not True or result.get('mode') != 'discover':
        return []
    hits = result.get('results')
    if not isinstance(hits, list):
        return []
    found = []
    for hit in hits:
        if not isinstance(hit, dict) or not any(isinstance(hit.get(key), list) and hit[key] for key in ('messages', 'bookend_start', 'bookend_end')):
            continue
        requested = _requested({'session_id': hit.get('session_id'), 'profile': args.get('profile')}, profile)
        link = _requested({'session_id': hit.get('link')}, profile)
        if link and (not requested or link[1] != requested[1] or args.get('profile') and link[0] != requested[0]):
            continue
        if requested:
            found.append((*(link or requested), 'read', row['id']))
    return found


def _delegates(db, target, function, result, profile):
    name, _ = _unwrap(function.get('name'), _object(function.get('arguments')) or {})
    if name != 'delegate_task' or not result:
        return [], []
    ids = result.get('subagent_ids')
    ids = list(ids) if isinstance(ids, list) else []
    for field in ('results', 'inline_results'):
        entries = result.get(field)
        if isinstance(entries, list):
            ids.extend(entry.get('subagent_id') for entry in entries if isinstance(entry, dict))
    from tools.delegate_tool_sessions import stored_child_session
    lineage = db.lineage.resume_lineage_ids(target)
    found, missing = [], []
    for subagent_id in dict.fromkeys(item for item in ids if isinstance(item, str) and item):
        child = stored_child_session(db, subagent_id, lineage)
        if child:
            found.append((profile, child, 'delegate'))
        else:
            missing.append(subagent_id)
    return found, missing


def _continuations(db, target, rows, profile):
    via = next((row['id'] for row in rows if row['role'] == 'user'), None)
    found = []
    for sid in db.lineage.resume_lineage_ids(target):
        record = db.sessions.get(sid)
        row = record.as_session_row() if record is not None else {}
        parent = row.get('parent_session_id')
        cfg = _object(row.get('model_config')) or {}
        if parent and (cfg.get('_branched_from') == parent or cfg.get('_reset_from') == parent):
            found.append((profile, parent, 'continuation', via))
    return found


def copied_session_results(rows):
    calls, copied = {}, set()
    for raw in rows:
        row = dict(raw)
        if row['role'] == 'assistant':
            _calls(row, calls)
            continue
        if row['role'] != 'tool':
            continue
        function = calls.get(row.get('tool_call_id'), {})
        name = function.get('name') or row.get('tool_name')
        args = _object(function.get('arguments')) or {}
        result = _object(_text(row.get('content')))
        if name == 'execute_code' and result:
            if result.get('status') != 'success':
                continue
            result = _object(result.get('output'))
        else:
            name, args = _unwrap(name, args)
        if (name in ('session_search', 'execute_code') and result
                and result.get('success') is True and result.get('mode') in ('read', 'scroll', 'discover')):
            copied.add(row['id'])
    return copied


def candidates(db, target, rows, profile):
    found, calls, missing, unresolved = [], {}, [], []
    for raw in rows:
        row = dict(raw)
        text = _text(row.get('content'))
        if row['role'] in ('user', 'assistant') and not row.get('_compressed_summary'):
            found += [(match.group(1).lower(), match.group(2), 'reference', row['id']) for match in _SESSION.finditer(text)]
            found += [(profile, match.group(1), 'reference', row['id']) for match in _BARE_SESSION.finditer(text)]
        if row['role'] == 'assistant':
            _calls(row, calls)
            continue
        if row['role'] != 'tool':
            continue
        function = calls.get(row.get('tool_call_id'), {})
        read = _read_call(db, row, function, profile)
        if read:
            found.append((*read, 'read', row['id']))
        elif _unresolved_read(row, function):
            unresolved.append(row['id'])
        found.extend(_discovered(row, function, profile))
        delegated, absent = _delegates(db, target, function, _object(text), profile)
        found += [(*item, row['id']) for item in delegated]
        missing.extend(absent)
    return list(dict.fromkeys([*found, *_continuations(db, target, rows, profile)])), list(dict.fromkeys(missing)), unresolved
