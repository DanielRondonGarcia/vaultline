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
    # Long-running guidance: explicit truth that the calculation may take
    # minutes and that closing the window does not cancel the background
    # calculation.
    # ------------------------------------------------------------------

    def test_long_running_guidance_copy_explains_minutes_and_close_and_return(self):
        for marker in (
            "El cálculo de estadísticas puede tardar varios minutos.",
            "Puedes cerrar esta ventana y volver más tarde",
            "el cálculo continúa en segundo plano sin cancelarse",
        ):
            self.assertIn(marker, self.source)

    def test_refresh_notice_mentions_several_minutes_and_returning_later(self):
        refresh_block = re.search(
            r"async function refreshTargetStats\(existingRequest = null\) \{.*?\n    \}",
            self.source,
            re.S,
        )
        self.assertIsNotNone(refresh_block)
        self.assertIn("Calculando raw-data y blobs-per-file con Restic", refresh_block.group(0))
        self.assertIn('label: "Actualizando"', refresh_block.group(0))
        self.assertIn("puedes cerrar esta ventana y volver más tarde sin cancelar el cálculo", refresh_block.group(0))

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

    # ------------------------------------------------------------------
    # Notice rendering: real errors keep the alert styling.
    # ------------------------------------------------------------------

    def test_notice_markup_renders_error_alert_and_loading_status(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        markup = self._extract(
            r"function targetStatsNoticeMarkup\(notice\) \{.*?\n    \}",
            "targetStatsNoticeMarkup",
        )
        script = r'''
const escapeHtml = value => String(value);
''' + markup + r'''
const error = targetStatsNoticeMarkup({ kind: "error", label: "Error", message: "boom" });
if (!error.includes('role="alert"')) throw new Error("real failures lost the alert role");
if (!error.includes('data-state="error"')) throw new Error("real failures lost the error state");
const loading = targetStatsNoticeMarkup({ kind: "loading", label: "Cargando", message: "m" });
if (!loading.includes('data-state="loading"')) throw new Error("loading notice state changed unexpectedly");
process.stdout.write("notice rendering passed");
'''
        result = _run_node(script)
        self.assertEqual(result.stdout, "notice rendering passed")

    # ------------------------------------------------------------------
    # Closing the modal stops only the local observation; the background
    # calculation is never cancelled through the API.
    # ------------------------------------------------------------------

    def test_closing_the_modal_makes_no_backend_call_and_stops_local_observation(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        close = self._extract(
            r"function closeTargetStatsModal\(\) \{.*?\n    \}",
            "closeTargetStatsModal",
        )
        script = f'''
let aborts = 0;
let cancelWaits = 0;
let focused = false;
let monitored = 0;
let closedAtClose = null;
const stopTargetStatsMonitoring = request => {{ monitored += 1; if (request.pollTimer) request.pollTimer = null; if (request.deadlineTimer) request.deadlineTimer = null; }};
let targetStatsState = null;
const apiCalls = [];
const apiGet = async (...args) => {{ apiCalls.push(args); return {{}}; }};
const apiPost = async (...args) => {{ apiCalls.push(args); return {{}}; }};
const apiDelete = async (...args) => {{ apiCalls.push(args); return {{}}; }};
const document = {{ contains: () => true }};
''' + close + f'''
const request = {{
  targetId: "t-1",
  controller: {{ signal: {{ aborted: false }}, abort() {{ aborts += 1; }} }},
  cancelWait() {{ cancelWaits += 1; closedAtClose = targetStatsState && targetStatsState.closed; }},
  pollTimer: "pending",
  deadlineTimer: "pending",
}};
const overlay = {{
  classList: {{ remove() {{}} }},
  innerHTML: "",
  onclick: null,
  onkeydown: null,
}};
targetStatsState = {{ closed: false, request, overlay, returnFocus: {{ focus() {{ focused = true; }} }} }};
closeTargetStatsModal();
if (!closedAtClose) throw new Error("closing did not flag the state as closed");
if (targetStatsState !== null) throw new Error("closing did not clear the module state");
if (aborts !== 1) throw new Error("closing did not abort the local wait");
if (cancelWaits !== 1) throw new Error("closing did not cancel the local wait promise");
if (monitored !== 1) throw new Error("closing did not stop the local poll monitoring");
if (request.pollTimer !== null || request.deadlineTimer !== null) throw new Error("closing left local timers alive");
if (apiCalls.length) throw new Error(`closing made a backend call (would cancel the background job): ${{JSON.stringify(apiCalls)}}`);
if (!focused) throw new Error("closing did not restore focus");
process.stdout.write("close passed");
'''
        result = _run_node(script)
        self.assertEqual(result.stdout, "close passed")

    # ------------------------------------------------------------------
    # Loading state keeps the long-running guidance visible.
    # ------------------------------------------------------------------

    def test_loading_body_shows_long_running_guidance(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        hint = _extract(self.source, r"const TARGET_STATS_LONG_WAIT_HINT = `.*?`;", "hint")
        markup = self._extract(
            r"function targetStatsNoticeMarkup\(notice\) \{.*?\n    \}",
            "targetStatsNoticeMarkup",
        )
        body_fn = self._extract(
            r"function renderTargetStatsBody\(\) \{.*?\n    \}",
            "renderTargetStatsBody",
        )
        script = f'''
const TARGET_STATS_MODES = ["raw-data", "blobs-per-file"];
const escapeHtml = value => String(value);
const fmtDate = value => "No disponible";
const normalizeTargetStatsRecord = () => null;
const renderTargetStatsMode = () => "";
const targetStatsCanUpdate = () => true;
''' + hint + "\n" + markup + "\n" + body_fn + f'''
let bodyEl = {{ ariaBusy: null, innerHTML: "", setAttribute(name, value) {{ if (name === "aria-busy") this.ariaBusy = value; }} }};
let refreshEl = {{ hidden: true, disabled: true, ariaBusy: null, textContent: "", setAttribute(name, value) {{ if (name === "aria-busy") this.ariaBusy = value; }} }};
let targetStatsState = {{
  overlay: {{ querySelector: selector => selector === "#targetStatsBody" ? bodyEl : (selector === "#targetStatsRefresh" ? refreshEl : null) }},
  loading: true,
  notice: {{ kind: "loading", label: "Actualizando", message: "Calculando..." }},
  liveState: "En vivo",
}};
renderTargetStatsBody();
for (const marker of [
  "puede tardar varios minutos",
  "cerrar esta ventana y volver más tarde",
  "continúa en segundo plano sin cancelarse",
]) {{
  if (!bodyEl.innerHTML.includes(marker)) throw new Error(`long-running guidance missing: ${{marker}}`);
}}
if (bodyEl.ariaBusy !== "true") throw new Error("loading body lost its busy state");
process.stdout.write("loading guidance passed");
'''
        result = _run_node(script)
        self.assertEqual(result.stdout, "loading guidance passed")


if __name__ == "__main__":
    unittest.main()
