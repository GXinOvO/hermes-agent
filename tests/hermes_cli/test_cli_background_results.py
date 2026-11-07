"""Classic CLI side results survive a new viewer without replaying the producer."""
from copy import deepcopy
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import cli as cli_module
from hermes_cli import cli_commands_mixin as commands, gateway_side
from hermes_state import SessionDB
from hermes_state_background import results
from tests.hermes_cli.test_cli_approval_ui import _make_background_cli_stub


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gateway_side, "connection_for", lambda _cli: None)
    monkeypatch.setattr(cli_module, "_cprint", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli_module, "_sync_process_session_id", lambda *_a: None)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli")
    db.create_session("elsewhere", source="cli")
    try:
        yield db
    finally:
        db.close()


def _viewer(db, session_id="parent"):
    viewer = _make_background_cli_stub()
    viewer.session_id = session_id
    viewer._session_db = db
    viewer.agent = None
    viewer.conversation_history = []
    viewer._app = None
    viewer._pending_resume_sessions = None
    viewer._resumed = False
    viewer.resume_display = "minimal"
    viewer._console_print = MagicMock()
    viewer._restore_session_cwd = MagicMock()
    viewer._restore_session_yolo = MagicMock()
    viewer._restore_session_model = MagicMock()
    threads = []
    real_side_worker = viewer._side_worker

    def capture_worker(*args, **kwargs):
        thread = real_side_worker(*args, **kwargs)
        threads.append(thread)
        return thread

    viewer._side_worker = capture_worker
    viewer._test_threads = threads
    return viewer


def _join(viewer):
    for thread in viewer._test_threads:
        thread.join(timeout=10)
        assert not thread.is_alive()


def _startup(viewer, monkeypatch):
    viewer._resumed = True
    viewer._inline_images_mode = "off"
    viewer.preloaded_skills = []
    viewer._preload_skills_requested = []
    viewer._startup_skills_line_shown = False
    for name in ("_ensure_gateway_session", "show_banner", "_show_security_advisories",
                 "_maybe_offer_first_run_setup", "_display_resumed_history",
                 "_tui_startup_prewarm_and_warnings", "_print_random_tip",
                 "_tui_startup_background_maintenance"):
        setattr(viewer, name, MagicMock())
    viewer._preload_resumed_session = MagicMock(return_value=False)
    monkeypatch.setattr(cli_module, "_detect_light_mode", lambda: None)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    viewer._tui_print_startup()


@pytest.mark.parametrize("verb", ["bg", "btw"])
def test_local_result_is_sealed_before_print_and_read_once_per_viewer(local_store, monkeypatch, verb):
    calls = []

    class Agent:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs["session_id"]))

        def run_conversation(self, **kwargs):
            calls.append(("run", kwargs["task_id"]))
            return {"final_response": "sealed answer", "messages": []}

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr("run_agent.AIAgent", Agent)

    def answer(*args, **kwargs):
        calls.append(("btw", None))
        return "sealed answer"

    monkeypatch.setattr("agent.side_question.answer_side_question", answer)
    panels = []

    def panel(viewer, **kwargs):
        saved = results(viewer._session_db, "parent")
        panels.append((viewer, kwargs["body"], deepcopy(saved)))

    monkeypatch.setattr(commands, "_print_side_result_panel", panel)
    viewer = _viewer(local_store)
    before_history = deepcopy(viewer.conversation_history)
    getattr(viewer, f"_handle_{'background' if verb == 'bg' else 'btw'}_command")(f"/{verb} inspect")
    _join(viewer)
    assert len(panels) == 1
    expected_kind = "background.complete" if verb == "bg" else "btw.complete"
    assert len(panels[0][2]) == 1, "the result must already be sealed when the panel prints"
    saved = panels[0][2][0]
    assert saved["event_type"] == expected_kind
    assert saved["payload"]["text"] == "sealed answer"
    assert saved["payload"]["task_id"]
    assert viewer.conversation_history == before_history
    assert local_store.get_messages("parent") == []
    expected_calls = deepcopy(calls)
    path = local_store.db_path
    local_store.close()
    reopened = SessionDB(path)
    try:
        assert results(reopened, "parent") == [saved]
        assert results(reopened, "elsewhere") == []
        resumed = _viewer(reopened, "elsewhere")
        resumed._handle_resume_command("/resume parent")
        resumed._handle_resume_command("/resume parent")
        assert len([p for p in panels if p[0] is resumed]) == 1
        assert resumed.conversation_history == []
        fresh = _viewer(reopened)
        _startup(fresh, monkeypatch)
        _startup(fresh, monkeypatch)
        assert len([p for p in panels if p[0] is fresh]) == 1
        assert results(reopened, "parent") == [saved]
        assert reopened.get_messages("parent") == []
        assert calls == expected_calls
        assert len([kind for kind, _ in calls if kind in ("run", "btw")]) == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("verb", ["bg", "btw"])
@pytest.mark.parametrize("existing", ["running", "completed", "unknown", "prompt", "target", "config"])
def test_existing_task_identity_never_reexecutes(local_store, monkeypatch, verb, existing):
    started, release = threading.Event(), threading.Event()
    calls = []

    def execute():
        calls.append("execute")
        started.set()
        assert release.wait(timeout=10)
        return "one answer"

    class Agent:
        def __init__(self, **_kwargs):
            calls.append("agent")

        def run_conversation(self, **_kwargs):
            return {"final_response": execute()}

        def close(self):
            pass

    monkeypatch.setattr("run_agent.AIAgent", Agent)
    monkeypatch.setattr("agent.side_question.answer_side_question", lambda *_a, **_kw: execute())
    monkeypatch.setattr(commands, "_print_side_result_panel", lambda *_a, **_kw: None)
    monkeypatch.setattr(commands.uuid, "uuid4", lambda: SimpleNamespace(hex="fixedtask"))
    monkeypatch.setattr(commands, "datetime", SimpleNamespace(
        now=lambda: SimpleNamespace(strftime=lambda _fmt: "123456")))
    viewer = _viewer(local_store)
    handler = getattr(viewer, f"_handle_{'background' if verb == 'bg' else 'btw'}_command")
    handler(f"/{verb} inspect")
    assert started.wait(timeout=10)
    try:
        if existing == "completed":
            release.set()
            _join(viewer)
        if existing == "target":
            viewer.session_id = "elsewhere"
        if existing == "config":
            viewer._resolve_turn_agent_config.return_value["runtime"]["base_url"] = "https://changed.invalid"
        prompt = "changed" if existing == "prompt" else "inspect"
        if existing in {"prompt", "target", "config"}:
            with pytest.raises(ValueError, match="identity conflict"):
                handler(f"/{verb} {prompt}")
        elif existing == "unknown":
            with monkeypatch.context() as dead_owner:
                dead_owner.setattr("hermes_state_background._owner_live", lambda _record: False)
                handler(f"/{verb} {prompt}")
        else:
            handler(f"/{verb} {prompt}")
    finally:
        release.set()
        _join(viewer)
    assert calls.count("execute") == 1
    assert calls.count("agent") == (1 if verb == "bg" else 0)
    assert local_store.get_messages("parent") == []


@pytest.mark.parametrize("verb", ["bg", "btw"])
def test_gateway_side_work_is_never_registered_or_executed_locally(local_store, monkeypatch, verb):
    connection = object()
    monkeypatch.setattr(gateway_side, "connection_for", lambda _cli: connection)
    start = MagicMock(return_value=("gateway-task", lambda: "gateway answer"))
    monkeypatch.setattr(gateway_side, "start", start)
    local_agent = MagicMock(side_effect=AssertionError("must remain gateway-owned"))
    monkeypatch.setattr("run_agent.AIAgent", local_agent)
    monkeypatch.setattr("agent.side_question.answer_side_question", local_agent)
    begin = MagicMock(side_effect=AssertionError("must not locally register gateway work"))
    monkeypatch.setattr("hermes_state_background.begin", begin)
    panels = []
    monkeypatch.setattr(commands, "_print_side_result_panel",
                        lambda _cli, **kwargs: panels.append(kwargs["body"]))
    viewer = _viewer(local_store)
    handler = getattr(viewer, f"_handle_{'background' if verb == 'bg' else 'btw'}_command")
    handler(f"/{verb} inspect")
    _join(viewer)
    start.assert_called_once_with(connection, "parent", "background" if verb == "bg" else "btw", "inspect")
    begin.assert_not_called()
    local_agent.assert_not_called()
    assert panels == ["gateway answer"]
    assert results(local_store, "parent") == []


def test_deleted_target_between_seal_and_display_never_prints_success(local_store, monkeypatch):
    from hermes_cli.cli_background_results import admit
    from hermes_state_background import complete

    viewer = _viewer(local_store)
    sealed = admit(viewer, "bg-deleted", "background.complete", inputs={"prompt": "inspect"}, route={})
    complete(local_store, sealed.task_id, sealed.binding,
             {"task_id": sealed.task_id, "text": "must not appear"})
    local_store.delete_session("parent")
    printed = MagicMock()
    assert sealed.display(printed) is False
    printed.assert_not_called()
    assert results(local_store, "parent") == []


@pytest.mark.parametrize("verb", ["bg", "btw"])
def test_unsealed_result_reports_unknown_without_printing_success(local_store, monkeypatch, verb):
    class Agent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **_kwargs):
            return {"final_response": "unsealed answer"}

        def close(self):
            pass

    monkeypatch.setattr("run_agent.AIAgent", Agent)
    monkeypatch.setattr("agent.side_question.answer_side_question", lambda *_a, **_kw: "unsealed answer")
    monkeypatch.setattr("hermes_state_background.complete", MagicMock(side_effect=OSError("cannot write")))
    panels = MagicMock()
    monkeypatch.setattr(commands, "_print_side_result_panel", panels)
    printed = []
    monkeypatch.setattr(cli_module, "_cprint", lambda *args, **_kw: printed.extend(map(str, args)))
    viewer = _viewer(local_store)
    getattr(viewer, f"_handle_{'background' if verb == 'bg' else 'btw'}_command")(f"/{verb} inspect")
    _join(viewer)
    panels.assert_not_called()
    assert "unknown_execution" in "\n".join(printed)
    assert "unsealed answer" not in "\n".join(printed)
    assert results(local_store, "parent") == []


@pytest.mark.parametrize("verb", ["bg", "btw"])
def test_new_parent_exists_before_begin_and_stored_inputs_contain_only_hashes(local_store, monkeypatch, verb):
    import hermes_state_background as background
    from agent.redact import redact_sensitive_text

    secret = "sk-" + "private_credential_" * 4
    seen = []
    real_begin = background.begin

    def begin(db, task_id, binding):
        assert db.get_session("new-parent") is not None
        assert secret not in str(binding)
        seen.append(binding)
        return real_begin(db, task_id, binding)

    class Agent:
        def __init__(self, **kwargs):
            assert kwargs["session_db"] is local_store

        def run_conversation(self, **_kwargs):
            return {"final_response": "answer " + secret}

        def close(self):
            pass

    monkeypatch.setattr(background, "begin", begin)
    monkeypatch.setattr("run_agent.AIAgent", Agent)
    monkeypatch.setattr("agent.side_question.answer_side_question", lambda *_a, **_kw: "answer " + secret)
    monkeypatch.setattr(commands, "_print_side_result_panel", lambda *_a, **_kw: None)
    viewer = _viewer(local_store, "new-parent")
    viewer.config = {"private": secret}
    viewer._resolve_turn_agent_config.return_value["runtime"]["api_key"] = secret
    getattr(viewer, f"_handle_{'background' if verb == 'bg' else 'btw'}_command")(f"/{verb} inspect {secret}")
    _join(viewer)
    assert len(seen) == 1
    with local_store._read_ctx() as conn:
        raw = conn.execute("SELECT value FROM state_meta WHERE key LIKE 'background-result:%'").fetchone()[0]
    assert secret not in raw
    assert results(local_store, "new-parent")[0]["payload"]["text"] == redact_sensitive_text("answer " + secret)
    assert local_store.get_messages("new-parent") == []
