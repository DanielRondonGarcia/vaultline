"""Dashboard read-model UI tests (dashboard-read-models T3, specs S1-S4).

These tests run the shipped Control Plane UI JavaScript
(src/control_plane/ui/index.html) directly inside Node, with controlled
promises and fake timers:

- dashboard components (recent jobs, stats snapshot, targets snapshot,
  workers) apply independently as their own requests resolve, without
  await-all gates;
- a warming-up ``503 read_model_warming_up`` is never treated as empty-data
  success: the UI shows the Spanish preparing copy and retries automatically
  per ``Retry-After`` on one coalesced, bounded timer (no browser reload);
- last-good data and freshness are retained on transient errors;
- freshness ``X-Read-Model-*`` headers are captured separately from payloads
  by an optional apiGet hook, without appending fields to the stats body;
- targets polls coalesce outstanding requests, refresh header metadata even
  when the target fingerprint did not change, and all dashboard-only timers
  stop on navigation/unload.

Legacy behavior (API helper role/error handling, target polling projection
semantics, loaders) is covered by the shipped cases exercised in
tests/test_ui_states.py and tests/test_ui_version_updates.py.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "src" / "control_plane" / "ui" / "index.html"
QUICKSTART_PATH = ROOT / "doc" / "control-plane-quickstart.md"

READ_MODEL_BLOCK_PATTERN = (
    r"const READ_MODEL_WARMUP_CODE = .*?function stopReadModelFreshnessTicker\(\) \{.*?\n    \}"
)


def extract_block(pattern, label=""):
    """Extract real shipped helpers from index.html so tests run the UI code."""
    source = UI_PATH.read_text(encoding="utf-8")
    match = re.search(pattern, source, re.S)
    if not match:
        raise AssertionError(f"{label}: pattern not found in index.html: {pattern}")
    return match.group(0).strip()


def run_node_eval(script):
    return subprocess.run(
        ["node", "--input-type=commonjs", "--eval", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )


def require_node(test_case):
    if not shutil.which("node"):
        test_case.skipTest("Node.js is not installed")


# Shared Node harness: fake timers (setTimeout callbacks are captured for
# deterministic manual firing), controlled promises, and a fake apiGet that
# emulates the shipped apiGet contract (the metadata hook fires, when the
# caller supplied one, before the response body settles).
HARNESS_PRELUDE = r'''
const realSetTimeout = globalThis.setTimeout.bind(globalThis);
const scheduledTimers = [];
let nextTimerHandle = 1;
globalThis.setTimeout = (fn, ms, ...extra) => {
  scheduledTimers.push({ handle: nextTimerHandle, fn, ms });
  return nextTimerHandle++;
};
globalThis.clearTimeout = handle => {
  const index = scheduledTimers.findIndex(timer => timer.handle === handle);
  if (index >= 0) scheduledTimers.splice(index, 1);
  return undefined;
};
globalThis.setInterval = () => 9001;
globalThis.clearInterval = () => undefined;
const sleep = ms => new Promise(resolve => realSetTimeout(resolve, ms));
const pendingTimers = () => scheduledTimers.map(timer => ({ handle: timer.handle, fn: timer.fn, ms: timer.ms }));
const findTimer = ms => scheduledTimers.find(timer => timer.ms === ms) || null;
const fireTimer = timer => {
  const index = scheduledTimers.findIndex(entry => entry.handle === timer.handle);
  if (index >= 0) scheduledTimers.splice(index, 1);
  timer.fn();
};
const controlled = () => {
  let resolveResult;
  let rejectResult;
  const promise = new Promise((resolve, reject) => { resolveResult = resolve; rejectResult = reject; });
  return { promise, resolve: resolveResult, reject: rejectResult };
};
'''

FAKE_API_GET = r'''
const calls = [];
let respond = () => Promise.resolve({ items: [] });
const apiGet = (path, options = {}) => {
  calls.push({ path, options });
  const result = respond(path, options);
  if (result && typeof result.then === "function") return result;   // simple promise for background paths
  // Emulates the shipped apiGet contract: read-model paths capture freshness
  // metadata through the path hook unless the caller supplied an explicit one.
  result.settleWith = outcome => {
    const hook = (options && typeof options.onResponseMeta === "function")
      ? options.onResponseMeta
      : (typeof captureReadModelMetaForPath === "function" ? captureReadModelMetaForPath(path) : null);
    if (outcome.meta && hook) hook(outcome.meta);
    if (outcome.ok) result.resolve(outcome.body);
    else result.reject(outcome.error || new Error("backend failure"));
  };
  return result.promise;
};
const warmupError = retryAfterMs => {
  const error = new Error("dashboard read model is warming up");
  error.status = 503;
  error.code = "read_model_warming_up";
  error.retryAfterMs = retryAfterMs; // informational only; Retry-After flows through the hook
  return error;
};
const genericError = status => {
  const error = new Error("unexpected backend failure");
  error.status = status;
  error.code = "";
  return error;
};
const freshReadModels = () => ({
  stats: { updatedAt: null, ageSeconds: null, observedAtMs: null, warmingUp: false, retryTimer: null, retryAfterMs: null, retryDelayMs: null, outstanding: null },
  targets: { updatedAt: null, ageSeconds: null, observedAtMs: null, warmingUp: false, retryTimer: null, retryAfterMs: null, retryDelayMs: null, outstanding: null },
});
'''

SHIPPED_EXTRACTS = (
    ("read_model_block", READ_MODEL_BLOCK_PATTERN),
    ("fetch_dashboard_jobs", r"async function fetchDashboardJobs\(\) \{.*?\n    \}"),
    ("apply_dashboard_jobs", r"function applyDashboardJobs\(dashboard\) \{.*?\n    \}"),
    ("merge_job_list", r"function mergeJobList\(items\) \{.*?\n    \}"),
    ("browser_tz", r"function browserTimezoneOffsetMinutes\(now = new Date\(\)\) \{.*?\n    \}"),
    ("state_block", r"const state = \{.*?\n    \};\n    let workersNotice = null;"),
    ("api_get", r"async function apiGet\(path, options = \{\}\) \{.*?\n    \}"),
    ("header_value_helper", r"function readModelHeaderValue\(resp, name\) \{.*?\n    \}"),
    ("retry_after_helper", r"function readModelRetryAfterMs\(resp\) \{.*?\n    \}"),
    ("target_fingerprint", r"function targetFingerprint\(target\) \{.*?\n    \}"),
    ("targets_polling_lets", r"let targetsPollTimer = null;\n    let targetsPolling = false;"),
    ("start_targets_polling", r"function startTargetsPolling\(\) \{.*?\n    \}\n\n    function stopTargetsPolling\(\) \{.*?\n    \}"),
    ("render_dashboard_block", r"function renderDashboard\(content\) \{.*?\n    \}"),
    ("stat_card", r"function statCard\(eyebrow, title, value, meta\) \{.*?\n    \}"),
    ("build_activity_days", r"function buildActivityDays\(byDay\) \{.*?\n    \}"),
    ("empty_job_stats", r"const EMPTY_JOB_STATS = \{.*?\};"),
    ("escape_html", r"function escapeHtml\(str\) \{.*?\n    \}"),
    ("fmt_date", r"function fmtDate\(ts\) \{.*?\n    \}"),
    ("nav_items", r"const NAV_ITEMS = \[.*?\n    \];"),
    ("load_page_data", r"function loadPageData\(view, force = false\) \{.*?\n    \}"),
    ("current_route_view", r"function currentRouteView\(\) \{.*?\n    \}"),
    ("should_fetch_jobs", r"function shouldFetchJobsDuringRefresh\(view\) \{.*?\n    \}"),
    ("route_block", r"function route\(\) \{.*?\n    \}"),
    ("refresh_dashboard_jobs", r"async function refreshDashboardJobs\(\) \{.*?\n    \}"),
    ("refresh_all_block", r"async function refreshAll\(\) \{.*?\n    \}"),
)

_EXTRACTS_CACHE = None


def shipped():
    global _EXTRACTS_CACHE
    if _EXTRACTS_CACHE is None:
        _EXTRACTS_CACHE = {label: extract_block(pattern, label) for label, pattern in SHIPPED_EXTRACTS}
    return _EXTRACTS_CACHE


def wrap_async(body):
    """Run a scenario body inside an async IIFE (CommonJS scripts cannot use
    top-level await)."""
    return "(async () => {\n" + body + "})().catch(error => { console.error(error); process.exitCode = 1; });\n"


class DashboardReadModelApiGetTests(unittest.TestCase):
    """The optional response-header metadata hook on apiGet captures snapshot
    freshness separately and never mutates API error/role behavior."""

    STATS_TARGETS_META_STATE = r'''
const state = { readModels: {
  stats: { updatedAt: null, ageSeconds: null, observedAtMs: null, warmingUp: false, retryTimer: null, retryAfterMs: null, retryDelayMs: null, outstanding: null },
  targets: { updatedAt: null, ageSeconds: null, observedAtMs: null, warmingUp: false, retryTimer: null, retryAfterMs: null, retryDelayMs: null, outstanding: null },
} };
'''

    def test_apiget_captures_freshness_headers_without_touching_payload(self):
        extracts = shipped()
        script = '"use strict";\n' + r'''
const window = { location: { href: "/start" } };
const body = { total: 42, by_status: { succeeded: 40, failed: 2 }, by_day: [] };
const makeResponse = (status, headers, payload) => ({
  status,
  statusText: "OK",
  ok: status >= 200 && status < 400,
  headers: { get: name => (name in headers ? headers[name] : null) },
  json: async () => payload,
});
const fetchCalls = [];
const makeTargetsBody = { items: [] };
globalThis.fetch = (path, init) => {
  fetchCalls.push({ path, init });
  const headerBag = path.startsWith("/api/v1/targets") ? {} : { "X-Read-Model-Updated-At": "2026-01-01T00:00:05Z", "X-Read-Model-Age-Seconds": "4" };
  return Promise.resolve(makeResponse(200, headerBag, path.startsWith("/api/v1/targets") ? { items: [] } : body));
};
''' + "\n" + extracts["header_value_helper"] + "\n" + extracts["retry_after_helper"] + "\n" + extracts["api_get"] + "\n" + self.STATS_TARGETS_META_STATE + wrap_async(r'''
  const metaCalls = [];
  const payload = await apiGet("/api/v1/jobs/stats?tz_offset=-300", { onResponseMeta: meta => metaCalls.push(meta) });
  const targetPayload = await apiGet("/api/v1/targets", { onResponseMeta: meta => metaCalls.push(meta) });
  process.stdout.write(JSON.stringify({
    statsMeta: metaCalls[0],
    targetMeta: metaCalls[1],
    metaCallCount: metaCalls.length,
    payloadUnchanged: JSON.stringify(payload) === JSON.stringify(body),
    payloadKeys: Object.keys(payload),
    targetPayload,
    fetchHookLeaked: fetchCalls.some(call => "onResponseMeta" in call.init),
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["metaCallCount"], 2)
        self.assertEqual(output["statsMeta"]["status"], 200)
        self.assertEqual(output["statsMeta"]["updatedAt"], "2026-01-01T00:00:05Z")
        self.assertEqual(output["statsMeta"]["ageSeconds"], 4)
        self.assertEqual(output["targetMeta"]["status"], 200)
        self.assertIsNone(output["targetMeta"]["updatedAt"], "a snapshot without freshness headers captures no fake metadata")
        self.assertTrue(output["payloadUnchanged"], "response bodies must pass through untouched")
        self.assertEqual(output["payloadKeys"], ["total", "by_status", "by_day"], "freshness must not be appended to the payload")
        self.assertEqual(output["targetPayload"], {"items": []})
        self.assertFalse(output["fetchHookLeaked"], "the hook must not leak into the fetch init")

    def test_apiget_reports_retry_after_and_warmup_code_on_503(self):
        extracts = shipped()
        script = '"use strict";\n' + r'''
const window = { location: { href: "/start" } };
globalThis.fetch = () => Promise.resolve({
  status: 503,
  statusText: "Service Unavailable",
  headers: { get: name => name === "Retry-After" ? "5" : null },
  json: async () => ({ error: "dashboard read model is warming up", code: "read_model_warming_up" }),
});
''' + "\n" + extracts["header_value_helper"] + "\n" + extracts["retry_after_helper"] + "\n" + extracts["api_get"] + "\n" + self.STATS_TARGETS_META_STATE + wrap_async(r'''
  const metaCalls = [];
  let thrown = null;
  try {
    await apiGet("/api/v1/targets", { onResponseMeta: meta => metaCalls.push(meta) });
  } catch (error) {
    thrown = { message: error.message, status: error.status, code: error.code };
  }
  process.stdout.write(JSON.stringify({ metaCalls, thrown }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(
            output["metaCalls"],
            [{"status": 503, "retryAfterMs": 5000}],
            "the 503 warm-up response must expose Retry-After through the hook",
        )
        self.assertEqual(output["thrown"]["status"], 503)
        self.assertEqual(output["thrown"]["code"], "read_model_warming_up")

    def test_apiget_keeps_401_redirect_403_role_gate_and_error_body(self):
        extracts = shipped()
        script = '"use strict";\n' + r'''
const window = { location: { href: "/start" } };
const responses = {
  "/api/v1/forbidden": { status: 403, statusText: "Forbidden", headers: { get: () => null }, json: async () => ({ error: "Solo administradores.", reason: "role", path: "/api/v1/secrets" }) },
  "/api/v1/expired": { status: 401, statusText: "Unauthorized", headers: { get: () => null }, json: async () => ({ error: "unauthorized" }) },
};
globalThis.fetch = path => Promise.resolve(responses[path]);
''' + "\n" + extracts["header_value_helper"] + "\n" + extracts["retry_after_helper"] + "\n" + extracts["api_get"] + "\n" + self.STATS_TARGETS_META_STATE + wrap_async(r'''
  const forbiddenMeta = [];
  const expiredMeta = [];
  const forbidden = await apiGet("/api/v1/forbidden", { onResponseMeta: meta => forbiddenMeta.push(meta) }).catch(error => ({
    message: error.message, status: error.status, code: error.code, reason: error.reason, path: error.path,
  }));
  const expired = await apiGet("/api/v1/expired", { onResponseMeta: meta => expiredMeta.push(meta) }).catch(error => ({ message: error.message }));
  process.stdout.write(JSON.stringify({ forbidden, expired, redirected: window.location.href, forbiddenMeta, expiredMeta }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        # 403/role gate: exact legacy error shape and no redirect.
        self.assertEqual(output["forbidden"]["status"], 403)
        self.assertEqual(output["forbidden"]["message"], "Solo administradores.")
        self.assertEqual(output["forbidden"]["reason"], "role")
        self.assertEqual(output["forbidden"]["path"], "/api/v1/secrets")
        self.assertEqual(output["forbiddenMeta"], [{"status": 403, "retryAfterMs": None}])
        # 401: legacy redirect behavior is untouched, and the hook never fires
        # on the auth short-circuit (no metadata from a redirected response).
        self.assertEqual(output["redirected"], "/login")
        self.assertEqual(output["expired"]["message"], "auth")
        self.assertEqual(output["expiredMeta"], [])


class DashboardReadModelMetaTests(unittest.TestCase):
    """Snapshot freshness capture, coalesced bounded warm-up retries and the
    view-scoped pausing of dashboard-only timers."""

    def meta_env(self):
        extracts = shipped()
        return (
            '"use strict";\n'
            + extracts["browser_tz"] + "\n"
            + extracts["fmt_date"] + "\n"
            + extracts["state_block"] + "\n"
            + extracts["read_model_block"]
        )

    def test_capture_freshness_meta_populates_and_clears_warming(self):
        script = self.meta_env() + wrap_async(r'''
  captureReadModelMeta("stats", { status: 503, retryAfterMs: 5000 });
  const warming = {
    warmingUp: state.readModels.stats.warmingUp,
    retryAfterMs: state.readModels.stats.retryAfterMs,
    updatedAt: state.readModels.stats.updatedAt,
  };
  captureReadModelMeta("stats", { status: 200, updatedAt: "2026-01-01T00:02:00Z", ageSeconds: 12 });
  captureReadModelMeta("targets", { status: 200, updatedAt: "2026-01-01T00:01:00Z", ageSeconds: 2 });
  process.stdout.write(JSON.stringify({
    warming,
    statsWarmingCleared: state.readModels.stats.warmingUp === false,
    statsUpdatedAt: state.readModels.stats.updatedAt,
    statsAge: state.readModels.stats.ageSeconds,
    statsFreshness: readModelFreshness("stats", true),
    targetsFreshness: readModelFreshness("targets", true),
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["warming"]["warmingUp"])
        self.assertEqual(output["warming"]["retryAfterMs"], 5000)
        self.assertIsNone(output["warming"]["updatedAt"], "a warm-up 503 must not fabricate snapshot metadata")
        self.assertTrue(output["statsWarmingCleared"], "a successful snapshot response clears the warming state")
        self.assertEqual(output["statsUpdatedAt"], "2026-01-01T00:02:00Z")
        self.assertEqual(output["statsAge"], 12)
        self.assertIn("Actualizado hace 12 s", output["statsFreshness"])
        self.assertIn("datos de", output["statsFreshness"])
        self.assertIn("Actualizado hace 2 s", output["targetsFreshness"])

    def test_capture_keeps_meta_on_role_gate_and_legacy_bodies(self):
        script = self.meta_env() + wrap_async(r'''
  captureReadModelMeta("stats", { status: 200, updatedAt: "2026-01-01T00:00:00Z", ageSeconds: 1 });
  const before = JSON.stringify({
    updatedAt: state.readModels.stats.updatedAt,
    ageSeconds: state.readModels.stats.ageSeconds,
    warmingUp: state.readModels.stats.warmingUp,
  });
  captureReadModelMeta("stats", { status: 403, retryAfterMs: 5000 });   // role gate keeps its metadata
  captureReadModelMeta("stats", { status: 200, updatedAt: null, ageSeconds: null }); // legacy body without headers
  captureReadModelMeta("targets", { status: 500 });
  const after = JSON.stringify({
    updatedAt: state.readModels.stats.updatedAt,
    ageSeconds: state.readModels.stats.ageSeconds,
    warmingUp: state.readModels.stats.warmingUp,
  });
  process.stdout.write(JSON.stringify({
    before, after,
    targetsTouched: state.readModels.targets.updatedAt !== null || state.readModels.targets.warmingUp,
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["before"], output["after"], "role gates and legacy bodies must not overwrite captured freshness")
        self.assertFalse(output["targetsTouched"])

    def test_dashboard_refreshes_snapshots_periodically_and_stops_outside_view(self):
        script = self.meta_env() + "\n" + HARNESS_PRELUDE + r'''
let tick = null;
globalThis.setInterval = fn => { tick = fn; return 9001; };
let clock = 100000;
Date.now = () => clock;
const document = { getElementById: () => null };
let statsRefreshes = 0;
let targetRefreshes = 0;
const refreshDashboardJobs = () => { statsRefreshes += 1; };
refreshTargetsSnapshotWithRetry = () => { targetRefreshes += 1; };
const targetsPolling = false;
''' + wrap_async(r'''
  state.currentView = "dashboard";
  startReadModelFreshnessTicker();
  clock += 5000;
  tick();
  if (statsRefreshes !== 1 || targetRefreshes !== 1) throw new Error("periodic tick did not refresh both snapshots");
  state.readModels.stats.outstanding = Promise.resolve();
  state.readModels.targets.retryTimer = 91;
  clock += 5000;
  tick();
  if (statsRefreshes !== 1 || targetRefreshes !== 1) throw new Error("periodic tick duplicated pending/retry work");
  state.readModels.stats.outstanding = null;
  state.readModels.targets.retryTimer = null;
  clock += 5000;
  tick();
  if (statsRefreshes !== 2 || targetRefreshes !== 2) throw new Error("successful first load disabled subsequent refreshes");
  state.currentView = "workers";
  clock += 5000;
  tick();
  if (statsRefreshes !== 2 || targetRefreshes !== 2) throw new Error("snapshots polled outside dashboard");
  stopReadModelFreshnessTicker();
  process.stdout.write("periodic snapshot refresh passed");
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "periodic snapshot refresh passed")

    def test_retry_timer_is_coalesced_bounded_and_view_paused(self):
        script = self.meta_env() + "\n" + HARNESS_PRELUDE + wrap_async(r'''
  state.currentView = "dashboard";
  let reloads = 0;
  const reload = () => { reloads += 1; };
  state.readModels.stats.retryAfterMs = 5000;
  scheduleReadModelRetry("stats", reload);
  scheduleReadModelRetry("stats", reload);   // duplicate while one timer is outstanding
  const statsScheduled = pendingTimers();
  if (JSON.stringify(statsScheduled.map(timer => timer.ms)) !== "[5000]") {
    throw new Error(`expected exactly one coalesced Retry-After timer, got: ${JSON.stringify(statsScheduled)}`);
  }
  state.readModels.targets.retryAfterMs = 3600000;   // Retry-After of one hour
  scheduleReadModelRetry("targets", reload);
  const boundedMs = pendingTimers().map(timer => timer.ms);

  const statsHandle = statsScheduled[0].handle;
  fireTimer(pendingTimers().find(timer => timer.handle === statsHandle));  // on dashboard: reloads
  fireTimer(pendingTimers().find(timer => timer.ms === 30000));            // targets retry fires too
  if (reloads !== 2) throw new Error(`each fired timer must reload exactly once, got ${reloads}`);

  state.readModels.stats.retryAfterMs = 5000;
  scheduleReadModelRetry("stats", reload);
  const pausedHandle = pendingTimers()[0].handle;
  state.currentView = "targets";                   // navigate away from the dashboard
  fireTimer(pendingTimers().find(timer => timer.handle === pausedHandle));
  if (reloads !== 2) throw new Error("stats retries must be paused outside the dashboard");
  stopReadModelRetryTimer("stats");
  if (pendingTimers().length !== 0) throw new Error(`timers not cleared: ${JSON.stringify(pendingTimers())}`);
  stopReadModelRetryTimer("stats");                // idempotent stop
  process.stdout.write(JSON.stringify({ boundedMs, reloads, cleared: pendingTimers().length === 0 }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["boundedMs"], [5000, 30000], "hour-long Retry-After is capped at 30s; the still-pending stats timer stays 5s")
        self.assertEqual(output["reloads"], 2)
        self.assertTrue(output["cleared"])


class DashboardStatsWidgetTests(unittest.TestCase):
    """Recent jobs and the stats snapshot apply independently; warm-up 503
    retries automatically without fake zeros; last-good survives errors."""

    def stats_env(self, state_fields):
        extracts = shipped()
        return '"use strict";\n' + r'''
let currentLogJobSnapshot = null;
const hasJobDetail = value => value !== null && value !== undefined;
const renderCalls = [];
const renderDashboard = () => { renderCalls.push(1); };
const contentElement = { innerHTML: "" };
const document = { getElementById: id => (id === "content" ? contentElement : null) };
''' + "\n" + extracts["browser_tz"] + "\n" + extracts["merge_job_list"] + "\n" + extracts["apply_dashboard_jobs"] + "\n" + extracts["read_model_block"] + "\n" + extracts["fetch_dashboard_jobs"] + "\n" + HARNESS_PRELUDE + "\n" + FAKE_API_GET + "\n" + r'''
const state = ''' + json.dumps(state_fields, ensure_ascii=False) + ''';
'''

    def test_recent_jobs_apply_while_stats_pending(self):
        script = self.stats_env({
            "workers": [], "targets": [], "jobs": [], "jobStats": None,
            "currentView": "dashboard", "storageProfiles": [], "readModels": None,
        }) + wrap_async(r'''
  let statsEntry = null;
  let jobsEntry = null;
  respond = (path, options) => {
    if (path.startsWith("/api/v1/jobs/stats?")) {
      statsEntry = { options, ...controlled() };
      return statsEntry;
    }
    if (path === "/api/v1/jobs?limit=5") {
      jobsEntry = { options, ...controlled() };
      return jobsEntry;
    }
    return Promise.resolve({ items: [] });
  };
  void fetchDashboardJobs();
  await sleep(5);
  if (!statsEntry || !jobsEntry) throw new Error(`expected one recent-jobs request and one stats request: ${JSON.stringify(calls.map(call => call.path))}`);
  if (JSON.stringify(statsEntry.options) !== "{}") throw new Error("the read-model read must call apiGet without call-site options; metadata capture is contained in the shipped apiGet path hook");
  jobsEntry.resolve({ items: [{ id: "quick-job" }] });
  await sleep(10);
  process.stdout.write(JSON.stringify({
    quickJobsApplied: state.jobs.length === 1 && state.jobs[0].id === "quick-job",
    statsStillPending: !state.jobStats,
    dashboardRenderedWhileStatsPending: renderCalls.length > 0,
    noFakeStatsZero: state.jobStats === null,
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["quickJobsApplied"], "the recent-jobs widget must apply while stats are still pending")
        self.assertTrue(output["statsStillPending"])
        self.assertTrue(output["dashboardRenderedWhileStatsPending"])
        self.assertTrue(output["noFakeStatsZero"], "a pending stats snapshot must never be treated as zero stats")

    def test_stats_apply_while_recent_jobs_pending_and_payload_stays_clean(self):
        script = self.stats_env({
            "workers": [], "targets": [], "jobs": [{ "id": "previous" }], "jobStats": None,
            "currentView": "dashboard", "storageProfiles": [], "readModels": None,
        }) + wrap_async(r'''
  const body = { total: 42, by_status: { succeeded: 40, failed: 2 }, by_day: [] };
  let statsEntry = null;
  respond = (path, options) => {
    if (path.startsWith("/api/v1/jobs/stats?")) {
      statsEntry = { options, ...controlled() };
      return statsEntry;
    }
    if (path === "/api/v1/jobs?limit=5") return controlled().promise; // recent jobs stay pending
    return Promise.resolve({ items: [] });
  };
  void fetchDashboardJobs();
  await sleep(5);
  statsEntry.settleWith({ ok: true, meta: { status: 200, updatedAt: "2026-01-01T00:00:05Z", ageSeconds: 4 }, body });
  await sleep(10);
  process.stdout.write(JSON.stringify({
    statsApplied: state.jobStats !== null && state.jobStats.total === 42,
    payloadKeys: Object.keys(state.jobStats || {}),
    freshnessCaptured: state.readModels.stats.updatedAt === "2026-01-01T00:00:05Z" && state.readModels.stats.ageSeconds === 4,
    jobsUntouchedByStatsApply: state.jobs.length === 1 && state.jobs[0].id === "previous",
    dashboardRendered: renderCalls.length > 0,
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["statsApplied"])
        self.assertEqual(output["payloadKeys"], ["total", "by_status", "by_day"], "freshness must not be appended to the stats payload")
        self.assertTrue(output["freshnessCaptured"])
        self.assertTrue(output["jobsUntouchedByStatsApply"], "applying stats must never reset the recent-jobs list")
        self.assertTrue(output["dashboardRendered"])

    def test_stats_warmup_retry_converges_without_reload_or_fake_zero(self):
        script = self.stats_env({
            "workers": [], "targets": [], "jobs": [], "jobStats": None,
            "currentView": "dashboard", "storageProfiles": [], "readModels": None,
        }) + wrap_async(r'''
  const statsEntries = [];
  respond = (path, options) => {
    if (!path.startsWith("/api/v1/jobs/stats?")) return Promise.resolve({ items: [] });
    if (path !== "/api/v1/jobs/stats?tz_offset=-300") throw new Error(`unexpected stats path: ${path}`);
    const entry = { options, ...controlled() };
    statsEntries.push(entry);
    return entry;
  };
  void fetchDashboardJobs();
  await sleep(5);
  // First cycle: warm-up 503 with Retry-After: 5 (hook fires before the body).
  statsEntries[0].settleWith({ ok: false, meta: { status: 503, retryAfterMs: 5000 }, error: warmupError(5000) });
  await sleep(5);
  const retryTimer = findTimer(5000);
  if (!retryTimer) throw new Error(`no Retry-After retry was scheduled: ${JSON.stringify(pendingTimers())}`);
  if (state.jobStats !== null) throw new Error("warm-up 503 produced fake zero stats");
  if (state.readModels.stats.warmingUp !== true) throw new Error("warm-up state was not captured");
  if (statsEntries.length !== 1) throw new Error("a duplicate outstanding stats request appeared before the retry");
  fireTimer(retryTimer);
  await sleep(5);
  if (statsEntries.length !== 2) throw new Error("the scheduled retry did not re-request stats");
  statsEntries[1].settleWith({ ok: true, meta: { status: 200, updatedAt: "2026-01-01T00:00:10Z", ageSeconds: 3 }, body: { total: 42, by_status: { succeeded: 40, failed: 2 }, by_day: [] } });
  await sleep(10);
  process.stdout.write(JSON.stringify({
    converged: !!state.jobStats && state.jobStats.total === 42,
    warmingCleared: state.readModels.stats.warmingUp === false,
    freshnessCaptured: state.readModels.stats.updatedAt === "2026-01-01T00:00:10Z",
    noTimersAfterSuccess: pendingTimers().length === 0,
    dashboardRenderedAfterRetry: renderCalls.length > 0,
    payloadKeys: Object.keys(state.jobStats || {}),
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["converged"], "warm-up must converge through the auto retry, without a manual reload")
        self.assertTrue(output["warmingCleared"])
        self.assertTrue(output["freshnessCaptured"])
        self.assertTrue(output["noTimersAfterSuccess"])
        self.assertTrue(output["dashboardRenderedAfterRetry"])
        self.assertEqual(output["payloadKeys"], ["total", "by_status", "by_day"])

    def test_stats_keep_last_good_on_transient_error_and_payload_untouched(self):
        script = self.stats_env({
            "workers": [], "targets": [], "jobs": [], "jobStats": None,
            "currentView": "dashboard", "storageProfiles": [], "readModels": None,
        }) + wrap_async(r'''
  const statsEntries = [];
  respond = (path, options) => {
    if (path.startsWith("/api/v1/jobs/stats?")) {
      const entry = { options, ...controlled() };
      statsEntries.push(entry);
      return entry;
    }
    return Promise.resolve({ items: [] });
  };
  void fetchDashboardJobs();
  await sleep(5);
  statsEntries[0].settleWith({ ok: true, meta: { status: 200, updatedAt: "2026-01-01T00:00:00Z", ageSeconds: 2 }, body: { total: 42, by_status: { succeeded: 40, failed: 2 }, by_day: [] } });
  await sleep(10);
  void fetchDashboardJobs(); // explicit refresh while the backend hits a transient failure
  await sleep(5);
  statsEntries[1].settleWith({ ok: false, meta: { status: 500, retryAfterMs: null }, error: genericError(500) });
  await sleep(10);
  process.stdout.write(JSON.stringify({
    lastGoodRetained: state.jobStats !== null && state.jobStats.total === 42,
    payloadKeys: Object.keys(state.jobStats || {}),
    freshnessUnchanged: state.readModels.stats.updatedAt === "2026-01-01T00:00:00Z" && state.readModels.stats.ageSeconds === 2,
    notMarkedWarming: state.readModels.stats.warmingUp === false,
    noRetryTimerForTransientError: pendingTimers().length === 0,
    outstandingCleared: state.readModels.stats.outstanding === null,
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["lastGoodRetained"], "transient errors must retain the last-good stats")
        self.assertEqual(output["payloadKeys"], ["total", "by_status", "by_day"])
        self.assertTrue(output["freshnessUnchanged"])
        self.assertTrue(output["notMarkedWarming"])
        self.assertTrue(output["noRetryTimerForTransientError"])
        self.assertTrue(output["outstandingCleared"], "outstanding stats requests must not leak")


class DashboardTargetsSnapshotTests(unittest.TestCase):
    """Targets polling reads the snapshot with freshness headers; warm-up keeps
    last-good data; requests coalesce and polling stops on navigation."""

    def targets_env(self, state_fields):
        extracts = shipped()
        return '"use strict";\n' + r'''
let currentLogJobSnapshot = null;
const renderTableCalls = [];
const renderTargetsTable = () => { renderTableCalls.push(1); };
const document = { getElementById: () => null };
const workerFingerprint = worker => JSON.stringify(worker && worker.id);
''' + "\n" + extracts["escape_html"] + "\n" + extracts["target_fingerprint"] + "\n" + extracts["targets_polling_lets"] + "\n" + extracts["start_targets_polling"] + "\n" + extracts["read_model_block"] + "\n" + HARNESS_PRELUDE + "\n" + FAKE_API_GET + 'const state = ' + json.dumps(state_fields, ensure_ascii=False) + ';'

    def test_poll_refreshes_freshness_with_same_fingerprint_without_rerender(self):
        target_fixture = { "id": "t-1", "enabled": True, "worker_id": "w-1", "worker_name": "W", "worker_status": "online", "execution_blocked": False, "blocked_reason": "", "updated_at": "2026-01-01T00:00:00Z" }
        state_fixture = { "currentView": "targets", "workers": [], "targets": [target_fixture], "systemVersion": "dev", "readModels": None }
        script = self.targets_env(state_fixture) + wrap_async(r'''
  const target = { id: "t-1", enabled: true, worker_id: "w-1", worker_name: "W", worker_status: "online", execution_blocked: false, blocked_reason: "", updated_at: "2026-01-01T00:00:00Z" };
  const targetEntries = [];
  respond = (path, options) => {
    if (path === "/api/v1/targets") {
      const entry = { options, ...controlled() };
      targetEntries.push(entry);
      return entry;
    }
    if (path === "/api/v1/workers") return Promise.resolve({ items: [] });
    return Promise.resolve({ items: [] });
  };
  startTargetsPolling();
  await sleep(5);
  const pollTimer = pendingTimers()[0];
  fireTimer(pollTimer);   // tick 1: targets request in flight (same fingerprint expected)
  await sleep(5);
  if (targetEntries.length !== 1) throw new Error("exactly one outstanding targets request expected");
  // A second caller during the in-flight read must reuse it, never duplicate it.
  const coalescedPromise = loadTargetsSnapshot();
  const coalescedWhileInFlight = targetEntries.length === 1;
  targetEntries[0].settleWith({ ok: true, meta: { status: 200, updatedAt: "2026-01-01T00:00:30Z", ageSeconds: 6 }, body: { items: [JSON.parse(JSON.stringify(target))] } }); // same fingerprint
  await coalescedPromise;
  await sleep(10);
  stopTargetsPolling();
  await sleep(5);
  process.stdout.write(JSON.stringify({
    freshnessUpdatedWithSameFingerprint: state.readModels.targets.updatedAt === "2026-01-01T00:00:30Z",
    targetsKept: state.targets.length === 1 && state.targets[0].id === "t-1",
    noRerenderWithoutChange: renderTableCalls.length === 0,
    pollTimerClearedOnStop: pendingTimers().length === 0,
    requestCoalesced: coalescedWhileInFlight,
    targetsCalls: targetEntries.length,
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["freshnessUpdatedWithSameFingerprint"],
                        "header metadata must refresh even when targetFingerprint is unchanged")
        self.assertTrue(output["targetsKept"])
        self.assertTrue(output["noRerenderWithoutChange"])
        self.assertTrue(output["pollTimerClearedOnStop"], "stopTargetsPolling must clear the pending poll timer")
        self.assertTrue(output["requestCoalesced"])
        self.assertEqual(output["targetsCalls"], 1, "no duplicate outstanding targets requests")

    def test_targets_warmup_keeps_last_good_and_poll_converges(self):
        target_fixture = { "id": "t-1", "enabled": True, "worker_id": "w-1", "worker_name": "W", "worker_status": "online", "execution_blocked": False, "blocked_reason": "", "updated_at": "2026-01-01T00:00:00Z" }
        state_fixture = { "currentView": "targets", "workers": [], "targets": [target_fixture], "systemVersion": "dev", "readModels": None }
        script = self.targets_env(state_fixture) + wrap_async(r'''
  const target = { id: "t-1", enabled: true, worker_id: "w-1", worker_name: "W", worker_status: "online", execution_blocked: false, blocked_reason: "", updated_at: "2026-01-01T00:00:00Z" };
  const targetEntries = [];
  respond = (path, options) => {
    if (path === "/api/v1/targets") {
      const entry = { options, ...controlled() };
      targetEntries.push(entry);
      return entry;
    }
    if (path === "/api/v1/workers") return Promise.resolve({ items: [] });
    return Promise.resolve({ items: [] });
  };
  startTargetsPolling();
  const tick = pendingTimers().find(timer => timer.ms === 3000);
  fireTimer(tick);
  await sleep(5);
  // Warm-up 503 with Retry-After: 5 (hook fires before the body settles).
  targetEntries[0].settleWith({ ok: false, meta: { status: 503, retryAfterMs: 5000 }, error: warmupError(5000) });
  await sleep(10);
  if (state.targets.length !== 1) throw new Error("warm-up 503 cleared last-good targets");
  if (state.readModels.targets.warmingUp !== true) throw new Error("warm-up state missing");
  if (state.readModels.targets.updatedAt !== null) throw new Error("warm-up must not fabricate freshness metadata");
  // Poll cadence keeps retrying without any manual reload.
  const secondTick = pendingTimers().find(timer => timer.ms === 3000);
  fireTimer(secondTick);
  await sleep(5);
  if (targetEntries.length !== 2) throw new Error("poll cadence did not retry");
  targetEntries[1].settleWith({ ok: true, meta: { status: 200, updatedAt: "2026-01-01T00:01:00Z", ageSeconds: 2 }, body: { items: [{ ...target, enabled: false }] } });
  await sleep(10);
  process.stdout.write(JSON.stringify({
    lastGoodKept: state.targets.length === 1,
    converged: state.targets[0].enabled === false,
    freshnessConverged: state.readModels.targets.updatedAt === "2026-01-01T00:01:00Z",
    warmingCleared: state.readModels.targets.warmingUp === false,
    rerenderOnConvergence: renderTableCalls.length === 1,
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["lastGoodKept"])
        self.assertTrue(output["converged"], "poll cadence must converge warm-up without a browser reload")
        self.assertTrue(output["freshnessConverged"])
        self.assertTrue(output["warmingCleared"])
        self.assertTrue(output["rerenderOnConvergence"])

    def test_targets_view_marks_preparing_and_timer_lifecycle_is_wired(self):
        source = UI_PATH.read_text(encoding="utf-8")
        # Targets view carries the preparing copy and the freshness element.
        self.assertIn('id="readModelTargetsFreshness"', source)
        self.assertIn("Preparando targets...", source)
        # Timer lifecycle: view switches pause/stop the dashboard-only timers.
        set_view = re.search(r"function setView\(view\) \{.*?\n    function route", source, re.S)
        self.assertIsNotNone(set_view)
        self.assertIn("stopReadModelTimersForView(view)", set_view.group(0))
        block_source = extract_block(READ_MODEL_BLOCK_PATTERN, "read model block")
        for marker in (
            'if (nextView !== "dashboard") stopReadModelRetryTimer("stats");',
            "stopReadModelFreshnessTicker()",
            "startReadModelFreshnessTicker()",
        ):
            self.assertIn(marker, block_source)
        self.assertIn('window.addEventListener("pagehide"', source)


class DashboardFreshnessViewTests(unittest.TestCase):
    """The dashboard renders explicit warm-up copy instead of fake zeros, keeps
    last-good stats, and freshness ages advance from cached snapshot metadata."""

    def render_dashboard(self, read_models, job_stats=None, targets=None):
        extracts = shipped()
        state_fields = {
            "currentView": "dashboard",
            "workers": [],
            "targets": targets or [],
            "jobs": [{"id": "recent-job"}],
            "jobStats": job_stats,
            "storageProfiles": [],
            "readModels": read_models,
        }
        return '"use strict";\n' + r'''
const contentElement = { innerHTML: "" };
const document = { getElementById: id => (id === "content" ? contentElement : null) };
const fmtStatus = () => "";
const fmtDate = value => (value ? "12:00:00" : "—");
''' + "\n" + extracts["browser_tz"] + "\n" + extracts["build_activity_days"] + "\n" + extracts["empty_job_stats"] + "\n" + extracts["state_block"] + "\n" + extracts["read_model_block"] + "\n" + extracts["stat_card"] + "\n" + extracts["escape_html"] + "\n" + extracts["render_dashboard_block"] + r'''
Object.assign(state, ''' + json.dumps(state_fields, ensure_ascii=False) + ''');
renderDashboard(contentElement);
process.stdout.write(JSON.stringify({ html: contentElement.innerHTML }));
'''

    def warming_read_models(self, stats_warming=True, targets_warming=True, stats_freshness=None):
        def meta(warming, freshness):
            return {
                "updatedAt": freshness.get("updatedAt") if freshness else None,
                "ageSeconds": freshness.get("ageSeconds") if freshness else None,
                "observedAtMs": freshness.get("observedAtMs") if freshness else None,
                "warmingUp": warming,
                "retryTimer": None, "retryAfterMs": None, "retryDelayMs": None, "outstanding": None,
            }
        return {
            "stats": meta(stats_warming, stats_freshness),
            "targets": meta(targets_warming, None),
        }

    def test_dashboard_shows_preparing_copy_and_no_fake_zero_when_stats_warm_up(self):
        result = run_node_eval(self.render_dashboard(self.warming_read_models()))
        self.assertEqual(result.returncode, 0, result.stderr)
        html = json.loads(result.stdout)["html"]
        self.assertIn("Preparando estadísticas...", html)
        self.assertIn("Preparando targets...", html)
        self.assertIn('id="readModelStatsFreshness"', html)
        self.assertNotIn("historico de jobs", html, "a pending snapshot must not render the settled-stats card copy")
        self.assertNotIn('<div class="legend-empty">Sin datos</div>', html,
                         "a warming snapshot must not be displayed as a settled empty result")
        self.assertNotRegex(html, r'fill="var\(--foreground\)"[^>]*>0</text>', "the donut center must not fake zero while warming")

    def test_dashboard_keeps_last_good_stats_instead_of_preparing(self):
        read_models = self.warming_read_models(
            stats_freshness={"updatedAt": "2026-01-01T00:00:00Z", "ageSeconds": 3, "observedAtMs": 0},
        )
        result = run_node_eval(self.render_dashboard(
            read_models,
            job_stats={"total": 42, "by_status": {"succeeded": 40, "failed": 2}, "by_day": []},
        ))
        self.assertEqual(result.returncode, 0, result.stderr)
        html = json.loads(result.stdout)["html"]
        self.assertNotIn("Preparando estadísticas...", html)
        self.assertIn('class="value">42</div>', html, "last-good stats must keep the dashboard numbers")

    def test_freshness_age_advances_using_cached_data(self):
        extracts = shipped()
        script = '"use strict";\n' + extracts["browser_tz"] + "\n" + extracts["fmt_date"] + "\n" + extracts["state_block"] + "\n" + extracts["read_model_block"] + wrap_async(r'''
  captureReadModelMeta("stats", { status: 200, updatedAt: "2026-01-01T00:00:00Z", ageSeconds: 5 });
  const first = readModelFreshness("stats", true);
  // 10 seconds pass without any re-render; the age text must advance on its own.
  state.readModels.stats.observedAtMs -= 10000;
  const second = readModelFreshness("stats", true);
  captureReadModelMeta("targets", { status: 200, updatedAt: "2026-01-01T00:00:00Z", ageSeconds: 0 });
  state.readModels.targets.observedAtMs -= 3000;
  const targetsFreshness = readModelFreshness("targets", true);
  state.readModels.targets.updatedAt = null;
  state.readModels.targets.warmingUp = true;
  const warmingCopy = readModelFreshness("targets", false);
  const settledNoMeta = readModelFreshness("stats", true) !== null && readModelFreshness("targets", true) === null;
  process.stdout.write(JSON.stringify({ first, second, ageAdvances: first !== second, targetsFreshness, warmingCopy, settledNoMeta }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertIn("Actualizado hace 5 s", output["first"])
        self.assertIn("Actualizado hace 15 s", output["second"], "age must advance without any re-render")
        self.assertTrue(output["ageAdvances"])
        self.assertIn("Actualizado hace 3 s", output["targetsFreshness"])
        self.assertEqual(output["warmingCopy"], "Preparando targets...")
        self.assertTrue(output["settledNoMeta"], "no snapshot metadata means no fake freshness text")


class DashboardRefreshIndependenceTests(unittest.TestCase):
    """Boot/refresh flows resolve without awaiting pending stats/targets and
    converge warm-ups through bounded Retry-After retries."""

    def integration_env(self):
        extracts = shipped()
    def integration_env(self):
        extracts = shipped()
        return '"use strict";\n' + r'''
const renderTargetsCalls = [];
const renderTargetsTable = () => { renderTargetsCalls.push(1); };
let currentLogJobSnapshot = null;
const hasJobDetail = value => value !== null && value !== undefined;
''' + "\n" + extracts["nav_items"] + "\n" + extracts["browser_tz"] + "\n" + extracts["merge_job_list"] + "\n" + extracts["apply_dashboard_jobs"] + "\n" + extracts["empty_job_stats"] + "\n" + extracts["escape_html"] + "\n" + extracts["fmt_date"] + "\n" + extracts["state_block"] + "\n" + extracts["current_route_view"] + "\n" + extracts["should_fetch_jobs"] + "\n" + extracts["fetch_dashboard_jobs"] + "\n" + extracts["read_model_block"] + "\n" + extracts["load_page_data"] + "\n" + extracts["route_block"] + "\n" + extracts["refresh_dashboard_jobs"] + "\n" + extracts["refresh_all_block"] + "\n" + extracts["target_fingerprint"] + "\n" + extracts["targets_polling_lets"] + "\n" + HARNESS_PRELUDE + "\n" + FAKE_API_GET + "\n" + r'''
const setViewCalls = [];
const renderDashboardCalls = [];
const window = { location: { hash: "#/dashboard" } };
const setView = view => {
  setViewCalls.push(view);
  state.currentView = view;
};
const renderDashboard = () => { renderDashboardCalls.push(1); };
const renderVersionBanner = () => {};
const contentElement = { innerHTML: "" };
const document = { getElementById: id => (id === "content" ? contentElement : null) };
'''

    def test_boot_applies_recent_jobs_while_stats_and_targets_pending(self):
        script = self.integration_env() + wrap_async(r'''
  const statsEntries = [];
  const targetEntries = [];
  respond = (path, options) => {
    if (path.startsWith("/api/v1/jobs/stats?")) {
      const entry = { options, ...controlled() };
      statsEntries.push(entry);
      return entry;
    }
    if (path === "/api/v1/targets") {
      const entry = { options, ...controlled() };
      targetEntries.push(entry);
      return entry;
    }
    if (path === "/api/v1/jobs?limit=5") return Promise.resolve({ items: [{ id: "recent-1" }] });
    if (path === "/api/v1/workers") return Promise.resolve({ items: [{ id: "w-1" }] });
    return Promise.resolve({ items: [], scheduler_timezone: "America/Bogota" });
  };
  window.location.hash = "#/dashboard";
  state.currentRole = "admin";
  state.currentView = "dashboard";
  await refreshAll();   // must not await the pending read models
  if (statsEntries.length !== 1 || targetEntries.length !== 1) {
    throw new Error(`unexpected boot requests: ${JSON.stringify(calls.map(call => call.path))}`);
  }
  process.stdout.write(JSON.stringify({
    bootResolvedWithPendingModels: true,
    quickJobsApplied: state.jobs.length === 1 && state.jobs[0].id === "recent-1",
    statsStillPending: !state.jobStats,
    targetsStillPending: state.targets.length === 0,
    dashboardRenderedQuickly: renderDashboardCalls.length > 0,
    routeReachedBoot: setViewCalls.includes("dashboard"),
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["bootResolvedWithPendingModels"], "boot must not wait for pending read models")
        self.assertTrue(output["quickJobsApplied"], "quick widget data applies while stats/targets are pending")
        self.assertTrue(output["statsStillPending"])
        self.assertTrue(output["targetsStillPending"])
        self.assertTrue(output["dashboardRenderedQuickly"])
        self.assertTrue(output["routeReachedBoot"])

    def test_stats_warmup_retries_per_retry_after_during_boot_and_converges(self):
        script = self.integration_env() + wrap_async(r'''
  const statsEntries = [];
  respond = (path, options) => {
    if (path.startsWith("/api/v1/jobs/stats?")) {
      const entry = { options, ...controlled() };
      statsEntries.push(entry);
      return entry;
    }
    if (path === "/api/v1/jobs?limit=5") return Promise.resolve({ items: [{ id: "recent-1" }] });
    return Promise.resolve({ items: [] });
  };
  window.location.hash = "#/dashboard";
  state.currentRole = "admin";
  state.currentView = "dashboard";
  await refreshAll();
  // Warm-up 503 with Retry-After: 5 (hook fires before the body settles).
  statsEntries[0].settleWith({ ok: false, meta: { status: 503, retryAfterMs: 5000 }, error: warmupError(5000) });
  await sleep(10);
  if (state.jobStats) throw new Error("boot warm-up produced fake zero stats");
  if (state.readModels.stats.warmingUp !== true) throw new Error("boot warm-up was not captured");
  const retryTimer = findTimer(5000);
  if (!retryTimer) throw new Error(`boot warm-up did not schedule a Retry-After retry: ${JSON.stringify(pendingTimers())}`);
  if (statsEntries.length !== 1) throw new Error("a duplicate outstanding stats request appeared before the retry");
  fireTimer(retryTimer);
  await sleep(5);
  if (statsEntries.length !== 2) throw new Error("the scheduled retry did not re-request stats");
  statsEntries[1].settleWith({ ok: true, meta: { status: 200, updatedAt: "2026-01-01T00:00:10Z", ageSeconds: 3 }, body: { total: 42, by_status: { succeeded: 40, failed: 2 }, by_day: [] } });
  await sleep(10);
  process.stdout.write(JSON.stringify({
    converged: !!state.jobStats && state.jobStats.total === 42,
    warmingCleared: state.readModels.stats.warmingUp === false,
    freshnessCaptured: state.readModels.stats.updatedAt === "2026-01-01T00:00:10Z",
    noTimersAfterSuccess: pendingTimers().length === 0,
    quickJobsStillFirst: state.jobs.length === 1 && state.jobs[0].id === "recent-1",
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["converged"], "boot warm-up must converge via the bounded Retry-After retry")
        self.assertTrue(output["warmingCleared"])
        self.assertTrue(output["freshnessCaptured"])
        self.assertTrue(output["noTimersAfterSuccess"])
        self.assertTrue(output["quickJobsStillFirst"])

    def test_targets_view_boot_stays_route_scoped_and_warmup_converges(self):
        script = self.integration_env() + wrap_async(r'''
  const targetEntries = [];
  respond = (path, options) => {
    if (path === "/api/v1/targets") {
      const entry = { options, ...controlled() };
      targetEntries.push(entry);
      return entry;
    }
    if (path === "/api/v1/workers") return Promise.resolve({ items: [] });
    return Promise.resolve({ items: [], scheduler_timezone: "America/Bogota" });
  };
  window.location.hash = "#/targets";
  state.currentRole = "admin";
  state.currentView = "targets";
  await refreshAll();
  if (targetEntries.length !== 1) throw new Error("targets boot must request the targets snapshot once");
  // Warm-up 503 with Retry-After: 5 (hook fires before the body settles).
  targetEntries[0].settleWith({ ok: false, meta: { status: 503, retryAfterMs: 5000 }, error: warmupError(5000) });
  await sleep(10);
  const retryTimer = findTimer(5000);
  if (!retryTimer) throw new Error(`targets warm-up did not schedule a Retry-After retry: ${JSON.stringify(pendingTimers())}`);
  fireTimer(retryTimer);
  await sleep(5);
  if (targetEntries.length !== 2) throw new Error("retry did not re-request the targets snapshot");
  targetEntries[1].settleWith({ ok: true, meta: { status: 200, updatedAt: "2026-01-01T00:00:10Z", ageSeconds: 2 }, body: { items: [{ id: "t-1", created_at: "2026-01-01", enabled: true }] } });
  await sleep(10);
  process.stdout.write(JSON.stringify({
    routeScoped: !calls.map(call => call.path).some(path => path.startsWith("/api/v1/jobs")),
    targetsAppliedAfterRetry: state.targets.length === 1 && state.targets[0].id === "t-1",
    freshnessApplied: state.readModels.targets.updatedAt === "2026-01-01T00:00:10Z",
    viewKept: state.currentView === "targets",
    noTimersAfterSuccess: pendingTimers().length === 0,
  }));
''')
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertTrue(output["routeScoped"], "targets boot stays route-scoped (no jobs/stats requests)")
        self.assertTrue(output["targetsAppliedAfterRetry"])
        self.assertTrue(output["freshnessApplied"])
        self.assertTrue(output["viewKept"])
        self.assertTrue(output["noTimersAfterSuccess"])


class DashboardReadModelDocsTests(unittest.TestCase):
    """The operator quickstart documents the T3 UI contract with the exact
    Spanish strings."""

    def test_quickstart_documents_operator_contract_and_exact_strings(self):
        doc = QUICKSTART_PATH.read_text(encoding="utf-8")
        self.assertIn("Preparando estadísticas...", doc)
        self.assertIn("Preparando targets...", doc)
        self.assertIn("X-Read-Model-Updated-At", doc)
        self.assertIn("read_model_warming_up", doc)
        self.assertIn("Actualizado hace", doc)
        self.assertIn("sin recargar el navegador", doc)


if __name__ == "__main__":
    unittest.main()