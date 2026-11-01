"""Primary transport recovery waits obey stop/steer without changing recovery budgets.

These tests use the real recovery and caller functions. Only client rebuilding and
clock/sleep are replaced; no AIAgent construction, network, or session DB is needed.
The file is also directly executable with stdlib unittest in runtime-only installs.
"""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx

from agent import agent_runtime_helpers as runtime
from agent import turn_recovery
from agent.error_classifier import classify_api_error
from agent.interrupt_control import InterruptControlMixin
from agent.turn_api_error import settle_unrecovered_error
from agent.turn_retry_state import TurnRetryState


class _RecoveryAgent(InterruptControlMixin, SimpleNamespace):
    """Native stop/redirect handling without constructing a network client."""


class _Clock:
    def __init__(self, agent, *, stop_after=None, redirect=None, wall_jump_after=None, wall_delta=1000.0):
        self.agent = agent
        self.elapsed = 0.0
        self.stop_after = stop_after
        self.wall_jump_after = wall_jump_after
        self.wall_delta = wall_delta
        self.wall_offset = 0.0
        self.sleeps = []
        self.redirect = redirect

    def time(self):
        return 1000.0 + self.elapsed + self.wall_offset

    def monotonic(self):
        return 5000.0 + self.elapsed

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds
        if self.wall_jump_after is not None and self.elapsed >= self.wall_jump_after:
            self.wall_offset = self.wall_delta
        if self.stop_after is not None and self.elapsed >= self.stop_after:
            if self.redirect is not None:
                self.agent._pending_redirect = self.redirect
            self.agent._interrupt_requested = True


class PrimaryRecoveryInterruptTests(unittest.TestCase):
    def setUp(self):
        self.agent = _RecoveryAgent(
            provider="custom", model="synthetic-primary", api_mode="chat_completions",
            base_url="https://example.invalid/v1", _fallback_activated=False,
            _fallback_index=0, _interrupt_requested=False, log_prefix="", client=None,
            _is_openrouter_url=lambda: False, _vprint=Mock(),
            _retire_shared_openai_client=Mock(), _persist_session=Mock(),
            _drop_trailing_empty_response_scaffolding=Mock(),
            _adopt_nous_key_before_expiry=Mock(), _touch_activity=Mock(),
            _has_pending_fallback=Mock(return_value=False),
            _try_activate_fallback=Mock(return_value=False),
            _buffer_diagnostic_status=Mock(), _auto_recovery_cycles=0,
            _current_streamed_assistant_text="", _has_content_after_think_block=bool,
            _execution_thread_id=None, _pending_redirect=None, _pending_steer=None,
        )
        self.agent._primary_runtime = {
            "provider": "custom", "model": "synthetic-primary", "api_mode": "chat_completions",
            "base_url": "https://example.invalid/v1", "api_key": "synthetic-not-a-secret",
            "client_kwargs": {}, "request_overrides": {},
        }

        self.agent._try_recover_primary_transport = lambda error, **kwargs: runtime.try_recover_primary_transport(self.agent, error, **kwargs)
        self.retry = TurnRetryState()
        self.messages = [{"role": "user", "content": "synthetic request"}]
        self.error = httpx.ReadTimeout("synthetic timeout")
        self.rebuild = self.enterContext(patch.object(runtime, "_rebuild_primary_client"))

    def _settle(self):
        return settle_unrecovered_error(
            self.agent, api_error=self.error,
            classified=classify_api_error(self.error, provider="custom"), _retry=self.retry,
            status_code=None, error_msg=str(self.error), is_context_length_error=False,
            is_rate_limited=False, _is_zai_coding_overload=False, _provider="custom",
            _base="https://example.invalid/v1", _model="synthetic-primary",
            messages=self.messages, api_messages=self.messages, api_kwargs={},
            active_system_prompt=None, conversation_history=[], approx_tokens=10,
            retry_count=3, max_retries=3, compression_attempts=0, api_call_count=1,
        )

    def _clock(self, stop_after=None, *, redirect=None):
        clock = _Clock(self.agent, stop_after=stop_after, redirect=redirect)
        self.enterContext(patch("time.time", clock.time))
        self.enterContext(patch("time.monotonic", clock.monotonic))
        self.enterContext(patch("time.sleep", clock.sleep))
        return clock

    def _clock_with_wall_jump(self, wall_jump_after, *, wall_delta=1000.0):
        clock = _Clock(self.agent, wall_jump_after=wall_jump_after, wall_delta=wall_delta)
        self.enterContext(patch("time.time", clock.time))
        self.enterContext(patch("time.monotonic", clock.monotonic))
        self.enterContext(patch("time.sleep", clock.sleep))
        return clock

    def test_pending_stop_returns_interrupted_without_recovery_sleep(self):
        self.agent._interrupt_requested = True
        clock = self._clock()
        verdict = self._settle()
        self.assertEqual(verdict.action, "return")
        assert verdict.result is not None
        self.assertTrue(verdict.result["interrupted"])
        self.assertEqual(clock.sleeps, [])
        self.agent._persist_session.assert_called_once()
        self.agent._try_activate_fallback.assert_not_called()

    def test_stop_during_recovery_wait_returns_within_one_slice(self):
        clock = self._clock(stop_after=0.2)
        verdict = self._settle()
        self.assertEqual(verdict.action, "return")
        assert verdict.result is not None
        self.assertTrue(verdict.result["interrupted"])
        self.assertLessEqual(clock.elapsed, 0.4)
        self.agent._try_activate_fallback.assert_not_called()

    def test_stop_during_final_recovery_slice_is_observed(self):
        clock = self._clock(stop_after=6.0)
        verdict = self._settle()
        self.assertEqual(verdict.action, "return")
        assert verdict.result is not None
        self.assertTrue(verdict.result["interrupted"])
        self.assertAlmostEqual(clock.elapsed, 6.0, delta=0.21)
        self.agent._try_activate_fallback.assert_not_called()

    def test_steer_preserves_redirect_and_rebuilds_outer_iteration(self):
        self.agent._pending_redirect = "synthetic correction"
        self.agent._interrupt_requested = True
        clock = self._clock()
        verdict = self._settle()
        self.assertEqual(verdict.action, "break")
        self.assertTrue(self.retry.restart_with_redirected_messages)
        self.assertEqual(self.agent._pending_redirect, "synthetic correction")
        self.assertEqual(clock.sleeps, [])
        self.agent._persist_session.assert_not_called()
        self.agent._try_activate_fallback.assert_not_called()

    def test_normal_recovery_keeps_six_second_budget_and_retry_state(self):
        self.retry.has_retried_429 = True
        self.agent._fallback_index = 2
        clock = self._clock()
        verdict = self._settle()
        self.assertEqual(verdict.action, "continue")
        self.assertEqual(verdict.retry_count, 0)
        self.assertTrue(self.retry.primary_recovery_attempted)
        self.assertFalse(self.retry.has_retried_429)
        self.assertEqual(self.agent._fallback_index, 0)
        self.assertFalse(self.agent._fallback_activated)
        self.assertAlmostEqual(clock.elapsed, 6.0, delta=0.21)
        self.rebuild.assert_called_once()
        self.agent._try_activate_fallback.assert_not_called()

    def test_wall_clock_jump_does_not_shorten_recovery_wait(self):
        clock = self._clock_with_wall_jump(wall_jump_after=0.2)
        verdict = self._settle()
        self.assertEqual(verdict.action, "continue")
        self.assertAlmostEqual(clock.elapsed, 6.0, delta=0.21)
        self.assertTrue(self.retry.primary_recovery_attempted)

    def test_backward_wall_clock_jump_does_not_extend_recovery_wait(self):
        clock = self._clock_with_wall_jump(wall_jump_after=0.2, wall_delta=-100.0)
        verdict = self._settle()
        self.assertEqual(verdict.action, "continue")
        self.assertAlmostEqual(clock.elapsed, 6.0, delta=0.21)

    def test_long_recovery_wait_touches_activity_every_thirty_seconds(self):
        clock = self._clock()
        interrupted = turn_recovery.interruptible_backoff_sleep(
            self.agent, 61.0, self.retry, messages=self.messages,
            conversation_history=[], api_call_count=1,
            abort_message="synthetic abort", interrupt_text="synthetic interrupt",
            activity_label="synthetic recovery backoff",
        )
        self.assertIsNone(interrupted)
        self.assertAlmostEqual(clock.elapsed, 61.0, delta=0.21)
        self.assertEqual(self.agent._touch_activity.call_count, 2)
        activity = [call.args[0] for call in self.agent._touch_activity.call_args_list]
        self.assertEqual(activity, [
            "synthetic recovery backoff, 31s remaining",
            "synthetic recovery backoff, 1s remaining",
        ])

    def test_primary_rebuild_helper_has_no_wait_of_its_own(self):
        clock = self._clock()
        self.assertTrue(runtime.try_recover_primary_transport(self.agent, self.error, retry_count=3, max_retries=3))
        self.assertEqual(clock.sleeps, [])
        self.rebuild.assert_called_once()

    def test_rebuild_failure_still_reaches_existing_fallback(self):
        self.rebuild.side_effect = RuntimeError("synthetic rebuild failure")
        self.agent._try_activate_fallback.return_value = True
        self.enterContext(patch("agent.conversation_loop._arm_fallback_restart", return_value=None))
        clock = self._clock()
        verdict = self._settle()
        self.assertEqual(verdict.action, "break")
        self.assertFalse(self.retry.primary_recovery_attempted)
        self.assertEqual(clock.sleeps, [])
        self.agent._try_activate_fallback.assert_called_once()

    def test_status_503_does_not_rebuild_primary(self):
        class APIStatusError(Exception):
            status_code = 503

        error = APIStatusError("synthetic 503")
        clock = self._clock()
        self.assertFalse(runtime.try_recover_primary_transport(self.agent, error, retry_count=3, max_retries=3))
        self.rebuild.assert_not_called()
        self.assertEqual(clock.sleeps, [])

    def test_aggregator_and_active_fallback_skip_primary_rebuild(self):
        self.agent._is_openrouter_url = lambda: True
        self.assertFalse(runtime.try_recover_primary_transport(self.agent, self.error, retry_count=3, max_retries=3))
        self.agent._is_openrouter_url = lambda: False
        self.agent._fallback_activated = True
        self.assertFalse(runtime.try_recover_primary_transport(self.agent, self.error, retry_count=3, max_retries=3))
        self.rebuild.assert_not_called()

    def test_second_exhaustion_does_not_rebuild_or_wait_again(self):
        clock = self._clock()
        self.assertEqual(self._settle().action, "continue")
        self.agent._try_activate_fallback.return_value = True
        self.enterContext(patch("agent.conversation_loop._arm_fallback_restart", return_value=None))
        elapsed = clock.elapsed
        self.assertEqual(self._settle().action, "break")
        self.assertEqual(clock.elapsed, elapsed)
        self.rebuild.assert_called_once()
        self.agent._try_activate_fallback.assert_called_once()

    def test_redirect_arriving_during_wait_is_preserved(self):
        clock = self._clock(stop_after=0.2, redirect="synthetic correction")
        verdict = self._settle()
        self.assertEqual(verdict.action, "break")
        self.assertTrue(self.retry.restart_with_redirected_messages)
        self.assertEqual(self.agent._pending_redirect, "synthetic correction")
        self.assertLessEqual(clock.elapsed, 0.4)
        self.agent._persist_session.assert_not_called()

    def test_redirect_arriving_during_final_recovery_slice_is_preserved(self):
        clock = self._clock(stop_after=6.0, redirect="synthetic correction")
        verdict = self._settle()
        self.assertEqual(verdict.action, "break")
        self.assertTrue(self.retry.restart_with_redirected_messages)
        self.assertEqual(self.agent._pending_redirect, "synthetic correction")
        self.assertAlmostEqual(clock.elapsed, 6.0, delta=0.21)
        self.agent._persist_session.assert_not_called()

    def test_stop_clears_unconsumed_steer(self):
        self.agent._pending_steer = "synthetic steer"
        self.agent._interrupt_requested = True
        self._clock()
        self.assertEqual(self._settle().action, "return")
        self.assertIsNone(self.agent._pending_steer)


if __name__ == "__main__":
    unittest.main()
