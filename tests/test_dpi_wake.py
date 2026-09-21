"""DPI recovery across quick power cycles, slow wake and missing HID wake reports."""

import sys
import unittest
from unittest.mock import Mock, patch

from core import hid_gesture


def dpi_reply(value):
    return (0x11, 2, 0x14, 0x20, [0, value >> 8, value & 0xFF])


class DpiWakeTests(unittest.TestCase):
    def setUp(self):
        self.listener = hid_gesture.HidGestureListener()
        self.listener._dev = Mock()
        self.listener._connected = True
        self.listener._dpi_idx = 0x14
        self.listener._desired_dpi = 4000
        self.now = 100.0
        clock = patch.object(hid_gesture.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.hardware_dpi = 4000
        self.writes = []

        def request(index, function, params, **kwargs):
            if function == 3:
                self.hardware_dpi = (params[1] << 8) | params[2]
                self.writes.append(self.hardware_dpi)
            return dpi_reply(self.hardware_dpi)

        self.listener._request = Mock(side_effect=request)

    def move_and_check(self, elapsed=2.0):
        self.now += elapsed
        self.listener.notify_pointer_activity()
        self.listener._apply_wake_dpi_check()

    def test_quick_power_cycle_does_not_require_thirty_second_idle(self):
        self.move_and_check()
        self.hardware_dpi = 1000  # Three-second off/on cycle.
        self.move_and_check(3)
        self.assertEqual(self.hardware_dpi, 4000)
        self.assertEqual(self.writes, [4000])

    def test_late_reset_during_continuous_motion_is_recovered(self):
        for _ in range(10):
            self.move_and_check()
        self.hardware_dpi = 1000  # Reset well after the old three-check window.
        self.move_and_check()
        self.assertEqual(self.writes, [4000])

    def test_event_tap_never_sends_hid_and_checks_are_rate_limited(self):
        self.listener.notify_pointer_activity()
        self.listener._request.assert_not_called()
        self.listener._apply_wake_dpi_check()
        self.assertEqual(self.listener._request.call_count, 1)
        for _ in range(1000):
            self.move_and_check(0.001)
        self.assertEqual(self.listener._request.call_count, 1)
        self.move_and_check(1.1)
        self.assertEqual(self.listener._request.call_count, 2)
        self.assertEqual(self.writes, [])

    def test_idle_does_not_poll_and_stale_pending_activity_expires(self):
        self.move_and_check()
        self.now += 60
        self.listener._apply_wake_dpi_check()
        self.assertEqual(self.listener._request.call_count, 1)
        self.listener.notify_pointer_activity()
        self.now += 4  # A busy listener must discard this stale signal.
        self.listener._apply_wake_dpi_check()
        self.assertEqual(self.listener._request.call_count, 1)
        self.assertFalse(self.listener._dpi_check_requested.is_set())

    def test_read_timeout_does_not_prevent_a_successful_restore_write(self):
        self.listener._request = Mock(side_effect=[None, dpi_reply(4000), dpi_reply(4000)])
        self.move_and_check()
        self.assertEqual([c.args[1] for c in self.listener._request.call_args_list], [2, 3, 2])
        self.assertFalse(self.listener._dpi_recovery_pending)
        for call in self.listener._request.call_args_list:
            self.assertEqual(call.kwargs['timeout_ms'], 500)

    def test_failed_checks_can_retry_after_three_attempts_without_idle_gap(self):
        request = self.listener._request.side_effect
        self.listener._request.side_effect = lambda *a, **kw: None
        for _ in range(5):
            self.move_and_check()
        self.assertTrue(self.listener._dpi_recovery_pending)
        self.listener._request.side_effect = request
        self.hardware_dpi = 1000
        self.move_and_check()
        self.assertEqual(self.writes, [4000])
        self.assertFalse(self.listener._dpi_recovery_pending)

    def test_manual_change_during_read_uses_latest_intent(self):
        request = self.listener._request.side_effect

        def change_during_read(*args, **kwargs):
            self.listener._desired_dpi = 2400
            return request(*args, **kwargs)

        self.listener._request.side_effect = change_during_read
        self.move_and_check()
        self.assertEqual(self.writes, [2400])

    def test_manual_command_arriving_during_read_takes_precedence(self):
        def manual_command(*args, **kwargs):
            self.listener._desired_dpi = 2400
            self.listener._pending_dpi = 2400
            return dpi_reply(1000)

        self.listener._request.side_effect = manual_command
        self.move_and_check()
        self.assertEqual(self.listener._request.call_count, 1)
        self.assertEqual(self.listener._pending_dpi, 2400)

    def test_check_does_not_use_or_corrupt_manual_command_mailbox(self):
        listener = self.listener
        listener._pending_dpi = "read"
        self.move_and_check()
        listener._request.assert_not_called()
        listener._pending_dpi = None
        listener._dpi_result = 1600
        self.move_and_check()
        self.assertEqual(listener._dpi_result, 1600)
        self.assertFalse(listener._dpi_event.is_set())

    def test_disconnect_cancels_checks_but_preserves_intent(self):
        listener = self.listener
        listener.notify_pointer_activity()
        listener._drain_pending_requests()
        self.assertFalse(listener._dpi_check_requested.is_set())
        self.assertEqual(listener._desired_dpi, 4000)
        listener.notify_pointer_activity()
        self.assertTrue(listener._dpi_check_requested.is_set())

    def test_set_dpi_records_intent_even_if_mouse_is_asleep(self):
        self.listener._dpi_event = Mock()
        self.listener._dpi_event.wait.return_value = False
        self.assertFalse(self.listener.set_dpi(2400))
        self.assertEqual(self.listener._desired_dpi, 2400)

    def test_unknown_intent_or_unsupported_device_does_no_io(self):
        self.listener._desired_dpi = None
        self.move_and_check()
        self.listener._desired_dpi = 4000
        self.listener._dpi_idx = None
        self.move_and_check()
        self.listener._request.assert_not_called()

    def test_listener_resumes_after_timeouts_on_pointer_motion_without_hid_report(self):
        listener = self.listener
        listener._running = True
        listener._try_connect = Mock(return_value=True)
        listener._undivert = Mock()
        listener._on_connect = Mock()
        listener._on_disconnect = Mock()
        request = listener._request.side_effect
        attempts = 0

        def slow_wake(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= 4:
                listener._consecutive_request_timeouts += 1
                return None
            listener._consecutive_request_timeouts = 0
            return request(*args, **kwargs)

        listener._request.side_effect = slow_wake
        self.hardware_dpi = 1000
        listener.notify_pointer_activity()
        iterations = 0

        def receive(timeout):
            nonlocal iterations
            iterations += 1
            self.now += 1
            listener.notify_pointer_activity()
            if self.writes or iterations > 20:
                listener._running = False
            return None  # No proprietary wake report, only OS pointer activity.

        listener._rx = receive
        listener._main_loop()
        self.assertEqual(self.writes, [4000])
        self.assertEqual(listener._on_connect.call_count, 2)
        self.assertTrue(listener._on_disconnect.called)
        self.assertLess(iterations, 20)


@unittest.skipUnless(sys.platform == "darwin", "macOS event tap")
class MacOSDpiWakeSignalTests(unittest.TestCase):
    def test_only_hardware_events_signal_even_while_hid_is_asleep(self):
        from core import mouse_hook_macos

        hook = mouse_hook_macos.MouseHook()
        hook._hid_gesture = Mock()
        quartz = mouse_hook_macos.Quartz
        event = object()
        for connected in (True, False):
            hook._should_intercept_events = Mock(return_value=connected)
            for pid, expected in ((0, 1), (123, 0)):
                hook._hid_gesture.reset_mock()

                def field_value(event, field):
                    return pid if field == quartz.kCGEventSourceUnixProcessID else 0

                with patch.object(quartz, "CGEventGetIntegerValueField", side_effect=field_value):
                    result = hook._event_tap_callback(None, quartz.kCGEventMouseMoved, event, None)
                self.assertIs(result, event)
                self.assertEqual(hook._hid_gesture.notify_pointer_activity.call_count, expected)


if __name__ == "__main__":
    unittest.main()
