"""Regression coverage for session previews after a model switch."""

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "test_state.db")
    yield session_db
    session_db.close()


def test_preview_skips_model_switch_marker(db):
    db.create_session("s1", "desktop")
    db.append_message(
        "s1",
        "user",
        "[System: The active model for this chat has changed to deepseek-v4.1-flash via provider custom:relay]",
        display_kind="model_switch",
    )
    db.append_message("s1", "user", "What remains to finish in this project?")

    session = db.list_sessions_rich()[0]

    assert session["preview"] == "What remains to finish in this project?"


def test_preview_is_empty_for_model_switch_only_session(db):
    db.create_session("s1", "desktop")
    db.append_message(
        "s1",
        "user",
        "[System: The active model for this chat has changed to deepseek-v4.1-flash via provider custom:relay]",
        display_kind="model_switch",
    )

    session = db.list_sessions_rich()[0]

    assert session["preview"] == ""
