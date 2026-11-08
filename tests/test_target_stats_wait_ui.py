import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "index.html"


def _extract(source, pattern, label):
    match = re.search(pattern, source, re.S)
    if not match:
        raise AssertionError(f"UI source does not contain a {label!r} block matching {pattern}")
    return match.group(0)


def _run_node(script):
    # The combined script exceeds the Windows command-line limit; run from a temp file.
    with tempfile.TemporaryDirectory(prefix="target-stats-wait-ui-check-") as tmp:
        script_path = Path(tmp) / "check.js"
        script_path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            ["node", str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )
    if result.returncode != 0:
        raise AssertionError(f"node check failed:\nSTDERR:{result.stderr}\nSTDOUT:{result.stdout}")
    return result


class TargetStatsWaitUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not UI_PATH.exists():
            raise unittest.SkipTest(f"UI file not found: {UI_PATH}")
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def _extract(self, pattern, label):
        return _extract(self.source, pattern, label)
    # ------------------------------------------------------------------
    # No frontend deadline: the UI observes the existing background job
    # while the modal is open, until a terminal status or a network
    # failure. Closing stops only the local observation.
    # ------------------------------------------------------------------

    def test_no_frontend_deadline_remains(self):
        self.assertNotIn("TARGET_STATS_POLL_MAX_MS", self.source)
        self.assertNotRegex(self.source, r"request\.deadlineTimer\s*=\s*setTimeout")
        self.assertNotIn("La consulta de estadísticas tardó demasiado.", self.source)
        self.assertNotIn("target-stats-poll-timeout", self.source)
        self.assertNotIn("target-stats-wait-timeout", self.source)
        markup = self._extract(
            r"function targetStatsNoticeMarkup\(notice\) \{.*?\n    \}",
            "targetStatsNoticeMarkup",
        )
        self.assertNotIn('"info"', markup)

    def test_wait_stays_pending_after_ten_minutes_of_fake_elapsed_time(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        wait = self._extract(
            r"function waitForTargetStatsJob\(request, initialJob\) \{.*?\n    \}",
            "waitForTargetStatsJob",
        )
        script = f'''
const TARGET_STATS_POLL_INTERVAL_MS = 2000;
const LOG_POLL_MAX_FAILURES = 3;
const LOG_POLL_FAILURE_WINDOW_MS = 30000;
let timers = [];
let timerSeq = 0;
const setTimeout = (fn, delay) => {{ const timer = {{ oneShot: true, fn, delay, seq: ++timerSeq }}; timers.push(timer); return timer; }};
const clearTimeout = timer => {{ timers = timers.filter(t => t !== timer); }};
const setInterval = fn => {{ const timer = {{ oneShot: false, fn, seq: ++timerSeq }}; timers.push(timer); return timer; }};
const clearInterval = timer => {{ timers = timers.filter(t => t !== timer); }};
const isCurrentTargetStatsRequest = () => true;
const mergeJobProjection = () => {{}};
const isTerminalJobStatus = status => status === "succeeded" || status === "failed";
const normalizeJobStatus = status => status;
const currentTargetStatsLiveState = () => {{}};
const connectJobEvents = (jobId, handlers) => {{ handlers.onFallback(); return () => {{}}; }};
const never = new Promise(() => {{}});
const apiGet = () => never;
const stopTargetStatsMonitoring = request => {{
  if (typeof request.eventSource === "function") request.eventSource();
  request.eventSource = null;
  if (request.pollTimer) clearInterval(request.pollTimer);
  request.pollTimer = null;
  if (request.deadlineTimer) clearTimeout(request.deadlineTimer);
  request.deadlineTimer = null;
}};
{wait}
const outcomes = [];
const request = {{
  targetId: "t-1",
  controller: {{ signal: {{ aborted: false }} }},
  eventSource: null,
  pollTimer: null,
  deadlineTimer: null,
  cancelWait: null,
  jobId: null,
}};
const promise = waitForTargetStatsJob(request, {{ id: "job-1", status: "running" }});
promise.then(
  value => outcomes.push({{ settled: "resolved", value }}),
  error => outcomes.push({{ settled: "rejected", code: error && error.code, message: error && error.message }}),
);
// Simulate 10 minutes of elapsed wall clock while the job is still running:
// every registered timer fires (intervals repeat for the full window), and
// the wait must still be observing the job, not settled by any deadline.
const intervalTimer = timers.find(timer => !timer.oneShot);
if (!intervalTimer) throw new Error("the job polling interval was not registered");
const oneShots = timers.filter(timer => timer.oneShot);
if (oneShots.length) {{
  throw new Error(`a frontend deadline timer still exists: ${{JSON.stringify(timers.filter(t => t.oneShot).map(t => t.delay))}}`);
}}
for (let tick = 0; tick < 300; tick += 1) intervalTimer.fn();
if (outcomes.length) throw new Error(`the wait settled after 10 fake minutes without a terminal job: ${{JSON.stringify(outcomes)}}`);
if (typeof request.eventSource !== "undefined" && request.eventSource === null) throw new Error("the job observation was dropped while the job was still running");
if (!request.cancelWait) throw new Error("closing the window lost its way to stop the local observation");
process.stdout.write(JSON.stringify({{ pending: outcomes.length === 0, ticks: 300 }}));
'''
        result = _run_node(script)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["pending"])

    # ------------------------------------------------------------------
    # Real job lifecycle through the extracted wait: terminal success
    # resolves, terminal failure rejects with the worker's own error, and
    # the local observation stops cleanly at settle.
    # ------------------------------------------------------------------

    def _target_stats_lifecycle_harness(self):
        wait = self._extract(
            r"function waitForTargetStatsJob\(request, initialJob\) \{.*?\n    \}",
            "waitForTargetStatsJob",
        )
        failure = self._extract(
            r"function targetStatsJobFailure\(job\) \{.*?\n    \}",
            "targetStatsJobFailure",
        )
        refresh = self._extract(
            r"async function refreshTargetStats\(existingRequest = null\) \{.*?\n    \}",
            "refreshTargetStats",
        )
        return failure + "\n" + wait + "\n" + refresh

    def test_terminal_success_resolves_and_reloads_persisted_record(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        lifecycle = self._target_stats_lifecycle_harness()
        script = f'''
const TARGET_STATS_POLL_INTERVAL_MS = 2000;
const LOG_POLL_MAX_FAILURES = 3;
const LOG_POLL_FAILURE_WINDOW_MS = 30000;
let timers = [];
let timerSeq = 0;
const setTimeout = (fn, delay) => {{ const timer = {{ oneShot: true, fn, delay, seq: ++timerSeq }}; timers.push(timer); return timer; }};
const clearTimeout = timer => {{ timers = timers.filter(t => t !== timer); }};
const setInterval = fn => {{ const timer = {{ oneShot: false, fn, seq: ++timerSeq }}; timers.push(timer); return timer; }};
const clearInterval = timer => {{ timers = timers.filter(t => t !== timer); }};
let targetStatsState = null;
const targetStatsCanUpdate = () => true;
const isCurrentTargetStatsRequest = request => Boolean(
  targetStatsState && !targetStatsState.closed && request && targetStatsState.request === request
  && String(targetStatsState.target.id) === request.targetId
  && !request.controller.signal.aborted
);
const isSnapshotAbortError = error => Boolean(error && error.name === "AbortError");
const finishTargetStatsRequest = request => {{
  if (targetStatsState && targetStatsState.request === request) targetStatsState.request = null;
}};
const renders = [];
const liveStates = [];
const recordLoads = [];
const observedJobs = [];
const renderTargetStatsBody = () => renders.push(JSON.parse(JSON.stringify(targetStatsState.notice)));
const currentTargetStatsLiveState = value => {{ liveStates.push(String(value)); }};
const loadTargetStatsRecord = async (request, options) => {{ recordLoads.push(options); }};
const apiCalls = [];
const apiPost = async (...args) => {{ apiCalls.push(args); return {{ id: "job-9", status: "running" }}; }};
const apiGet = async (...args) => {{ apiCalls.push(args); return {{}}; }};
const mergeJobProjection = job => {{ observedJobs.push(job && job.status); }};
const isTerminalJobStatus = status => ["succeeded", "failed", "error", "canceled", "cancelled"].includes(status);
const normalizeJobStatus = status => status;
let deliveredJob = null;
const connectJobEvents = (jobId, handlers) => {{
  if (deliveredJob) handlers.onJob(deliveredJob);
  return () => {{}};
}};
const stopTargetStatsMonitoring = request => {{
  if (typeof request.eventSource === "function") request.eventSource();
  request.eventSource = null;
  if (request.pollTimer) clearInterval(request.pollTimer);
  request.pollTimer = null;
  if (request.deadlineTimer) clearTimeout(request.deadlineTimer);
  request.deadlineTimer = null;
}};
''' + lifecycle + f'''
targetStatsState = {{
  closed: false,
  target: {{ id: "t-1", name: "t" }},
  overlay: null,
  record: null,
  request: null,
  loading: true,
  syncing: false,
  notice: null,
  liveState: "En vivo",
}};
const request = {{
  targetId: "t-1",
  controller: {{ signal: {{ aborted: false }} }},
  eventSource: null,
  pollTimer: null,
  deadlineTimer: null,
  cancelWait: null,
  jobId: null,
}};
targetStatsState.request = request;
deliveredJob = {{ id: "job-9", status: "succeeded", result_summary: {{}} }};
await refreshTargetStats(request);
const notice = renders.find(item => item && item.label === "Guardado");
if (!notice) throw new Error("success flow lost the Guardado notice");
if (notice.kind !== "loading") throw new Error("success notice kind changed");
if (!notice.message.includes("Job exitoso")) throw new Error("success notice lost its message");
if (recordLoads.length !== 1) throw new Error("success flow did not reload the persisted record");
if (recordLoads[0].autoRefreshMissing !== false) throw new Error("post-success record reload changed options");
if (!observedJobs.includes("succeeded")) throw new Error("the successful job was not observed");
process.stdout.write("terminal success preserved");
'''
        result = _run_node(script)
        self.assertEqual(result.stdout, "terminal success preserved")

    def test_terminal_failure_rejects_with_worker_error_and_error_notice(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        lifecycle = self._target_stats_lifecycle_harness()
        script = f'''
const TARGET_STATS_POLL_INTERVAL_MS = 2000;
const LOG_POLL_MAX_FAILURES = 3;
const LOG_POLL_FAILURE_WINDOW_MS = 30000;
let timers = [];
let timerSeq = 0;
const setTimeout = (fn, delay) => {{ const timer = {{ oneShot: true, fn, delay, seq: ++timerSeq }}; timers.push(timer); return timer; }};
const clearTimeout = timer => {{ timers = timers.filter(t => t !== timer); }};
const setInterval = fn => {{ const timer = {{ oneShot: false, fn, seq: ++timerSeq }}; timers.push(timer); return timer; }};
const clearInterval = timer => {{ timers = timers.filter(t => t !== timer); }};
let targetStatsState = null;
const targetStatsCanUpdate = () => true;
const isCurrentTargetStatsRequest = request => Boolean(
  targetStatsState && !targetStatsState.closed && request && targetStatsState.request === request
  && !request.controller.signal.aborted
);
const isSnapshotAbortError = error => Boolean(error && error.name === "AbortError");
const finishTargetStatsRequest = request => {{
  if (targetStatsState && targetStatsState.request === request) targetStatsState.request = null;
}};
const renders = [];
const liveStates = [];
const recordLoads = [];
const observedJobs = [];
const renderTargetStatsBody = () => renders.push(JSON.parse(JSON.stringify(targetStatsState.notice)));
const currentTargetStatsLiveState = () => {{}};
const loadTargetStatsRecord = async (request, options) => {{ recordLoads.push(options); }};
const apiCalls = [];
const apiPost = async (...args) => {{ apiCalls.push(args); return {{ id: "job-7", status: "running" }}; }};
const apiGet = async (...args) => {{ apiCalls.push(args); return {{}}; }};
const mergeJobProjection = job => {{ observedJobs.push(job && job.status); }};
const isTerminalJobStatus = status => ["succeeded", "failed", "error", "canceled", "cancelled"].includes(status);
const normalizeJobStatus = status => status;
let deliveredJob = null;
const connectJobEvents = (jobId, handlers) => {{
  if (deliveredJob) handlers.onJob(deliveredJob);
  return () => {{}};
}};
const stopTargetStatsMonitoring = request => {{
  if (typeof request.eventSource === "function") request.eventSource();
  request.eventSource = null;
  if (request.pollTimer) clearInterval(request.pollTimer);
  request.pollTimer = null;
  if (request.deadlineTimer) clearTimeout(request.deadlineTimer);
  request.deadlineTimer = null;
}};
''' + lifecycle + f'''
targetStatsState = {{
  closed: false,
  target: {{ id: "t-1", name: "t" }},
  overlay: null,
  record: null,
  request: null,
  loading: true,
  syncing: false,
  notice: null,
  liveState: "En vivo",
}};
const request = {{
  targetId: "t-1",
  controller: {{ signal: {{ aborted: false }} }},
  eventSource: null,
  pollTimer: null,
  deadlineTimer: null,
  cancelWait: null,
  jobId: null,
}};
targetStatsState.request = request;
deliveredJob = {{ id: "job-7", status: "failed", error: "restic boom", result_summary: {{}} }};
await refreshTargetStats(request);
const notice = renders[renders.length - 1];
if (!notice || notice.kind !== "error") throw new Error(`job failure was rendered as ${{notice && notice.kind}}`);
if ((notice.label || "") !== "Error") throw new Error("job failure lost the Error label");
if (!notice.message.includes("restic boom")) throw new Error(`job failure lost the worker detail: ${{notice.message}}`);
if (targetStatsState.loading !== false) throw new Error("loading stayed stuck after a failed job");
if (recordLoads.length) throw new Error("a failed job must not reload the persisted record");
process.stdout.write("terminal failure still is an error");
'''
        result = _run_node(script)
        self.assertEqual(result.stdout, "terminal failure still is an error")
