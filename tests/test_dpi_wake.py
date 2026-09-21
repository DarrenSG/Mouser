"""Regression tests for Bolt mouse sleep without a receiver disconnect."""

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
        self.hardware_dpi = 1000
        self.writes = []

        def request(index, function, params):
            if function == 3:
                self.hardware_dpi = (params[1] << 8) | params[2]
                self.writes.append(self.hardware_dpi)
            return dpi_reply(self.hardware_dpi)

        self.listener._request = Mock(side_effect=request)

    def advance_to_check(self):
        self.now = self.listener._dpi_wake_due
        self.listener._apply_wake_dpi_check()

    def test_idle_wake_restores_and_catches_late_firmware_reset(self):
        listener = self.listener
        listener.notify_pointer_activity()
        listener._apply_wake_dpi_check()
        listener._request.assert_not_called()  # Neither tap nor early loop does USB.
        self.advance_to_check()
        self.assertEqual(self.hardware_dpi, 4000)
        self.advance_to_check()
        self.assertEqual(self.writes, [4000])  # No write when already correct.
        self.hardware_dpi = 1000  # Firmware resets again after the first check.
        self.advance_to_check()
        self.assertEqual(self.hardware_dpi, 4000)
        self.assertEqual(self.writes, [4000, 4000])
        self.assertIsNone(listener._dpi_wake_due)
        calls = listener._request.call_count
        self.now += 600
        listener._apply_wake_dpi_check()
        self.assertEqual(listener._request.call_count, calls)  # No idle polling.

    def test_continuous_movement_does_not_rearm_checks(self):
        listener = self.listener
        listener.notify_pointer_activity()
        for _ in range(3):
            self.advance_to_check()
        for _ in range(600):
            self.now += 1
            listener.notify_pointer_activity()
        self.assertIsNone(listener._dpi_wake_due)
        self.now += listener.DPI_WAKE_IDLE_SECONDS
        listener.notify_pointer_activity()
        self.assertIsNotNone(listener._dpi_wake_due)

    def test_timeout_retries_are_bounded_and_do_not_guess_hardware_state(self):
        listener = self.listener
        listener._request = Mock(return_value=None)
        listener.notify_pointer_activity()
        for _ in range(3):
            self.advance_to_check()
        self.assertEqual(listener._request.call_count, 3)
        self.assertEqual(self.writes, [])
        self.assertIsNone(listener._dpi_wake_due)

    def test_manual_change_during_read_uses_latest_intent(self):
        listener = self.listener
        request = listener._request.side_effect

        def change_during_read(index, function, params):
            listener._desired_dpi = 2400
            return request(index, function, params)

        listener._request.side_effect = change_during_read
        listener.notify_pointer_activity()
        self.advance_to_check()
        self.assertEqual(self.writes, [2400])

    def test_manual_command_arriving_during_read_takes_precedence(self):
        listener = self.listener

        def manual_command(*args):
            listener._desired_dpi = 2400
            listener._pending_dpi = 2400
            return dpi_reply(1000)

        listener._request.side_effect = manual_command
        listener.notify_pointer_activity()
        self.advance_to_check()
        self.assertEqual(listener._request.call_count, 1)
        self.assertEqual(listener._pending_dpi, 2400)

    def test_check_does_not_use_or_corrupt_manual_command_mailbox(self):
        listener = self.listener
        listener.notify_pointer_activity()
        listener._pending_dpi = "read"
        self.advance_to_check()
        listener._request.assert_not_called()
        listener._pending_dpi = None
        listener._dpi_result = 1600
        self.advance_to_check()
        self.assertEqual(listener._dpi_result, 1600)
        self.assertFalse(listener._dpi_event.is_set())

    def test_disconnect_cancels_checks_but_preserves_intent(self):
        listener = self.listener
        listener.notify_pointer_activity()
        listener._drain_pending_requests()
        self.assertIsNone(listener._dpi_wake_due)
        self.assertEqual(listener._desired_dpi, 4000)
        listener.notify_pointer_activity()
        self.assertIsNotNone(listener._dpi_wake_due)

    def test_set_dpi_records_intent_even_if_mouse_is_asleep(self):
        listener = self.listener
        listener._dpi_event = Mock()
        listener._dpi_event.wait.return_value = False
        self.assertFalse(listener.set_dpi(2400))
        self.assertEqual(listener._desired_dpi, 2400)

    def test_unknown_intent_or_unsupported_device_does_no_io(self):
        listener = self.listener
        listener._desired_dpi = None
        listener.notify_pointer_activity()
        self.assertIsNone(listener._dpi_wake_due)
        listener._desired_dpi = 4000
        self.now += 60
        listener.notify_pointer_activity()
        listener._dpi_idx = None
        self.advance_to_check()
        listener._request.assert_not_called()
        self.assertIsNone(listener._dpi_wake_due)

    def test_listener_loop_services_armed_check(self):
        listener = self.listener
        listener._running = True
        listener._try_connect = Mock(return_value=True)
        listener._undivert = Mock()
        listener.notify_pointer_activity()
        self.now += 1

        def receive(timeout):
            listener._running = False
            return None

        listener._rx = receive
        listener._main_loop()
        self.assertEqual(self.writes, [4000])


@unittest.skipUnless(sys.platform == "darwin", "macOS event tap")
class MacOSDpiWakeSignalTests(unittest.TestCase):
    def test_only_hardware_events_signal_listener_and_always_pass_through(self):
        from core import mouse_hook_macos

        hook = mouse_hook_macos.MouseHook()
        hook._should_intercept_events = Mock(return_value=True)
        hook._hid_gesture = Mock()
        quartz = mouse_hook_macos.Quartz
        event = object()
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
