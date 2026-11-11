"""Behavioral harness tests for the restore snapshot modal (UI unit).

Runs the real `openRestoreFromSnapshotModal` function extracted from the
index.html source inside Node with a minimal in-memory DOM, then asserts the
observable behavior per ODD spec native-restore-preserve-original S2-S5:
preserve is the safe default without destructive confirmation, replacement
requires an explicit warning-visible consent, toggling resets consent, native
destination offers the original configured route without new request fields,
and manual permissions stay gated behind their own confirmation.

No production API requests are made: `apiPost` is mocked and every request
body is captured in-memory. The test runner is the existing silent unittest
runner invoked like `python scripts/run_tests_silent.py tests.test_restore_modal`.
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "index.html"


HARNESS_PRELUDE = r'''
const escapeHtml = value => String(value)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

class FakeElement {
  constructor(id) {
    this.id = id;
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.hidden = undefined;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.textContent = "";
    this.open = false;
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  focus() { focused = this; }
}

let focused = null;
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, new FakeElement(id));
  return elements.get(id);
};
const document = {
  contains: () => false,
  createElement: () => new FakeElement("created"),
  querySelector: () => null,
  getElementById: id => elements.get(id) || null,
};

let snapshotBrowserState = null;
const invalidateSnapshotRequest = () => {};
const renderSnapshotModalFrame = () => {};
let closedModal = false;
const closeSnapshotsModal = () => { closedModal = true; };
const apiCalls = [];
const withBtnLabels = [];
const logPanels = [];
const apiPost = async (path, body) => {
  apiCalls.push({ path, body });
  return { id: "job-1" };
};
const withBtnLoading = async (btn, run, label) => {
  withBtnLabels.push(label || "");
  await run();
};
const openLogPanel = (jobId, targetId) => { logPanels.push({ jobId, targetId }); };
'''
HELPER_AND_RESTORE = r'''

function getRestoreOwnershipVolumeCandidates(target) {
  const runtimeVolumes = target?.runtime_volumes;
  const validKey = /^compose:[A-Za-z0-9][A-Za-z0-9_.-]*:[A-Za-z0-9][A-Za-z0-9_.-]*$/;
  if (!runtimeVolumes || typeof runtimeVolumes !== "object" || Array.isArray(runtimeVolumes)) return [];
  const selectedPaths = new Set(Array.isArray(target.volume_targets) ? target.volume_targets.filter(path => typeof path === "string") : []);
  return Object.entries(runtimeVolumes)
    .map(([source, spec]) => {
      const bind = spec && typeof spec === "object" ? spec.bind : "";
      const key = spec && typeof spec === "object" ? (spec.stable_key || spec.volume_key || "") : "";
      const name = spec && typeof spec === "object" && spec.name
        ? spec.name
        : (spec && typeof spec === "object" && spec.compose_volume ? spec.compose_volume : (key.split(":")[2] || source));
      return { name, bind, key };
    })
    .filter(candidate => validKey.test(candidate.key) && (!selectedPaths.size || (typeof candidate.bind === "string" && candidate.bind && selectedPaths.has(candidate.bind))))
    .sort((left, right) => left.key.localeCompare(right.key));
}
'''

# The restore function is extracted from the live source so the harness tests
# the shipped code, not a copy.

HARNESS_DRIVER_PREP = r'''
const nativeTarget = (filesystemPaths = []) => ({
  id: "target-1",
  name: "App",
  runtime_type: "native",
  filesystem_paths: filesystemPaths,
});
const dockerTarget = () => ({ id: "target-2", name: "Compose", runtime_type: "docker" });

const buildHarness = target => {
  elements.clear();
  focused = null;
  closedModal = false;
  apiCalls.length = 0;
  withBtnLabels.length = 0;
  logPanels.length = 0;
  const preserveRadio = element("rsOwnershipPreserve");
  const mapRadio = element("rsOwnershipMap");
  preserveRadio.value = "preserve";
  mapRadio.value = "map";
  preserveRadio.checked = true;
  mapRadio.checked = false;
  const advancedDetails = element("rsOwnershipAdvanced");
  const advancedTextarea = element("rsOwnershipJson");
  advancedTextarea.value = JSON.stringify({ schema_version: 1, mode: "preserve", mappings: {} });
  element("rsOwnershipStatus");
  const confirmation = element("rsOwnershipConfirm");
  element("rsOwnershipConfirmRow");
  element("rsOwnershipConfirmText");
  element("rsConfirm");
  const overwriteSelect = element("rsOverwrite");
  overwriteSelect.value = "false";
  element("rsOverwriteGroup");
  element("rsRestoreWarning");
  const destinationSelect = element("rsRestoreDestination");
  destinationSelect.value = "custom";
  const originalSelect = element("rsRestoreOriginalPath");
  element("rsRestoreOriginalGroup");
  element("rsRestoreCustomGroup");
  element("rsRestoreTargetPath");
  element("rsForm");
  element("rsClose");
  element("rsCancel");

  const overlay = {
    innerHTML: "",
    querySelector(selector) {
      if (selector === '[aria-invalid="true"]') {
        for (const [, candidate] of elements) {
          if (candidate.attributes["aria-invalid"] === "true" && !candidate.hidden) return candidate;
        }
        return null;
      }
      if (selector.startsWith("#")) return elements.get(selector.slice(1)) || null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === 'input[name="restore_ownership_mode"]') return [preserveRadio, mapRadio];
      if (selector === "[data-restore-mapping]") return [];
      return [];
    },
  };
  snapshotBrowserState = { target, overlay };
  const apiPost = async (path, body) => {
    apiCalls.push({ path, body });
    return { id: "job-1" };
  };
  const withBtnLoading = async (btn, run, label) => {
    withBtnLabels.push(label || "");
    await run();
  };
  const openLogPanel = (jobId, targetId) => { logPanels.push({ jobId, targetId }); };
  openRestoreFromSnapshotModal("snap-1", "2024-05-01");
  const submit = async () => {
    const form = elements.get("rsForm");
    if (!form || !form.listeners.submit) throw new Error("submit listener missing");
    form.listeners.submit({ preventDefault() {}, submitter: null });
    await new Promise(resolve => setTimeout(resolve, 0));
    return apiCalls;
  };
  const fire = (id, event) => {
    const node = elements.get(id);
    if (!node || !node.listeners[event]) return false;
    node.listeners[event]();
    return true;
  };
  return {
    preserveRadio, mapRadio, advancedDetails, advancedTextarea, confirmation,
    overwriteSelect, destinationSelect, originalSelect,
    status: elements.get("rsOwnershipStatus"),
    button: elements.get("rsConfirm"),
    warning: elements.get("rsRestoreWarning"),
    overwriteGroup: elements.get("rsOverwriteGroup"),
    confirmRow: elements.get("rsOwnershipConfirmRow"),
    confirmText: elements.get("rsOwnershipConfirmText"),
    originalGroup: elements.get("rsRestoreOriginalGroup"),
    customGroup: elements.get("rsRestoreCustomGroup"),
    targetPathInput: elements.get("rsRestoreTargetPath"),
    markup: overlay.innerHTML,
    apiCalls, submit, fire, logPanels,
  };
};

const check = (condition, message) => { if (!condition) throw new Error(message); };
const equal = (actual, expected, message) => {
  if (actual !== expected) throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
};
'''

RUNNER_PREFIX = "(async () => {\n  const out = {};\n"
RUNNER_SUFFIX = "\n  process.stdout.write(JSON.stringify(out));\n})().catch(error => { console.error(error && error.message || error); process.exit(1); });\n"


def load_restore_source():
    source = UI_PATH.read_text(encoding="utf-8")
    restore = re.search(r"function openRestoreFromSnapshotModal\(snapshotId, dateStr\) \{.*?\n    \}", source, re.S)
    if restore is None:
        raise AssertionError("openRestoreFromSnapshotModal not found in index.html")
    return source, restore.group(0)


def run_node(script):
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js is not installed")
    result = subprocess.run(
        [node, "--input-type=commonjs"],
        input=script,
        timeout=15,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node harness failed: {result.stderr or result.stdout}")
    return json.loads(result.stdout)


PRESERVE_SUBMIT_BODY = r'''
  const h = buildHarness(nativeTarget(["/srv/app"]));
  h.targetPathInput.value = "/tmp/restore-dir";
  await h.submit();
  equal(h.apiCalls.length, 1, "preserve submits a request");
  equal(h.apiCalls[0].path, "/api/v1/targets/target-1/restore/run", "restore endpoint");
  const request = h.apiCalls[0].body;
  equal(request.force_overwrite, false, "preserve sends force_overwrite=false");
  equal(request.restore_target_path, "/tmp/restore-dir", "custom route sent");
  equal(request.stop_containers, false, "native stop_containers always false");
  equal(request.restore_ownership.mode, "preserve", "ownership preserve mode");
  equal("confirmation" in request.restore_ownership, false, "no confirmation flag without consent");
  equal(h.warning.hidden, true, "destructive warning hidden when preserving");
  equal(h.confirmRow.hidden, true, "destructive confirmation row hidden when preserving");
  equal(h.button.disabled, false, "restore button ready without checkbox when preserving");
  equal(h.overwriteGroup.dataset.destructive, "false", "overwrite group not marked destructive");
  equal(h.markup.includes('id="rsOwnershipConfirmText"'), true, "dynamic confirmation text exists");
  equal(h.markup.includes("⚠ Reemplazar archivos existentes"), true, "replace option visually warns");
  equal(h.markup.includes("no están en el snapshot"), true, "warning names files absent from snapshot");
  equal(h.markup.includes("crearon antes del snapshot"), true, "warning names pre-snapshot files");
  out.force = request.force_overwrite;
  out.confirmRowHidden = h.confirmRow.hidden;
'''

REPLACE_CONSENT_BODY = r'''
  const h = buildHarness(nativeTarget(["/srv/app"]));
  h.targetPathInput.value = "/tmp/restore-dir";
  h.overwriteSelect.value = "true";
  h.fire("rsOverwrite", "change");
  equal(h.warning.hidden, false, "warning shown when replacing");
  equal(h.overwriteGroup.dataset.destructive, "true", "group marked destructive when replacing");
  equal(h.confirmRow.hidden, false, "confirmation row shown when replacing");
  equal(h.confirmText.textContent.includes("reemplazar"), true, "confirmation text names the replacement");
  equal(h.button.disabled, true, "button blocked until confirm checked");
  await h.submit();
  equal(h.apiCalls.length, 0, "replacement blocked before explicit consent");
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  equal(h.button.disabled, false, "button ready after consent");
  await h.submit();
  equal(h.apiCalls.length, 1, "replacement submits after consent");
  equal(h.apiCalls[0].body.force_overwrite, true, "replacement sends force_overwrite=true");
  equal(h.apiCalls[0].body.restore_ownership.confirmation, "confirmed", "consent recorded");
  out.force = h.apiCalls[0].body.force_overwrite;
'''

SCOPE_RESET_DESTINATION_BODY = r'''
  const h = buildHarness(nativeTarget(["/srv/app"]));
  h.targetPathInput.value = "/tmp/restore-dir";
  h.overwriteSelect.value = "true";
  h.fire("rsOverwrite", "change");
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  equal(h.button.disabled, false, "consent granted for replacement");
  h.destinationSelect.value = "original";
  if (h.destinationSelect.listeners.change) h.destinationSelect.listeners.change();
  equal(h.confirmation.checked, false, "destination scope change resets destructive consent");
  equal(h.button.disabled, true, "button blocked after scope change");
  await h.submit();
  equal(h.apiCalls.length, 0, "cross-scope consent cannot submit");
  h.originalSelect.value = "/srv/app";
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  await h.submit();
  equal(h.apiCalls.length, 1, "re-confirmed replacement submits on the new scope");
  equal(h.apiCalls[0].body.restore_target_path, "/srv/app", "original path sent after re-consent");
  equal(h.apiCalls[0].body.force_overwrite, true, "replacement force kept after re-consent");
  out.rescope = h.apiCalls.length === 1;
'''

SCOPE_RESET_PATH_BODY = r'''
  const h = buildHarness(nativeTarget([]));
  h.overwriteSelect.value = "true";
  h.fire("rsOverwrite", "change");
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  equal(h.button.disabled, false, "consent granted for replacement");
  h.targetPathInput.value = "/tmp/other-dir";
  if (h.targetPathInput.listeners.input) h.targetPathInput.listeners.input();
  equal(h.confirmation.checked, false, "custom path change resets destructive consent");
  await h.submit();
  equal(h.apiCalls.length, 0, "cross-path submits blocked until re-confirmation");
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  await h.submit();
  equal(h.apiCalls.length, 1, "re-confirmed submits with the new custom path");
  equal(h.apiCalls[0].body.restore_target_path, "/tmp/other-dir", "new custom path sent");
  out.pathReset = h.apiCalls.length === 1;
'''

SCOPE_RESET_MAPPINGS_BODY = r'''
  const h = buildHarness(nativeTarget(["/srv/app"]));
  h.targetPathInput.value = "/tmp/restore-dir";
  h.mapRadio.checked = true;
  h.preserveRadio.checked = false;
  h.fire("rsOwnershipMap", "change");
  h.advancedTextarea.value = JSON.stringify({ schema_version: 1, mode: "map", mappings: { "compose:app:datos": "1000:1000" } });
  if (h.advancedTextarea.listeners.input) h.advancedTextarea.listeners.input();
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  equal(h.button.disabled, false, "manual permissions consent granted");
  h.advancedTextarea.value = JSON.stringify({ schema_version: 1, mode: "map", mappings: { "compose:app:datos": "2000:2000" } });
  if (h.advancedTextarea.listeners.input) h.advancedTextarea.listeners.input();
  equal(h.confirmation.checked, false, "mapping scope change resets manual consent");
  await h.submit();
  equal(h.apiCalls.length, 0, "cross-mapping submits blocked until re-confirmation");
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  await h.submit();
  equal(h.apiCalls.length, 1, "re-confirmed manual permissions submit");
  equal(h.apiCalls[0].body.restore_ownership.mappings["compose:app:datos"], "2000:2000", "new mapping sent after re-consent");
  out.mappingReset = h.apiCalls.length === 1;
'''

TOGGLE_RESET_BODY = r'''
  const h = buildHarness(nativeTarget(["/srv/app"]));
  h.targetPathInput.value = "/tmp/restore-dir";
  h.overwriteSelect.value = "true";
  h.fire("rsOverwrite", "change");
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  equal(h.button.disabled, false, "consent enabled replace");
  h.overwriteSelect.value = "false";
  h.fire("rsOverwrite", "change");
  equal(h.warning.hidden, true, "warning hidden after toggling back to preserve");
  equal(h.confirmation.checked, false, "stale destructive consent was reset");
  equal(h.overwriteGroup.dataset.destructive, "false", "destructive mark cleared");
  equal(h.button.disabled, false, "preserve is ready again without consent");
  await h.submit();
  equal(h.apiCalls.length, 1, "preserve submits after consent reset");
  equal(h.apiCalls[0].body.force_overwrite, false, "preserve still forces nothing");
  h.overwriteSelect.value = "true";
  h.fire("rsOverwrite", "change");
  equal(h.warning.hidden, false, "warning shown again on re-toggle");
  equal(h.confirmation.checked, false, "consent still reset after round trip");
  await h.submit();
  equal(h.apiCalls.length, 1, "replacement blocked with reset consent, preserve submit recorded earlier");
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  await h.submit();
  equal(h.apiCalls.length, 2, "fresh consent submits");
  equal(h.apiCalls[1].body.force_overwrite, true, "replacement after fresh consent forces overwrite");
  out.resetWorked = h.confirmation.checked === true && h.apiCalls.length === 2;
'''

ORIGINAL_ROUTE_BODY = r'''
  const h = buildHarness(nativeTarget(["/srv/app"]));
  equal(h.markup.includes('id="rsRestoreDestination"'), true, "destination selector exists");
  equal(h.markup.includes("Restaurar en la ruta original"), true, "original route option exists");
  equal(h.markup.includes('<option value=""'), false, "single source auto-selects without empty placeholder");
  h.destinationSelect.value = "original";
  if (h.destinationSelect.listeners.change) h.destinationSelect.listeners.change();
  equal(h.originalGroup.hidden, false, "original path chooser visible");
  equal(h.customGroup.hidden, true, "custom chooser hidden");
  equal(h.targetPathInput.required, false, "custom input no longer required");
  h.originalSelect.value = "/srv/app";
  await h.submit();
  equal(h.apiCalls.length, 1, "original route submits without destructive consent");
  equal(h.apiCalls[0].body.restore_target_path, "/srv/app", "exact configured Linux path sent");
  equal(h.apiCalls[0].body.force_overwrite, false, "no forced overwrite");
  out.path = h.apiCalls[0].body.restore_target_path;
'''

WINDOWS_PATH_BODY = r'''
  const h = buildHarness(nativeTarget(["C:\\Users\\Datos & Co\\app"]));
  h.destinationSelect.value = "original";
  if (h.destinationSelect.listeners.change) h.destinationSelect.listeners.change();
  equal(h.markup.includes("C:\\Users\\Datos &amp; Co\\app"), true, "path markup is HTML-escaped");
  equal(h.markup.includes("Datos & Co"), false, "no unescaped raw ampersand path rendered");
  h.originalSelect.value = "C:\\Users\\Datos & Co\\app";
  await h.submit();
  equal(h.apiCalls.length, 1, "original route submits");
  equal(h.apiCalls[0].body.restore_target_path, "C:\\Users\\Datos & Co\\app", "Windows path sent intact");
  out.path = h.apiCalls[0].body.restore_target_path;
'''

MULTI_SOURCE_BODY = r'''
  const h = buildHarness(nativeTarget(["/srv/one", "/srv/two"]));
  equal(h.markup.includes("/srv/one"), true, "first source listed");
  equal(h.markup.includes("/srv/two"), true, "second source listed");
  equal(h.markup.includes('<option value=""'), true, "multi sources get an explicit-choice placeholder");
  h.destinationSelect.value = "original";
  if (h.destinationSelect.listeners.change) h.destinationSelect.listeners.change();
  h.originalSelect.value = "";
  await h.submit();
  equal(h.apiCalls.length, 0, "multiple sources blocked without explicit choice");
  equal(h.status.textContent.length > 0, true, "blocked status explains the missing choice");
  h.originalSelect.value = "/srv/two";
  await h.submit();
  equal(h.apiCalls.length, 1, "explicit choice submits");
  equal(h.apiCalls[0].body.restore_target_path, "/srv/two", "chosen path sent, not all sources");
  out.path = h.apiCalls[0].body.restore_target_path;
'''

EMPTY_SOURCE_BODY = r'''
  const h = buildHarness(nativeTarget([]));
  equal(h.markup.includes("Restaurar en la ruta original"), false, "no original option without configured sources");
  equal(h.markup.includes('id="rsRestoreOriginalGroup"'), false, "no original chooser without sources");
  equal(h.markup.includes('id="rsRestoreDestination"'), true, "destination selector still present with custom route");
  h.targetPathInput.value = "/tmp/restore-dir";
  await h.submit();
  equal(h.apiCalls.length, 1, "custom route default submits with empty sources");
  equal(h.apiCalls[0].body.restore_target_path, "/tmp/restore-dir", "custom path sent");
  out.path = h.apiCalls[0].body.restore_target_path;
'''

DOCKER_TARGET_BODY = r'''
  const h = buildHarness(dockerTarget());
  equal(h.markup.includes('id="rsRestoreDestination"'), false, "no destination selector for docker targets");
  equal(h.markup.includes("Servicios durante la restauración"), true, "service controls remain for docker");
  equal(h.markup.includes('name="restore_target_path"'), false, "no restore_target_path field for docker");
  h.overwriteSelect.value = "true";
  if (h.overwriteSelect.listeners.change) h.overwriteSelect.listeners.change();
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  await h.submit();
  equal(h.apiCalls.length, 1, "docker restore submits");
  equal("restore_target_path" in h.apiCalls[0].body, false, "no restore_target_path field for docker");
  equal(h.apiCalls[0].body.stop_containers, false, "hot mode keeps containers running");
  out.force = h.apiCalls[0].body.force_overwrite;
'''

MANUAL_PERMISSIONS_BODY = r'''
  const h = buildHarness(nativeTarget(["/srv/app"]));
  h.targetPathInput.value = "/tmp/restore-dir";
  h.mapRadio.checked = true;
  h.preserveRadio.checked = false;
  h.fire("rsOwnershipMap", "change");
  h.advancedTextarea.value = "{ not json";
  await h.submit();
  equal(h.apiCalls.length, 0, "invalid manual permissions stay blocked");
  equal(h.overwriteSelect.value, "false", "preserve overwrite selected");
  equal(h.confirmRow.hidden, false, "map mode still requires its own confirmation");
  equal(h.confirmText.textContent.includes("permisos"), true, "confirmation text covers manual permissions");
  equal(h.confirmText.textContent.includes("solo a los archivos que se restauren"), true, "manual consent scoped to newly restored files");
  equal(h.confirmText.textContent.includes("permisos manuales al target"), false, "manual consent does not promise applying to existing target files");
  h.button.disabled = true;
  await h.submit();
  equal(h.apiCalls.length, 0, "confirmation not yet given");
  h.advancedTextarea.value = JSON.stringify({ schema_version: 1, mode: "map", mappings: { "compose:app:datos": "1000:1000" } });
  if (h.advancedTextarea.listeners.input) h.advancedTextarea.listeners.input();
  h.button.disabled = true;
  await h.submit();
  equal(h.apiCalls.length, 0, "map mode blocked until checkbox consent");
  h.confirmation.checked = true;
  if (h.confirmation.listeners.change) h.confirmation.listeners.change();
  equal(h.button.disabled, false, "map mode ready after permissions consent");
  await h.submit();
  equal(h.apiCalls.length, 1, "map mode submits after consent");
  equal(h.apiCalls[0].body.restore_ownership.confirmation, "confirmed", "manual permissions consent recorded");
  equal(h.apiCalls[0].body.force_overwrite, false, "manual permissions do not force overwrite");
  out.mode = h.apiCalls[0].body.restore_ownership.mode;
'''


class RestoreModalHarnessTests(unittest.TestCase):
    def setUp(self):
        self.source, self.restore_code = load_restore_source()
        self.script_prelude = HARNESS_PRELUDE + HELPER_AND_RESTORE + self.restore_code + HARNESS_DRIVER_PREP

    def run_scenario(self, body):
        return run_node(self.script_prelude + RUNNER_PREFIX + body + RUNNER_SUFFIX)

    def assert_run(self, body):
        try:
            return self.run_scenario(body)
        except RuntimeError as error:
            if shutil.which("node") is None:
                self.skipTest(f"Node.js is not installed ({error})")
            self.fail(str(error))

    def test_preserve_submits_without_confirmation_and_sends_force_false(self):
        result = self.assert_run(PRESERVE_SUBMIT_BODY)
        self.assertFalse(result["force"])
        self.assertTrue(result["confirmRowHidden"])

    def test_replacement_requires_explicit_confirmation_and_sends_force_true(self):
        result = self.assert_run(REPLACE_CONSENT_BODY)
        self.assertTrue(result["force"])

    def test_toggling_overwrite_resets_consent_and_hides_warning(self):
        result = self.assert_run(TOGGLE_RESET_BODY)
        self.assertTrue(result["resetWorked"])

    def test_native_original_route_sends_exact_configured_linux_path(self):
        result = self.assert_run(ORIGINAL_ROUTE_BODY)
        self.assertEqual(result["path"], "/srv/app")

    def test_native_original_route_preserves_windows_paths_verbatim(self):
        result = self.assert_run(WINDOWS_PATH_BODY)
        self.assertEqual(result["path"], "C:\\Users\\Datos & Co\\app")

    def test_native_multiple_original_sources_require_explicit_choice(self):
        result = self.assert_run(MULTI_SOURCE_BODY)
        self.assertEqual(result["path"], "/srv/two")

    def test_unc_sources_keep_custom_route_with_explicit_limitation(self):
        result = self.assert_run(r'''
  const h = buildHarness(nativeTarget(["\\\\server\\share\\app"]));
  equal(h.markup.includes('<option value="original">'), false, "unsupported UNC originals are not offered");
  equal(h.markup.includes("UNC"), true, "original-route limitation is explicit");
  h.targetPathInput.value = "\\\\server\\share\\restore";
  await h.submit();
  equal(h.apiCalls.length, 1, "custom UNC destination stays available");
  equal(h.apiCalls[0].body.restore_target_path, "\\\\server\\share\\restore", "custom UNC path is intact");
  out.customUnc = true;
''')
        self.assertTrue(result["customUnc"])

    def test_empty_sources_keep_custom_default_without_original_option(self):
        result = self.assert_run(EMPTY_SOURCE_BODY)
        self.assertEqual(result["path"], "/tmp/restore-dir")

    def test_docker_target_keeps_existing_request_contract(self):
        result = self.assert_run(DOCKER_TARGET_BODY)
        self.assertTrue(result["force"])

    def test_manual_permissions_invalid_stay_blocked_until_explicit_consent(self):
        result = self.assert_run(MANUAL_PERMISSIONS_BODY)
        self.assertEqual(result["mode"], "map")

    def test_consent_resets_when_destination_scope_changes_after_confirmation(self):
        result = self.assert_run(SCOPE_RESET_DESTINATION_BODY)
        self.assertTrue(result["rescope"])

    def test_consent_resets_when_custom_path_changes_after_confirmation(self):
        result = self.assert_run(SCOPE_RESET_PATH_BODY)
        self.assertTrue(result["pathReset"])

    def test_consent_resets_when_mapping_scope_changes_after_confirmation(self):
        result = self.assert_run(SCOPE_RESET_MAPPINGS_BODY)
        self.assertTrue(result["mappingReset"])


if __name__ == "__main__":
    unittest.main()
