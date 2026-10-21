import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "index.html"


def extract_block(pattern, label=""):
    """Extract real helpers from index.html so tests run the shipped code."""
    source = UI_PATH.read_text(encoding="utf-8")
    match = re.search(pattern, source, re.S)
    if not match:
        raise AssertionError(f"{label}: pattern not found in index.html: {pattern}")
    return match.group(0).strip()


def run_node_eval(script):
    result = subprocess.run(
        ["node", "--input-type=commonjs", "--eval", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )
    return result


class IsVersionOutdatedComparisonTest(unittest.TestCase):
    """Control Plane banner and worker badges must treat only a numerically
    higher known release as outdated; equal or newer stays quiet."""

    def test_version_banner_only_shows_for_strictly_newer_release(self):
        state_block = extract_block(
            r"const state = \{.*?\n    \};\n    let workersNotice = null;",
            "state",
        )
        escape_html = extract_block(r"function escapeHtml\(str\) \{.*?\n    \}", "escapeHtml")
        comparator = extract_block(
            r"function isVersionOutdated\(current, latest\) \{.*?\n    \}",
            "isVersionOutdated",
        )
        banner = extract_block(
            r"function renderVersionBanner\(\) \{.*?\n    \}",
            "renderVersionBanner",
        )
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")

        script = f"""
const versionBanner = {{ style: {{}}, innerHTML: "" }};
const document = {{
  getElementById: (id) => (id === "versionBanner" ? versionBanner : null),
}};
{state_block}
{escape_html}
{comparator}
{banner}
const cases = [
  {{ name: "older", cur: "3.15.7", lat: "v3.15.6", outdated: false }}, // shipped 3.15.7 install vs stale GitHub latest
  {{ name: "equal", cur: "3.15.7", lat: "v3.15.7", outdated: false }},
  {{ name: "equalNoPrefix", cur: "v3.15.7", lat: "3.15.7", outdated: false }},
  {{ name: "newerPatch", cur: "3.15.7", lat: "v3.15.8", outdated: true }},
  {{ name: "newerMinor", cur: "3.15.7", lat: "v3.16.0", outdated: true }},
  {{ name: "newerMajor", cur: "3.15.7", lat: "v4.0.0", outdated: true }},
  {{ name: "multiDigitNewer", cur: "3.15.9", lat: "v3.15.10", outdated: true }},
  {{ name: "multiDigitOlder", cur: "3.15.10", lat: "v3.15.9", outdated: false }},
  {{ name: "majorPrecedence", cur: "3.999.9", lat: "v4.0.0", outdated: true }},
  {{ name: "majorPrecedenceOlder", cur: "4.0.0", lat: "v3.999.9", outdated: false }},
  {{ name: "dev", cur: "dev", lat: "v3.15.8", outdated: false }},
  {{ name: "missingCurrent", cur: "", lat: "v3.15.8", outdated: false }},
  {{ name: "missingLatest", cur: "3.15.7", lat: "", outdated: false }},
  {{ name: "invalidCurrent", cur: "not-a-version", lat: "v3.15.8", outdated: false }},
  {{ name: "invalidLatest", cur: "3.15.7", lat: "vabc", outdated: false }},
  {{ name: "tooFewParts", cur: "3.15", lat: "v3.15.1", outdated: false }},
  {{ name: "tooManyParts", cur: "3.15.7.1", lat: "v3.16.0", outdated: false }},
];
const comparatorResults = cases.map(({{
  name, cur, lat, outdated: expected,
}}) => ({{ name, expected, outdated: isVersionOutdated(cur, lat) }}));

state.latestVersionUrl = "https://github.com/DanielRondonGarcia/vaultline/releases/latest";
const bannerRuns = [];
for (const {{ name, cur, lat, outdated: expected }} of cases) {{
  state.systemVersion = cur;
  state.latestVersion = lat || null;
  versionBanner.style = {{}};
  versionBanner.innerHTML = "";
  renderVersionBanner();
  bannerRuns.push({{ name, expected, visible: versionBanner.style.display !== "none", html: versionBanner.innerHTML }});
}}

state.systemVersion = "3.15.7";
state.latestVersion = "v3.15.8";
versionBanner.style = {{}};
versionBanner.innerHTML = "";
renderVersionBanner();
const genuineHtml = versionBanner.innerHTML;

process.stdout.write(JSON.stringify({{
  comparatorResults,
  bannerRuns: bannerRuns.map(({{ name, expected, visible }}) => ({{ name, expected, visible }})),
  genuineHtml,
}}));
"""
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)

        comparator_failures = [
            f"{r['name']}: comparator outdated={r['outdated']} expected={r['expected']}"
            for r in output["comparatorResults"]
            if r["outdated"] != r["expected"]
        ]
        banner_failures = [
            f"{r['name']}: banner visible={r['visible']} expected={r['expected']}"
            for r in output["bannerRuns"]
            if r["visible"] != r["expected"]
        ]
        self.assertEqual(comparator_failures, [], "comparator mismatches")
        self.assertEqual(banner_failures, [], "banner visibility mismatches")

        for expected in (
            "Nueva version disponible:",
            "v3.15.8",
            "estas en v3.15.7",
            'target="_blank"',
            "https://github.com/DanielRondonGarcia/vaultline/releases/latest",
            "Ver release",
        ):
            self.assertIn(expected, output["genuineHtml"])

    def test_worker_badges_only_outdated_for_strictly_newer_release(self):
        state_block = extract_block(
            r"const state = \{.*?\n    \};\n    let workersNotice = null;",
            "state",
        )
        escape_html = extract_block(r"function escapeHtml\(str\) \{.*?\n    \}", "escapeHtml")
        comparator = extract_block(
            r"function isVersionOutdated\(current, latest\) \{.*?\n    \}",
            "isVersionOutdated",
        )
        workers = extract_block(
            r"function renderWorkers\(content\) \{.*?\n    \}",
            "renderWorkers",
        )
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")

        script = f"""
const content = {{ innerHTML: "", querySelectorAll: () => [] }};
const document = {{ getElementById: () => null }};
const openAddWorkerModal = () => {{}};
const openEditWorkerLabelsModal = () => {{}};
const openRenewWorkerModal = () => {{}};
const revokeWorker = () => {{}};
const deleteWorker = () => {{}};
const startWorkersPolling = () => {{}};
const fmtStatus = () => "";
const fmtDate = () => "—";
const ICONS = {{ warn: "⚠" }};
{state_block}
{escape_html}
{comparator}
{workers}

state.currentRole = "admin";
state.workers = [
  {{ id: "w-ahead", name: "ahead", version: "3.15.7", status: "active", labels: {{ env: "prod" }} }}, // shipped 3.15.7 install vs stale GitHub latest v3.15.6
  {{ id: "w-equal", name: "equal", version: "v3.15.6", status: "active", labels: {{}} }},
  {{ id: "w-old", name: "old", version: "3.15.5", status: "active", labels: {{}} }},
  {{ id: "w-dev", name: "dev-worker", version: "dev", status: "active", labels: {{}} }},
  {{ id: "w-md", name: "multidigit", version: "v3.15.10", status: "active", labels: {{}} }},
  {{ id: "w-bad", name: "invalid", version: "not-a-version", status: "active", labels: {{}} }},
];

state.latestVersion = "v3.15.6";
renderWorkers(content);
const htmlForOlderLatest = content.innerHTML;

let olderLatestOutdatedBadgeCount = (htmlForOlderLatest.match(/version-badge outdated/g) || []).length;

state.latestVersion = "v3.15.9";
renderWorkers(content);
const htmlForHigherLatest = content.innerHTML;
let higherLatestOutdatedBadgeCount = (htmlForHigherLatest.match(/version-badge outdated/g) || []).length;

process.stdout.write(JSON.stringify({{
  htmlForOlderLatest,
  htmlForHigherLatest,
  olderLatestOutdatedBadgeCount,
  higherLatestOutdatedBadgeCount,
}}));
"""
        result = run_node_eval(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)

        # Latest known release (v3.15.6) is older than the installed 3.15.7 worker: no outdated badge.
        self.assertEqual(output["olderLatestOutdatedBadgeCount"], 1,
            "only the genuinely older worker (3.15.5) must be flagged")
        html = output["htmlForOlderLatest"]
        self.assertIn("✓ v3.15.7", html)
        self.assertNotIn("⚠ v3.15.7", html)
        self.assertIn("✓ v3.15.6", html)
        self.assertNotIn("⚠ v3.15.6", html)
        self.assertIn("⚠ v3.15.5", html)
        self.assertIn("Desactualizado: ultima version es v3.15.6", html)
        self.assertIn('<span class="mono">—</span>', html)  # dev worker keeps placeholder
        self.assertNotIn("⚠ not-a-version", html)  # invalid data must not flag a false update
        self.assertIn("✓ v3.15.10", html)  # 3.15.10 > 3.15.6
        # Genuine-update upkeep: labels and worker actions stay intact.
        self.assertIn("env: prod", html)
        self.assertIn("data-edit-labels=", html)
        self.assertIn("data-delete-worker=", html)

        # Latest v3.15.9: 3.15.7 worker becomes outdated, multidigit 3.15.10 stays current.
        self.assertEqual(output["higherLatestOutdatedBadgeCount"], 3,
            "3.15.7, v3.15.6 and 3.15.5 are older than v3.15.9; 3.15.10 is not")
        higher_html = output["htmlForHigherLatest"]
        self.assertIn("⚠ v3.15.7", higher_html)
        self.assertIn("Desactualizado: ultima version es v3.15.9", higher_html)
        self.assertIn("✓ v3.15.10", higher_html)
        self.assertNotIn("⚠ v3.15.10", higher_html)


if __name__ == "__main__":
    unittest.main()