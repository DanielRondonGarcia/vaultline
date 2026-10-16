import unittest

from src.worker_agent.infrastructure.adapters.runtime_watchdog import RuntimeWatchdog


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class RuntimeWatchdogTests(unittest.TestCase):
    def test_no_limits_means_no_verdict_regardless_of_elapsed_time(self):
        clock = FakeClock()
        watchdog = RuntimeWatchdog(None, None, clock=clock)
        for hours in (1, 12, 48):
            clock.now = hours * 3600.0
            self.assertIsNone(watchdog.verdict())
        self.assertEqual(watchdog.poll_interval(0.5), 0.5)

    def test_output_resets_the_idle_deadline_and_silence_eventually_fires(self):
        clock = FakeClock()
        watchdog = RuntimeWatchdog(None, 900.0, clock=clock)
        for minute in range(1, 61):
            clock.now = minute * 600.0
            watchdog.record_activity()
            self.assertIsNone(watchdog.verdict(), minute)
        clock.now += 899.0
        self.assertIsNone(watchdog.verdict())
        clock.now += 1.0
        self.assertEqual(watchdog.verdict(), RuntimeWatchdog.IDLE)
        self.assertEqual(watchdog.describe(RuntimeWatchdog.IDLE, "runtime"), "runtime produced no output for 900 seconds")

    def test_wall_clock_fires_even_while_output_flows(self):
        clock = FakeClock()
        watchdog = RuntimeWatchdog(1800.0, 900.0, clock=clock)
        clock.now = 1799.0
        watchdog.record_activity()
        self.assertIsNone(watchdog.verdict())
        clock.now = 1800.0
        watchdog.record_activity()
        self.assertEqual(watchdog.verdict(), RuntimeWatchdog.TIMEOUT)
        self.assertEqual(watchdog.describe(RuntimeWatchdog.TIMEOUT, "runtime"), "runtime timed out after 1800 seconds")

    def test_cancellation_wins_and_callback_errors_are_ignored(self):
        clock = FakeClock()
        answers = iter((False, True))
        watchdog = RuntimeWatchdog(1.0, 1.0, cancel_check=lambda: next(answers), clock=clock)
        self.assertIsNone(watchdog.verdict())
        clock.now = 5.0
        self.assertEqual(watchdog.verdict(), RuntimeWatchdog.CANCELED)
        broken = RuntimeWatchdog(None, None, cancel_check=lambda: 1 / 0, clock=clock)
        self.assertIsNone(broken.verdict())

    def test_poll_interval_never_overshoots_the_nearest_deadline_or_drops_to_zero(self):
        clock = FakeClock()
        watchdog = RuntimeWatchdog(10.0, 3.0, clock=clock)
        self.assertIsNone(watchdog.verdict())
        self.assertEqual(watchdog.poll_interval(0.5), 0.5)
        clock.now = 2.8
        self.assertIsNone(watchdog.verdict())
        self.assertAlmostEqual(watchdog.poll_interval(0.5), 0.2)
        clock.now = 2.999
        self.assertIsNone(watchdog.verdict())
        self.assertEqual(watchdog.poll_interval(0.5), RuntimeWatchdog.MIN_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    unittest.main()
