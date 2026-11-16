"""Read-only capability proof for the POSIX Cron installation admission path.

Probing never loads an external scheduler plugin. Only the exact built-in provider
is supported; an unstarted, custom or opaque provider fails closed. A serving
probe reads the instance and tick binding registered by the real ticker startup.
"""
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import types
from typing import Any

VERSION = 7


class CapabilityUnavailable(RuntimeError):
    pass


def probe_provider_name():
    from hermes_constants import get_hermes_home
    from hermes_cli.config_backend import FileBackend
    from hermes_cli.managed_scope import get_managed_dir

    if os.environ.get("HERMES_CONFIG_BACKEND", "file").strip() not in ("", "file"):
        raise CapabilityUnavailable("remote config capability probe is not side-effect-free")
    if get_managed_dir() is not None:
        raise CapabilityUnavailable("managed config capability resolution is not supported")
    path = get_hermes_home() / "config.yaml"
    try:
        config = FileBackend.read_path(path) or {}
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict) or not isinstance(config.get("cron", {}), dict):
        raise CapabilityUnavailable("Cron provider configuration is not a mapping")
    name = config.get("cron", {}).get("provider", "") or ""
    if not isinstance(name, str):
        raise CapabilityUnavailable("Cron provider configuration is not a literal name")
    return name.strip()


def _stable(value, seen=()):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, types.CodeType):
        return {"code": _code_digest(value)}
    if isinstance(value, (tuple, list)):
        return [_stable(item, seen) for item in value]
    if isinstance(value, frozenset):
        return {"frozenset": sorted((_stable(item, seen) for item in value), key=_json)}
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _stable(item, seen) for key, item in sorted(value.items())}
    if isinstance(value, types.FunctionType):
        return _callable_identity(value, seen)
    raise CapabilityUnavailable("opaque callable state cannot be proved without executing it")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def owner_digest(owner):
    return hashlib.sha256(_json(owner).encode()).hexdigest()


def _code_digest(code):
    # Exclude filenames/line numbers; identical code in another checkout must match.
    payload = (code.co_code, code.co_consts, code.co_names, code.co_varnames,
               code.co_freevars, code.co_cellvars, code.co_argcount,
               code.co_posonlyargcount, code.co_kwonlyargcount, code.co_flags,
               code.co_exceptiontable)
    return hashlib.sha256(_json(_stable(payload)).encode()).hexdigest()


def _module_identity(name):
    module = sys.modules.get(name)
    source = getattr(module, "__file__", None)
    if not source or Path(source).suffix != ".py":
        raise CapabilityUnavailable("implementation module source is unavailable")
    return {"module": name, "source_sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest()}


def _callable_identity(fn, seen=()):
    if isinstance(fn, types.MethodType):
        fn = fn.__func__
    if not isinstance(fn, types.FunctionType):
        raise CapabilityUnavailable("callable is not a directly inspectable Python function")
    identity: dict[str, Any] = {**_module_identity(fn.__module__), "qualname": fn.__qualname__,
                "code_sha256": _code_digest(fn.__code__)}
    if fn in seen:
        return identity
    seen = (*seen, fn)
    # Inspect the actual wrapper, then its wrapped function: never unwrap first.
    identity["closure"] = [_stable(cell.cell_contents, seen) for cell in (fn.__closure__ or ())]
    identity["defaults"] = _stable(fn.__defaults__, seen)
    identity["kwdefaults"] = _stable(fn.__kwdefaults__, seen)
    identity["globals"] = {
        name: _stable(value, seen)
        for name, value in sorted(fn.__globals__.items())
        if name in fn.__code__.co_names and name in {"install_gate", "_jobs_lock_admitted", "_jobs_lock",
                                                     "_under_fire_fence", "_with_job", "_fire_job_lock",
                                                     "lock_fd", "unlock_fd"}
    }
    if hasattr(fn, "__wrapped__"):
        identity["wrapped"] = _callable_identity(fn.__wrapped__, seen)
    return identity


def _global_binding(fn, name):
    fn = fn.__func__ if isinstance(fn, types.MethodType) else fn
    while name not in fn.__globals__ and hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    try:
        return fn.__globals__[name]
    except KeyError as exc:
        raise CapabilityUnavailable(f"required callable binding is missing: {name}") from exc


def _callable_bindings(fn, names):
    return {name: _callable_identity(_global_binding(fn, name)) for name in names}


def _provider_identity(provider):
    from cron import scheduler_provider as sp
    cls = type(provider)
    # Custom descriptors/__getattribute__ may execute arbitrary plugin code. Do not call them.
    if cls is not sp.InProcessCronScheduler:
        raise CapabilityUnavailable("external/custom provider capability requires unsafe plugin inspection")
    if inspect.getattr_static(cls, "__getattribute__") is not object.__getattribute__:
        raise CapabilityUnavailable("provider has an opaque attribute resolver")
    methods = {}
    for name in ("claim_fire", "_claim_fire_admitted", "fire_due", "fire_claimed", "start", "_start_multiplex"):
        method = inspect.getattr_static(provider, name)
        if not isinstance(method, (types.FunctionType, types.MethodType)):
            raise CapabilityUnavailable("provider method binding is not a directly inspectable method")
        methods[name] = {"binding": "instance" if name in vars(provider) else "class",
                         "implementation": _callable_identity(method)}
    return {**_module_identity(cls.__module__), "class": cls.__qualname__,
            "dispatch": "split" if inspect.getattr_static(provider, "fire_due") is sp.CronScheduler.fire_due else "legacy-fire_due",
            "methods": methods}


def runtime_identity(*, serving=False, home=None):
    import cron
    modules = {}
    for name in ("executions", "jobs", "scheduler", "scheduler_provider", "scheduler_tick", "install_gate"):
        module = importlib.import_module(f"cron.{name}")
        if vars(cron).get(name) is not module:
            raise CapabilityUnavailable(f"package and dynamic import module bindings differ: {name}")
        modules[name] = module
    executions, jobs, scheduler, sp, scheduler_tick, gates = (
        modules[name] for name in ("executions", "jobs", "scheduler", "scheduler_provider", "scheduler_tick", "install_gate"))
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override

    token = set_hermes_home_override(home or get_hermes_home())  # health: allow HX016 -- read-only probe selects only the config home; it must not hydrate secret/terminal scopes
    try:
        # This mode refuses external configuration BEFORE the plugin loader/is_available hook.
        resolved = sp.resolve_cron_scheduler(capability_probe=True)
    finally:
        reset_hermes_home_override(token)
    provider = sp._install_runtime_provider if serving else resolved
    if provider is None:
        raise CapabilityUnavailable("serving scheduler instance has not been registered")
    provider_identity = _provider_identity(provider)
    if provider_identity != _provider_identity(resolved):
        raise CapabilityUnavailable("serving provider differs from the safe resolved provider")
    tick = vars(provider).get("_install_tick") if serving else scheduler.tick
    if tick is None:
        raise CapabilityUnavailable("serving ticker has not captured its imported tick callable")
    claim_fence = _global_binding(jobs.claim_job_for_fire, "_under_fire_fence")
    claim_with_job = _global_binding(jobs.claim_job_for_fire, "_with_job")
    claim_fire_lock = _global_binding(claim_fence, "_fire_job_lock")
    claim_jobs_lock = _global_binding(claim_with_job, "_jobs_lock")
    claim_jobs_lock_admitted = _global_binding(claim_jobs_lock, "_jobs_lock_admitted")
    functions = {
        "create_execution": executions.create_execution,
        "claim_job_for_fire": jobs.claim_job_for_fire,
        "run_one_job": scheduler.run_one_job,
        "scheduler_create_execution": scheduler.create_execution,
        "scheduler_claim_job_for_fire": scheduler.claim_job_for_fire,
        "execution_impl": _global_binding(executions.create_execution, "_create_execution"),
        "jobs_lock": jobs._jobs_lock,
        "tick": scheduler_tick.tick,
        "provider_imported_tick": tick,
        "scheduler_alias": scheduler.tick,
        "package_alias": cron.tick,
        "resolver": sp.resolve_cron_scheduler,
        "split_dispatch": sp.provider_supports_split_fire,
        "claim_under_fire_fence": claim_fence,
        "claim_with_job": claim_with_job,
        "claim_fire_lock": claim_fire_lock,
        "claim_jobs_lock": claim_jobs_lock,
        "claim_jobs_lock_admitted": claim_jobs_lock_admitted,
    }
    bindings = {
        "execution_gate": _global_binding(executions.create_execution, "install_gate"),
        "run_execution": _global_binding(scheduler.run_one_job, "create_execution"),
        "scheduler_claim_with_job": _global_binding(scheduler.claim_job_for_fire, "_with_job"),
        "jobs_gate": _global_binding(jobs._jobs_lock, "install_gate"),
        "jobs_lock_admitted": _global_binding(jobs._jobs_lock, "_jobs_lock_admitted"),
        "claim_jobs_lock": _global_binding(jobs.claim_job_for_fire, "_jobs_lock"),
        "claim_fence_chain": {
            "under_fire_fence": _callable_bindings(claim_fence, ("_fire_job_lock",)),
            "with_job": _callable_bindings(claim_with_job, ("_jobs_lock",)),
            "jobs_lock": _callable_bindings(claim_jobs_lock, ("install_gate", "_jobs_lock_admitted")),
            "jobs_lock_admitted": _callable_identity(claim_jobs_lock_admitted),
        },
        "tick_admitted": _global_binding(tick, "_tick_admitted"),
        # These are the modules read by the providers' dynamic IMPORT_FROM statements.
        "provider_dynamic_gate": gates.install_gate,
        "gate_lock_fd": _global_binding(gates.install_gate, "lock_fd"),
        "gate_unlock_fd": _global_binding(gates.install_gate, "unlock_fd"),
    }
    return {"version": VERSION, "provider": provider_identity,
            "functions": {name: _callable_identity(fn) for name, fn in functions.items()},
            "gate": _callable_identity(gates.install_gate),
            "bindings": {name: (_stable(fn) if isinstance(fn, dict) else _callable_identity(fn))
                         for name, fn in bindings.items()}}
