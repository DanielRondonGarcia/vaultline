import json
import shutil
import subprocess
import tempfile
import unittest
import re
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "index.html"
UI_CSS_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "styles" / "components.css"
UI_APP_CSS_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "styles" / "app.css"
README_PATH = Path(__file__).resolve().parents[1] / "README.md"
NOTIFICATION_GUIDE_PATH = Path(__file__).resolve().parents[1] / "doc" / "backup-failure-notifications.md"


SMTP_TEST_DIALOG_MOCK_PRELUDE = r'''
const escapeHtml = value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;");
const state = {
  notificationChannels: [
    { id: "smtp-1", name: "Sistemas", channel_type: "smtp", enabled: true,
      config_summary: { host: "mail.internal", port: 587, from: "backup@example.test", to: ["oncall@example.test", "admin@example.test"], security: "starttls" } },
    { id: "webhook-1", name: "Webhook", channel_type: "webhook", enabled: true,
      config_summary: { destination: "https://hooks.example.test" } },
  ],
  notificationChannelEditor: null,
  notificationChannelFeedback: { message: "Canal creado.", type: "success" },
};
let markup = "";
const events = {};
const apiCalls = [];
const errorElement = { hidden: true, textContent: "" };
const sendButton = { disabled: false, textContent: "", isConnected: true };
const customInput = { value: "", disabled: true, isConnected: true };
const customGroup = { hidden: true, isConnected: true };
const modeRadios = [
  { dataset: { notificationTestMode: "configured" }, checked: true },
  { dataset: { notificationTestMode: "custom" }, checked: false },
];
const checkboxes = [
  { value: "oncall@example.test", checked: false, disabled: false },
  { value: "admin@example.test", checked: false, disabled: false },
];
const toNodeList = values => {
  const list = { length: values.length };
  list.forEach = callback => { for (let index = 0; index < list.length; index += 1) callback(list[index], index, list); };
  list.item = index => (index >= 0 && index < list.length) ? list[index] : null;
  for (let index = 0; index < values.length; index += 1) list[index] = values[index];
  return list;
};
const form = {
  dataset: { notificationTestForm: "smtp-1" },
  querySelector(selector) {
    if (selector === "[data-notification-test-submit]") return sendButton;
    if (selector === "[data-notification-test-error]") return errorElement;
    if (selector === "[data-notification-test-custom]") return customInput;
    if (selector === "[data-notification-test-custom-group]") return customGroup;
    return null;
  },
  querySelectorAll(selector) {
    if (selector === "[data-notification-test-mode]") return toNodeList(modeRadios);
    if (selector === "[data-notification-test-recipient]") return toNodeList(checkboxes);
    return toNodeList([]);
  },
  closest() { return dialog; },
  addEventListener(name, handler) { events[name] = handler; },
};
const dialog = {
  open: false,
  showModal() { this.open = true; },
  close() { this.open = false; },
  querySelector(selector) {
    if (selector === "[data-notification-test-error]") return errorElement;
    if (selector.startsWith("[data-notification-test-form]")) return form;
    return null;
  },
};
const openButton = {
  dataset: { notificationTestOpen: "smtp-1" },
  addEventListener(name, handler) { events[`open:${name}`] = handler; },
};
const closeButton = {
  closest() { return dialog; },
  addEventListener(name, handler) { events[`close:${name}`] = handler; },
};
const container = {
  set innerHTML(value) { markup = value; },
  querySelector() { return null; },
  querySelectorAll(selector) {
    if (selector === "[data-notification-test-open]") return [openButton];
    if (selector === "[data-notification-test-close]") return [closeButton];
    if (selector === "[data-notification-test-form]") return [form];
    return [];
  },
};
const document = {
  getElementById(id) {
    if (id === "notificationChannelManager") return container;
    if (id === "notificationTestDialog-smtp-1") return dialog;
    return null;
  },
};
const apiPost = async (path, body) => {
  apiCalls.push({ path, body });
  return { recipients: ["oncall@example.test", "admin@example.test"] };
};
'''


class SnapshotExplorerUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.css = UI_CSS_PATH.read_text(encoding="utf-8")
        cls.app_css = UI_APP_CSS_PATH.read_text(encoding="utf-8")

    def test_worker_commands_modal_removes_http_warning_and_keeps_renewal_compose_markers(self):
        self.assertNotIn("HTTP no es confidencial", self.source)
        for marker in (
            "data-renew-enrollment=",
            "Renovar enrollment",
            "Renovacion de enrollment",
            "docker-compose.yml",
            "/tmp:/tmp",
        ):
            self.assertIn(marker, self.source)

    def test_snapshot_explorer_contains_v2_routes_and_legacy_fallbacks(self):
        for route in ("/api/v2/targets/", "/snapshots`,", "/api/v2/jobs/", "/cancel`,"):
            self.assertIn(route, self.source)
        for operation in ('"browse"', '"search"', '"dump"'):
            self.assertIn(operation, self.source)
        self.assertIn("/api/v1/targets/${encodeURIComponent(target.id)}/snapshots", self.source)
        self.assertIn("/snapshot-ls", self.source)
        self.assertIn("/snapshot-dump", self.source)
        self.assertIn("/api/v1/jobs/${encodeURIComponent(request.jobId)}", self.source)

    def test_snapshot_about_action_uses_v2_polling_and_renders_safe_logical_details(self):
        for marker in (
            'data-snapshot-about',
            '>Información</button>',
            'requestSnapshotOperation(request, "about"',
            '/api/v2/targets/${encodeURIComponent(request.targetId)}/${operation}',
            'Tamaño lógico/restaurable',
            'total_file_count',
            'snapshots_count',
            'Redis (cache hit)',
            'renderSnapshotAboutLoading',
            'data-snapshot-about-open-browser',
            'La consulta fue cancelada.',
        ):
            self.assertIn(marker, self.source)
        about = re.search(r"async function loadSnapshotAbout\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(about)
        about_source = about.group(0)
        for marker in (
            'beginSnapshotRequest("about", selectedId, null)',
            'request_id: `ui-${request.seq}`',
            'isCurrentSnapshotRequest(request)',
            'finishSnapshotRequest(request)',
        ):
            self.assertIn(marker, about_source)
        self.assertIn('snapshot-poll-timeout', self.source)
        navigation = re.search(r"function handleSnapshotNavigationClick\(event\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(navigation)
        self.assertIn('void loadSnapshotAbout(about.dataset.snapshotId)', navigation.group(0))
        self.assertIn('!event.target.closest("[data-snapshot-restore], [data-snapshot-about]")', navigation.group(0))
        self.assertIn('escapeHtml(source)', self.source)
        self.assertIn('metadataValue(normalized.created_at ? fmtDate(normalized.created_at) : "")', self.source)

    def test_snapshot_about_uses_bounded_page_session_cache_and_honest_source(self):
        for marker in (
            "SNAPSHOT_ABOUT_SESSION_CACHE_LIMIT = 24",
            "const snapshotAboutSessionCache = new Map()",
            "snapshotAboutSessionCacheKey(targetId, snapshotId)",
            "getSnapshotAboutSessionCache(state.target.id, selectedId)",
            "setSnapshotAboutSessionCache(state.target.id, selectedId, state.about)",
            "snapshotAboutSessionCache.size > SNAPSHOT_ABOUT_SESSION_CACHE_LIMIT",
            'source: "browser-session-cache"',
            'normalized.source === "browser-session-cache"',
            "if (result && result.changed) clearSnapshotAboutSessionCache(current.target.id)",
            "if (result && result.changed) clearSnapshotAboutSessionCache(state.target.id)",
        ):
            self.assertIn(marker, self.source)
        about = re.search(r"async function loadSnapshotAbout\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(about)
        about_source = about.group(0)
        self.assertIn("invalidateSnapshotRequest(false);", about_source)
        self.assertIn("const cachedAbout = bypassSessionCache ? null : getSnapshotAboutSessionCache(state.target.id, selectedId);", about_source)
        self.assertIn("if (cachedAbout)", about_source)
        self.assertLess(about_source.index("getSnapshotAboutSessionCache"), about_source.index("requestSnapshotOperation"))

    def test_snapshot_about_retry_bypasses_page_session_cache(self):
        retry = re.search(r"function retrySnapshotAction\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(retry)
        self.assertIn('loadSnapshotAbout(state.currentSnapshot, { bypassSessionCache: true });', retry.group(0))
        about = re.search(r"async function loadSnapshotAbout\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(about)
        self.assertIn("const bypassSessionCache = options.bypassSessionCache === true;", about.group(0))
        self.assertIn("const cachedAbout = bypassSessionCache ? null", about.group(0))

    def test_target_stats_action_is_restic_only_and_cache_first_with_bounded_job_refresh(self):
        for marker in (
            'data-action="target-stats"',
            ">Ver stats</button>",
            'api/v1/targets/${encodeURIComponent(request.targetId)}/stats',
            'api/v1/targets/${encodeURIComponent(request.targetId)}/stats-sync',
            'TARGET_STATS_MODES = ["raw-data", "blobs-per-file"]',
            'Fuente:</strong> Registro persistido',
            'Última actualización:</strong>',
            'raw-data representa almacenamiento físico/deduplicado',
            'blobs-per-file',
            'connectJobEvents(request.jobId',
            'Actualizando por polling',
            'state.currentRole === "admin" || state.currentRole === "operator"',
        ):
            self.assertIn(marker, self.source)
        actions = re.search(r"function bindTargetActions\(\).*?\n    \}", self.source, re.S)
        self.assertIsNotNone(actions)
        self.assertIn('else if (action === "target-stats") { await openTargetStats(target, e.currentTarget); }', actions.group(0))
        self.assertNotIn('data-action="target-stats"', re.search(r"function renderSnapshotList\(\).*?\n    \}", self.source, re.S).group(0))

    def test_snapshot_catalog_sync_runs_after_persisted_catalog_load_and_surfaces_failures(self):
        modal = re.search(r"async function openSnapshotsModal\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(modal)
        modal_source = modal.group(0)
        self.assertIn("void loadSnapshotsList(target).then", modal_source)
        self.assertIn("void refreshSnapshotCatalog();", modal_source)
        refresh = re.search(r"async function refreshSnapshotCatalog\(\) \{.*?\n    \}\n\n    function showSnapshotOperationError", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        for marker in ("snapshot-sync-failed", "snapshot-sync-incomplete", "state.catalogError", "snapshot-poll-timeout"):
            self.assertIn(marker, refresh_source)

    def test_target_worker_controls_only_allow_eligible_workers_and_patch_reassignment(self):
        targets = re.search(r"function renderTargets\(content\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(targets)
        targets_source = targets.group(0)
        self.assertIn('filter(w => w.status === "online")', targets_source)
        self.assertIn("No hay workers elegibles online", targets_source)
        edit = re.search(r"function openEditTargetModal\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(edit)
        edit_source = edit.group(0)
        self.assertIn('id="etWorkerId"', edit_source)
        self.assertIn('worker_id: document.getElementById("etWorkerId").value', edit_source)
        self.assertIn("no elegible", edit_source)
        self.assertIn("Error guardando target", edit_source)

    def test_target_runtime_options_follow_worker_capabilities(self):
        helpers = re.search(
            r"(    const TARGET_RUNTIME_DEFINITIONS = .*?)\n\n    function renderTargets",
            self.source,
            re.S,
        )
        self.assertIsNotNone(helpers)
        for marker in (
            "runtimeCapabilityValues",
            "workerRuntimeCapabilities",
            "supportedTargetRuntimeTypes",
            "applyTargetRuntimeOptions",
            "preserveUnsupported",
            "clearDockerSelection",
            "updateCreateRuntimeOptions",
        ):
            self.assertIn(marker, helpers.group(1) + self.source)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = helpers.group(1) + r'''
const select = () => ({ value: "docker", innerHTML: "", disabled: false });
const native = select();
const docker = select();
const legacy = select();
        const multiple = select();
        const objectNative = select();
        const flatString = select();
        const nestedList = select();
        const nestedObject = select();
        const nativeResult = applyTargetRuntimeOptions(native, { labels: { runtime_type: "native" } });
        const dockerResult = applyTargetRuntimeOptions(docker, { labels: { runtime_kind: "docker" } });
        const legacyResult = applyTargetRuntimeOptions(legacy, { labels: { zone: "legacy" } });
        const multipleResult = applyTargetRuntimeOptions(multiple, { labels: { capabilities: '["docker","native"]' } });
        const objectNativeResult = applyTargetRuntimeOptions(objectNative, { labels: { capabilities: '{"NATIVE": true}' } });
        const flatStringResult = applyTargetRuntimeOptions(flatString, { labels: { capabilities: "docker native" } });
        const nestedListResult = applyTargetRuntimeOptions(nestedList, { labels: { capabilities: "[[\"native\"]]" } });
        const nestedObjectResult = applyTargetRuntimeOptions(nestedObject, { labels: { capabilities: "[{\"NATIVE\": true}]" } });
        const kubernetesCapabilities = workerRuntimeCapabilities({ labels: { capabilities: '{"k8s": true}' } });
        process.stdout.write(JSON.stringify({
  native: [nativeResult.runtimeType, native.innerHTML, native.disabled],
  docker: [dockerResult.runtimeType, docker.innerHTML, docker.disabled],
  legacy: [legacyResult.runtimeType, legacy.innerHTML, legacy.disabled],
          multiple: [multipleResult.runtimeType, multiple.innerHTML, multiple.disabled],
          objectNative: [objectNativeResult.runtimeType, objectNative.innerHTML, objectNative.disabled],
          flatString: [flatStringResult.runtimeType, flatString.innerHTML, flatString.disabled],
          nestedList: [nestedListResult.runtimeType, nestedList.innerHTML, nestedList.disabled, nestedListResult.supported],
          nestedObject: [nestedObjectResult.runtimeType, nestedObject.innerHTML, nestedObject.disabled, nestedObjectResult.supported],
          kubernetes: Array.from(kubernetesCapabilities.advertised),
        }));
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["native"][0], "native")
        self.assertNotIn("Docker / Compose", output["native"][1])
        self.assertIn("Filesystem (worker nativo)", output["native"][1])
        self.assertEqual(output["docker"][0], "docker")
        self.assertNotIn("Filesystem (worker nativo)", output["docker"][1])
        self.assertEqual(output["legacy"][0], "docker")
        self.assertNotIn("Filesystem (worker nativo)", output["legacy"][1])
        self.assertEqual(output["multiple"][0], "docker")
        self.assertIn("Docker / Compose", output["multiple"][1])
        self.assertIn("Filesystem (worker nativo)", output["multiple"][1])
        self.assertEqual(output["objectNative"][0], "native")
        self.assertNotIn("Docker / Compose", output["objectNative"][1])
        self.assertIn("Filesystem (worker nativo)", output["objectNative"][1])
        self.assertEqual(output["flatString"][0], "docker")
        self.assertIn("Docker / Compose", output["flatString"][1])
        self.assertIn("Filesystem (worker nativo)", output["flatString"][1])
        for nested in ("nestedList", "nestedObject"):
            self.assertEqual(output[nested][0], "")
            self.assertTrue(output[nested][2])
            self.assertNotIn("Filesystem (worker nativo)", output[nested][1])
            self.assertEqual(output[nested][3], [])
        self.assertEqual(output["kubernetes"], ["kubernetes"])

    def test_stale_target_inventory_cannot_restore_docker_state_after_native_switch(self):
        clear = re.search(
            r"(function clearDockerSelection\(\) \{.*?\n\s+\})(?=\n\s+async function refreshProjects)",
            self.source,
            re.S,
        )
        refresh = re.search(
            r"(async function refreshProjects\(\) \{.*?\n\s+\})(?=\n\s+function renderProjectSummary)",
            self.source,
            re.S,
        )
        self.assertIsNotNone(clear)
        self.assertIsNotNone(refresh)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = f'''
let projectRefreshGeneration = 0;
let volumeSelectionState = {{ stacks: {{}}, expanded: {{}} }};
const runtimeSelect = {{ value: "docker" }};
const workerSelect = {{ value: "worker-docker" }};
const projectSelect = {{ innerHTML: "", value: "" }};
const summary = {{ textContent: "", innerHTML: "" }};
const volumeRenders = [];
const renderVolumeCheckboxes = project => volumeRenders.push(project);
const renderProjectSummary = project => {{ summary.textContent = project.name; }};
const escapeHtml = value => value;
let resolveInventory;
const fetchWorkerInventory = () => new Promise(resolve => {{ resolveInventory = resolve; }});
{clear.group(1)}
{refresh.group(1)}
(async () => {{
  const staleRequest = refreshProjects();
  runtimeSelect.value = "native";
  clearDockerSelection();
  resolveInventory({{ inventory: {{ docker_available: true, compose_project_details: [{{ name: "stale-docker" }}] }} }});
  await staleRequest;
  process.stdout.write(JSON.stringify({{
    project: projectSelect.innerHTML,
    summary: summary.textContent,
    volumes: volumeRenders.map(project => project ? project.name : null),
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["project"], '<option value="">-- Seleccionar compose project --</option>')
        self.assertIn("oculta", output["summary"])
        self.assertIn("nativos.", output["summary"])
        self.assertNotIn("stale-docker", output["summary"])
        self.assertEqual(output["volumes"], [None, None])

    def test_stale_edit_inventory_cannot_restore_docker_state_after_native_switch(self):
        edit_loader = re.search(
            r"(const loadEditVolumeSelection = async \(\) => \{.*?\n\s+\};)\n\s+const updateEditWorkerEligibility",
            self.source,
            re.S,
        )
        self.assertIsNotNone(edit_loader)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = f'''
let editVolumeRequestId = 0;
let editVolumeSelectionReady = false;
const editVolumeState = {{ stacks: {{}}, expanded: {{}} }};
const editRuntimeSelect = {{ value: "docker" }};
const editWorkerSelect = {{ value: "worker-old" }};
const editVolumeStatus = {{ textContent: "", className: "" }};
const editVolumeList = {{ innerHTML: "" }};
const editTargetVolumeList = {{ dataset: {{}} }};
const document = {{ getElementById: id => id === "editTargetVolumeList" ? editTargetVolumeList : null }};
const target = {{ compose_project: "stale-docker", volume_targets: [] }};
const renderCalls = [];
const renderVolumeCheckboxes = project => renderCalls.push(project);
const getTargetVolumeSelectionKeys = () => new Set();
const getProjectVolumeCandidates = project => project.volume_candidates || [];
const getWorkerComposeProjects = () => [{{ name: "stale-docker", volume_candidates: [{{ source: "stale-source" }}] }}];
const setEditVolumeStatus = (message, warning = false) => {{ editVolumeStatus.textContent = message; editVolumeStatus.className = warning ? "warning" : "helper"; }};
let resolveInventory;
const fetchWorkerInventory = () => new Promise(resolve => {{ resolveInventory = resolve; }});
{edit_loader.group(1)}
(async () => {{
  const staleRequest = loadEditVolumeSelection();
  editWorkerSelect.value = "worker-new";
  editRuntimeSelect.value = "native";
  const nativeRequest = loadEditVolumeSelection();
  resolveInventory({{ inventory: {{ docker_available: true, compose_project_details: [{{ name: "stale-docker" }}] }} }});
  await Promise.all([staleRequest, nativeRequest]);
  process.stdout.write(JSON.stringify({{
    ready: editVolumeSelectionReady,
    status: editVolumeStatus.textContent,
    list: editVolumeList.innerHTML,
    renderCalls: renderCalls.length,
    requestId: editVolumeRequestId,
  }}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertFalse(output["ready"])
        self.assertIn("runtime nativo", output["status"])
        self.assertIn("oculta", output["list"])
        self.assertEqual(output["renderCalls"], 0)
        self.assertEqual(output["requestId"], 2)

    def test_target_path_storage_is_hidden_for_native_and_preserved_on_edit(self):
        targets = re.search(r"function renderTargets\(content\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(targets)
        targets_source = targets.group(0)
        for marker in (
            'data-target-docker-only="true"',
            'id="targetPathStorage"',
            "Ruta remota del target (opcional)",
        ):
            self.assertIn(marker, targets_source)
        self.assertIn(
            'path_storage: runtimeType === "native" ? null : emptyToNull(document.getElementById("targetPathStorage").value)',
            self.source,
        )
        edit = re.search(r"function openEditTargetModal\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(edit)
        edit_source = edit.group(0)
        for marker in (
            'id="etPathStorage"',
            'value="${escapeHtml(target.path_storage || "")}"',
            'if (targetUpdate.runtime_type !== "native") targetUpdate.path_storage = emptyToNull(document.getElementById("etPathStorage").value)',
        ):
            self.assertIn(marker, edit_source)

    def test_native_target_form_hides_mode_and_uses_accessible_path_editor(self):
        self.assertIn('data-target-docker-only="mode"', self.source)
        self.assertIn('data-target-docker-only="true"', self.source)
        self.assertIn('data-path-editor="create"', self.source)
        self.assertIn('data-path-editor="edit"', self.source)
        self.assertIn('aria-live="polite"', self.source)
        self.assertIn('remove.setAttribute("aria-label", `Quitar ruta ${path}`)', self.source)
        self.assertIn('Agregar ruta', self.source)
        self.assertIn('validateFilesystemPath', self.source)
        self.assertIn('backup_mode: runtimeType === "native" ? "hot"', self.source)
        self.assertIn('setRuntimeSections(editRuntimeSelect.value, overlay)', self.source)
        self.assertNotIn('id="targetFilesystemPaths"', self.source)
        self.assertNotIn('id="etFilesystemPaths"', self.source)

    def test_removing_filesystem_path_restores_focus_to_adjacent_control(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")

        editor = re.search(
            r"function validateFilesystemPath\(path\) \{.*?\n    function bindFilesystemSizeEstimate",
            self.source,
            re.S,
        )
        self.assertIsNotNone(editor)
        script = editor.group(0).split("    function bindFilesystemSizeEstimate", 1)[0]
        script += r'''
class Element {
  constructor(tag) { this.tag = tag; this.dataset = {}; this.children = []; this.listeners = {}; this.style = {}; this.value = ""; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  focus() { focused = this; }
  querySelectorAll(selector) { return selector === "[data-path-remove]" ? this.children.flatMap(row => row.children.filter(child => child.dataset.pathRemove !== undefined)) : []; }
}
let focused;
const elements = new Map();
const addButton = new Element("button");
global.document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); },
  createElement(tag) { return new Element(tag); },
  querySelector() { return addButton; },
};
const run = (initialPaths, removeIndex) => {
  elements.clear();
  const editor = bindFilesystemPathEditor("target", initialPaths);
  const list = document.getElementById("targetFilesystemPathList");
  list.children[removeIndex].children.find(child => child.dataset.pathRemove !== undefined).listeners.click();
  const paths = editor.getPaths();
  const expectedFocus = paths.length
    ? list.children[Math.min(removeIndex, paths.length - 1)].children.find(child => child.dataset.pathRemove !== undefined)
    : document.getElementById("targetFilesystemPathInput");
  if (focused !== expectedFocus) throw new Error(`wrong focus after removing row ${removeIndex}`);
  return paths;
};
const results = {
  first: run(["/one", "/two", "/three"], 0),
  middle: run(["/one", "/two", "/three"], 1),
  last: run(["/one", "/two", "/three"], 2),
  only: run(["/one"], 0),
};
process.stdout.write(JSON.stringify(results));
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "first": ["/two", "/three"],
                "middle": ["/one", "/three"],
                "last": ["/one", "/two"],
                "only": [],
            },
        )

    def test_target_volume_selector_uses_source_candidates_and_sends_source_identity(self):
        for marker in (
            "Array.isArray(project.volume_candidates)",
            "candidate.anonymous !== true",
            "data-source=\"${escapeHtml(candidate.source || \"\")}\"",
            "Servicio:",
            "Contenedor:",
            "volume_sources: hasExplicitVolumeSources ? selectedSources : null",
            "function getSelectedVolumeSources()",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("Object.keys(runtimeVols).find(k => runtimeVols[k].bind === v)", self.source)

        volume_cell = re.search(r"function renderVolumeTargetsCell\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(volume_cell)
        volume_cell_source = volume_cell.group(0)
        self.assertIn("Object.entries(runtimeVols)", volume_cell_source)
        self.assertIn(".filter(([_, v]) => v && v.bind === p)", volume_cell_source)
        self.assertIn(".map(([name]) => name)", volume_cell_source)

    def test_edit_target_volume_selector_loads_inventory_preselects_and_preserves_unavailable_state(self):
        edit = re.search(r"function openEditTargetModal\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(edit)
        edit_source = edit.group(0)
        for marker in (
            "Volúmenes incluidos en el backup",
            'id="editTargetVolumeList"',
            'id="editTargetVolumeSummary"',
            "fetchWorkerInventory(workerId)",
            "getTargetVolumeSelectionKeys(target, candidates)",
            'targetUpdate.volume_sources = getSelectedVolumeSourcesFor("editTargetVolumeList")',
            "editVolumeSelectionReady",
            "No se pudo cargar el inventario del worker",
            "La selección actual se conservará",
        ):
            self.assertIn(marker, edit_source)
        for marker in (
            "target.runtime_volumes",
            "target.volume_targets",
            '<input type="checkbox" class="volume-checkbox"',
            'aria-expanded="${String(isExpanded)}"',
            "volume-inline-warning",
        ):
            self.assertIn(marker, self.source)

        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        helper = re.search(
            r"(function getTargetVolumeSelectionKeys\(target, candidates\) \{.*?\n    \})\n\n    function bindTargetForm",
            self.source,
            re.S,
        )
        self.assertIsNotNone(helper)
        script = """
const volumeCandidateKey = candidate => candidate.source || candidate.bind;
""" + helper.group(1) + r'''
const candidates = [
  { source: "source-a", bind: "/shared" },
  { source: "source-b", bind: "/shared" },
  { source: "source-c", bind: "/other" },
];
const bySource = [...getTargetVolumeSelectionKeys({
  volume_targets: ["/shared"],
  runtime_volumes: { "source-a": { bind: "/shared", mode: "rw" } },
}, candidates)];
const byLegacyPath = [...getTargetVolumeSelectionKeys({
  volume_targets: ["/shared"],
  runtime_volumes: {},
}, candidates)];
const byEmptyLegacy = [...getTargetVolumeSelectionKeys({ volume_targets: [], runtime_volumes: {} }, candidates)];
process.stdout.write(JSON.stringify({ bySource, byLegacyPath, byEmptyLegacy }));
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["bySource"], ["source-a"])
        self.assertEqual(set(output["byLegacyPath"]), {"source-a", "source-b"})
        self.assertEqual(set(output["byEmptyLegacy"]), {"source-a", "source-b", "source-c"})

    def test_blocked_targets_cannot_run_or_enable_on_ineligible_workers(self):
        for marker in (
            "target-execution-blocked",
            "execution_blocked",
            "worker_revoked",
            "worker_missing",
            "target_disabled",
            "enableBlocked",
            "toggleDisabled ? \"disabled\" : \"\"",
            "aria-label=\"${escapeHtml(toggleTitle)}\"",
            "state.currentRole === \"admin\"",
            "state.currentRole === \"admin\" || state.currentRole === \"operator\"",
            "startTargetsPolling();",
            "apiGet(\"/api/v1/targets\")",
        ):
            self.assertIn(marker, self.source)

    def test_blocked_target_rows_do_not_capture_fixed_menus_and_keep_admin_delete_action(self):
        blocked_styles = re.findall(
            r"\.table tbody tr\.target-execution-blocked(?: > td|:hover > td) \{(.*?)\}",
            self.css,
            re.S,
        )
        self.assertEqual(len(blocked_styles), 2)
        for style in blocked_styles:
            self.assertIn("background:", style)
            self.assertNotRegex(
                style,
                r"\b(?:filter|opacity|transform|perspective|contain|isolation|will-change)\s*:",
            )

        dropdown = re.search(r"\.dropdown-menu \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(dropdown)
        self.assertIn("position: fixed;", dropdown.group(0))
        self.assertIn("z-index: 1000;", dropdown.group(0))

        body = re.search(
            r"function renderTargetsTableBody\(preservedDropdown = captureTargetDropdownState\(\)\) \{.*?\n    function renderVolumeTargetsCell",
            self.source,
            re.S,
        )
        self.assertIsNotNone(body)
        body_source = body.group(0)
        admin_actions = re.search(
            r"\$\{canManageTargets \? `<button[^`]*data-action=\"edit\".*?data-action=\"delete\".*?` : \"\"\}",
            body_source,
            re.S,
        )
        self.assertIsNotNone(admin_actions)
        self.assertNotIn("executionBlocked", admin_actions.group(0))
        self.assertIn('data-name="${escapeHtml(t.name)}"', admin_actions.group(0))

        actions = re.search(
            r"function bindTargetActions\(\).*?\n    \}\n\n    function normalizeLiveBrowserPath",
            self.source,
            re.S,
        )
        self.assertIsNotNone(actions)
        self.assertIn(
            'else if (action === "delete") { openDeleteTargetModal(id, btn.dataset.name); }',
            actions.group(0),
        )

    def test_targets_table_has_fluid_desktop_columns_and_responsive_scroll_contract(self):
        table = re.search(r'<table class="table datatable targets-table">.*?</table>', self.source, re.S)
        self.assertIsNotNone(table)
        table_source = table.group(0)
        for marker in (
            "<colgroup>",
            'class="target-col-name"',
            'class="target-col-worker"',
            'class="target-col-compose"',
            'class="target-col-mode"',
            'class="target-col-volumes"',
            'class="target-col-retention"',
            'class="target-col-actions"',
            'class="target-col-cron"',
            'class="target-col-toggle"',
        ):
            self.assertIn(marker, table_source)
        self.assertRegex(
            self.css,
            r"\.targets-table \{\s*width: 100%;\s*min-width: 0;\s*table-layout: fixed;\s*\}",
        )
        column_widths = dict(
            re.findall(r"\.targets-table col\.(target-col-[\w-]+) \{ width: (\d+)%; \}", self.css)
        )
        self.assertEqual(
            set(column_widths),
            {
                "target-col-name",
                "target-col-worker",
                "target-col-compose",
                "target-col-mode",
                "target-col-volumes",
                "target-col-retention",
                "target-col-cron",
                "target-col-toggle",
                "target-col-actions",
            },
        )
        self.assertEqual(sum(map(int, column_widths.values())), 100)
        self.assertEqual(int(column_widths["target-col-actions"]), 10)
        self.assertNotIn("min-width: 1280px", self.css)
        for marker in (
            ".targets-table th.col-actions,",
            ".targets-table td.action-cell { display: table-cell;",
            ".targets-table .target-cron-cell",
            ".targets-table .vol-cell code",
            ".targets-table th, .targets-table td { padding-inline: 8px; }",
            ".targets-table .toggle-cell { white-space: normal; }",
        ):
            self.assertIn(marker, self.css)
        self.assertIn("overflow-x: auto", self.app_css)
        self.assertIn("min-width: 0", self.app_css)

    def test_targets_mobile_layout_keeps_cards_labels_filters_actions_and_logs_visible(self):
        table = re.search(r'<table class="table datatable targets-table">.*?</table>', self.source, re.S)
        self.assertIsNotNone(table)
        table_source = table.group(0)
        for marker in (
            'data-label="Nombre"',
            'data-label="Worker"',
            'data-label="Compose project"',
            'data-label="Modo"',
            'data-label="Vol&uacute;menes"',
            'data-label="Retenci&oacute;n"',
            'data-label="Cron"',
            'data-label="Activo"',
            'data-label="Acciones"',
            'class="empty-targets-row"',
        ):
            self.assertIn(marker, self.source)
        for marker in (
            'aria-label="Filtrar nombre"',
            'aria-label="Filtrar worker"',
            'aria-label="Filtrar compose"',
            'aria-label="Filtrar modo"',
            'aria-label="Filtrar retencion"',
            'aria-label="Filtrar cron"',
        ):
            self.assertIn(marker, table_source)
        mobile = re.search(r'@media \(max-width: 1200px\) \{.*?\n\}', self.css, re.S)
        self.assertIsNotNone(mobile)
        mobile_css = mobile.group(0)
        self.assertIn("@media (max-width: 1200px)", mobile_css)
        for marker in (
            ".targets-table {",
            "min-width: 0",
            ".targets-table > thead > tr.filter-row",
            "grid-template-columns: repeat(auto-fit",
            ".targets-table > tbody > tr:not(.target-log-row):not(.empty-targets-row)",
            "content: attr(data-label)",
            ".targets-table > tbody > tr.target-log-row",
            ".targets-table > tbody > tr.target-log-row > td::before { display: none;",
            ".action-cell .dropdown-trigger",
            "min-height: 44px",
            "overflow-wrap: anywhere",
        ):
            self.assertIn(marker, mobile_css)
        for marker in (
            ".targets-table > thead > tr.filter-row > th:nth-child(5)",
            ".targets-table > thead > tr.filter-row > th:nth-child(8)",
            ".targets-table > thead > tr.filter-row > th:nth-child(9)",
        ):
            self.assertIn(marker, mobile_css)
        self.assertIn("overflow-x: hidden", self.app_css)
        self.assertIn("min-width: 0", self.app_css)
        for marker in ("renderTargetLogDetailRow(t)", "positionTargetDropdown(trigger, menu)", "overflow-y: auto"):
            self.assertIn(marker, self.source if marker != "overflow-y: auto" else self.css)

    def test_native_targets_do_not_display_or_match_a_backup_mode(self):
        helper = re.search(r"function targetModeValue\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(helper)
        self.assertIn('return String(target && target.runtime_type || "docker").toLowerCase() === "native"', helper.group(0))
        self.assertIn('targetModeValue(t).toLowerCase().includes(f.mode.toLowerCase())', self.source)
        self.assertIn('const modeValue = targetModeValue(t);', self.source)
        self.assertIn('${escapeHtml(modeValue || "—")}', self.source)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        result = subprocess.run(
            [
                "node",
                "--input-type=commonjs",
                "--eval",
                helper.group(0)
                + '\nprocess.stdout.write(JSON.stringify(['
                  'targetModeValue({runtime_type:"native",backup_mode:"hot"}),'
                  'targetModeValue({runtime_type:"docker",backup_mode:"hot"}),'
                  'targetModeValue({backup_mode:"cold"})]));',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["", "hot", "cold"])

    def test_target_action_cell_keeps_table_layout_and_non_target_tables_keep_flex_actions(self):
        self.assertIsNone(re.search(r"(?m)^\.action-cell\s*\{\s*display:\s*flex;", self.css))
        self.assertIn(".targets-table td.action-cell { display: table-cell;", self.css)
        self.assertIn(".targets-table .action-cell > .dropdown { display: flex;", self.css)
        self.assertIn(".table:not(.targets-table) .action-cell { display: flex;", self.css)

    def test_target_polling_keeps_worker_display_state_without_heartbeat_rerenders(self):
        fingerprint = re.search(
            r"function workerFingerprint\(worker\) \{.*?\n    \}\n\n    async function revokeWorker",
            self.source,
            re.S,
        )
        self.assertIsNotNone(fingerprint)
        fingerprint_source = fingerprint.group(0)
        self.assertNotIn("last_seen_at", fingerprint_source)
        self.assertIn("worker.status", fingerprint_source)
        self.assertIn("worker.version", fingerprint_source)
        self.assertIn("worker.labels[key]", fingerprint_source)

        polling = re.search(r"function startTargetsPolling\(\) \{.*?\n    \}\n\n    function stopTargetsPolling", self.source, re.S)
        self.assertIsNotNone(polling)
        polling_source = polling.group(0)
        self.assertIn("state.workers = newWorkers;", polling_source)
        self.assertIn("newWorkers.map(workerFingerprint)", polling_source)
        self.assertIn("if ((targetsChanged || workersChanged) && state.currentView === \"targets\") renderTargetsTable();", polling_source)

    def test_target_action_dropdown_flips_clamps_and_scrolls_without_losing_button_semantics(self):
        position = re.search(r"function positionTargetDropdown\(trigger, menu\) \{.*?\n    \}\n\n    function bindTargetActions", self.source, re.S)
        self.assertIsNotNone(position)
        position_source = position.group(0)
        for marker in (
            "trigger.getBoundingClientRect()",
            "menu.getBoundingClientRect()",
            "window.innerHeight",
            "window.innerWidth",
            "aboveTop",
            "Math.max",
            "Math.min",
            "menu.style.top",
            "menu.style.left",
        ):
            self.assertIn(marker, position_source)
        for marker in (
            'aria-haspopup="menu"',
            'aria-expanded="false"',
            'role="menu"',
            "setTargetDropdownOpen(menu, false)",
            'target.closest(".dropdown")',
        ):
            self.assertIn(marker, self.source)
        for marker in ("max-height: calc(100vh - 16px)", "max-width: calc(100vw - 16px)", "overflow-y: auto"):
            self.assertIn(marker, self.css)

    def test_target_action_dropdown_survives_rerenders_and_keeps_keyboard_focus(self):
        body = re.search(
            r"function renderTargetsTableBody\(preservedDropdown = captureTargetDropdownState\(\)\) \{.*?\n    function renderVolumeTargetsCell",
            self.source,
            re.S,
        )
        self.assertIsNotNone(body)
        body_source = body.group(0)
        self.assertIn("restoreTargetDropdownState(preservedDropdown);", body_source)
        self.assertNotIn("closeTargetDropdowns()", body_source)

        view = re.search(r"function setView\(view\) \{.*?\n    function route", self.source, re.S)
        self.assertIsNotNone(view)
        view_source = view.group(0)
        for marker in ("preservedTargetDropdown", "captureTargetDropdownState()", "restoreTargetDropdownState(preservedTargetDropdown)"):
            self.assertIn(marker, view_source)
        self.assertIn("if (state.currentView !== view && currentLogJobId) closeLogPanel();", view_source)
        self.assertNotIn("reopenLogPanel(currentLogJobId", view_source)

        polling = re.search(r"function startTargetsPolling\(\) \{.*?\n    \}\n\n    function stopTargetsPolling", self.source, re.S)
        self.assertIsNotNone(polling)
        self.assertNotIn("closeTargetDropdowns", polling.group(0))
        for marker in ("ArrowDown", "ArrowUp", "Home", "End", "Escape", "focusTargetDropdownItem", 'role="menuitem"'):
            self.assertIn(marker, self.source)

    def test_target_logs_use_exact_label_and_inline_detail_rows(self):
        target_body = re.search(
            r"function renderTargetsTableBody\(.*?\) \{.*?\n    function renderVolumeTargetsCell",
            self.source,
            re.S,
        )
        self.assertIsNotNone(target_body)
        target_source = target_body.group(0)
        self.assertIn(">Ver ultimos logs</button>", target_source)
        self.assertIn('data-action="logs" data-id="${escapeHtml(t.id)}">Ver ultimos logs</button>', target_source)
        self.assertNotIn("${lastJob ?", target_source)
        self.assertNotIn(">Ver logs</button>", target_source)
        actions = re.search(r"function bindTargetActions\(\) \{.*?\n    function normalizeLiveBrowserPath", self.source, re.S)
        self.assertIsNotNone(actions)
        self.assertIn('else if (action === "logs") { await openLatestTargetLogs(id); }', actions.group(0))
        for marker in (
            "renderTargetLogDetailRow(t)",
            "return targetRow +",
            'data-target-log-row',
            '<td colspan="9">',
            'role="region"',
            'data-target-log-close',
            "function targetLogContentId",
            "function closeLogPanel()",
        ):
            self.assertIn(marker, self.source)

    def test_target_log_selection_is_singleton_and_reopens_without_stale_poll_updates(self):
        for marker in (
            "let currentLogTargetId = null;",
            "let currentLogJobSnapshot = null;",
            "if (currentLogTargetId && !selectedTarget) closeLogPanel();",
            "currentLogTargetId = null;",
            "currentLogJobSnapshot = job;",
            "reopenLogPanel(currentLogJobId, currentLogTargetId ||",
            "if (currentLogJobId !== jobId) return;",
            "renderTargetLogPanelInto(host, jobId, tid);",
            "ensureTargetLogFallback",
        ):
            self.assertIn(marker, self.source)
        self.assertGreaterEqual(self.source.count("if (currentLogJobId !== jobId) return;"), 2)

    def test_jobs_log_accordion_markers_remain_separate(self):
        for marker in (
            "function toggleJobLogAccordion(jobId, content)",
            "log-accordion-",
            "function closeJobLogAccordion()",
            "function pollAccordionJobLogs(jobId)",
            "async function loadAccordionJobLogs(jobId)",
            'data-view-logs',
            "renderJobLogsInto(job, container)",
        ):
            self.assertIn(marker, self.source)

    def test_initial_job_log_loaders_fetch_encoded_finite_detail_for_both_panels(self):
        target_loader = re.search(r"async function loadTargetJobLogs\(jobId\) \{.*?\n    \}", self.source, re.S)
        accordion_loader = re.search(r"async function loadAccordionJobLogs\(jobId\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(target_loader)
        self.assertIsNotNone(accordion_loader)
        for loader in (target_loader.group(0), accordion_loader.group(0)):
            self.assertIn("apiGet(`/api/v1/jobs/${encodeURIComponent(jobId)}/logs`)", loader)
            self.assertIn("if (!isActive()) return;", loader)
            self.assertIn("mergeJobProjection(job);", loader)
            self.assertIn("isTerminalJobStatus(job.status)", loader)
            self.assertIn("Finalizado", loader)

        target_open = re.search(r"function openLogPanel\(jobId, targetId\) \{.*?\n    \}\n\n    function closeLogPanel", self.source, re.S)
        accordion_open = re.search(r"function toggleJobLogAccordion\(jobId, content\) \{.*?\n    \}\n\n    function closeJobLogAccordion", self.source, re.S)
        self.assertIsNotNone(target_open)
        self.assertIsNotNone(accordion_open)
        self.assertIn("void loadTargetJobLogs(jobId);", target_open.group(0))
        self.assertIn("void loadAccordionJobLogs(jobId);", accordion_open.group(0))
        self.assertNotIn("if (existingJob && isTerminalJobStatus(existingJob.status))", target_open.group(0))
        self.assertNotIn("if (existingJob && isTerminalJobStatus(existingJob.status))", accordion_open.group(0))

    def test_manual_target_backup_uses_accessible_hot_cold_dialog_and_override_payload(self):
        for marker in (
            "function openBackupRunModal",
            'role="dialog" aria-modal="true"',
            'name="targetBackupMode"',
            'value="hot"',
            'value="cold"',
            'aria-live="polite"',
            "targetRunCancel",
            "targetRunExecute",
            'backup_mode: isNative ? "hot" : selectedMode',
            "El modo seleccionado aplica solo a esta ejecución",
        ):
            self.assertIn(marker, self.source)

    def test_manual_native_backup_hides_mode_choices_and_submits_hot(self):
        modal = re.search(r"function openBackupRunModal\(targetId, triggerButton\) \{.*?\n    \}\n\n    function runBackupNow", self.source, re.S)
        self.assertIsNotNone(modal)
        source = modal.group(0)
        self.assertIn('target.runtime_type === "native"', source)
        self.assertIn("Los backups del sistema de archivos se ejecutan sin detener contenedores.", source)
        self.assertIn('backup_mode: isNative ? "hot" : selectedMode', source)
        self.assertIn("executeButton.focus()", source)
        self.assertIn('role="alert" aria-live="polite"', source)

    def test_inactive_targets_keep_manual_backup_and_snapshot_restore_actions(self):
        for marker in (
            "function targetExecutionBlocked(target)",
            'target.blocked_reason !== "target_disabled"',
            "const schedulingDisabled = Boolean(t.scheduling_disabled ?? !t.enabled);",
            "const runDisabled = !canRunTargets || executionBlocked;",
            "Target inactivo: el cron está deshabilitado",
            "acciones manuales disponibles",
            'data-action="snapshots"',
            "if (targetExecutionBlocked(target) || ![\"admin\", \"operator\"].includes(state.currentRole)) return;",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("const runDisabled = !canRunTargets || executionBlocked || !t.enabled;", self.source)

    def test_snapshot_explorer_is_race_safe_and_cancellable(self):
        for marker in (
            "new AbortController()",
            "requestSequence",
            "request.controller.abort()",
            "isCurrentSnapshotRequest",
            "signal: request.controller.signal",
            "SNAPSHOT_POLL_ATTEMPTS",
            "SNAPSHOT_POLL_DELAYS",
        ):
            self.assertIn(marker, self.source)
        self.assertIn("setTimeout(() => {", self.source)
        self.assertIn("}, 250);", self.source)

    def test_snapshot_explorer_has_bounded_wall_clock_poll_budget(self):
        match = re.search(r"const SNAPSHOT_POLL_MAX_MS\s*=\s*(\d+)", self.source)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 60_000)

    def test_backup_completion_refreshes_catalog_and_modal_close_cleans_up(self):
        self.assertIn("function scheduleSnapshotCatalogRefreshAfterBackup(job)", self.source)
        self.assertIn('job.command !== "backup.run"', self.source)
        self.assertIn("SNAPSHOT_CATALOG_REFRESH_DELAYS", self.source)
        self.assertIn("loadSnapshotsList(current.target, { background: true })", self.source)
        self.assertIn("isCurrentSnapshotCatalogRequest(request)", self.source)
        close_modal = re.search(r"function closeSnapshotsModal\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(close_modal)
        self.assertIn("stopSnapshotCatalogRefresh();", close_modal.group(0))
        self.assertIn("abortSnapshotCatalogRequest();", close_modal.group(0))
        self.assertIn("abortSnapshotCatalogSync();", close_modal.group(0))

    def test_snapshot_explorer_manual_refresh_syncs_once_before_catalog_reload(self):
        refresh = re.search(r"async function refreshSnapshotCatalog\(\) \{.*?\n    \}\n\n    function showSnapshotOperationError", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        self.assertIn("/api/v1/targets/${encodeURIComponent(state.target.id)}/snapshots-sync", refresh_source)
        self.assertEqual(refresh_source.count("snapshots-sync"), 1)
        self.assertIn("beginSnapshotCatalogSyncRequest()", refresh_source)
        self.assertIn("if (!syncRequest) return;", refresh_source)
        self.assertIn("pollSnapshotJob(syncRequest,", refresh_source)
        self.assertIn("SNAPSHOT_POLL_MAX_MS", self.source)
        self.assertIn("await loadSnapshotsList(state.target);", refresh_source)
        self.assertIn("isSnapshotAbortError(error)", refresh_source)
        self.assertIn("void refreshSnapshotCatalog();", self.source)
        self.assertIn("useLegacyJobPolling", self.source)

    def test_snapshot_refresh_button_loader_is_scoped_and_cleans_up(self):
        controls = re.search(r"function setSnapshotCatalogRefreshBusy\(.*?\n    \}\n\n    function beginSnapshotCatalogRequest", self.source, re.S)
        self.assertIsNotNone(controls)
        controls_source = controls.group(0)
        for marker in (
            'data-snapshot-refresh-spinner',
            '.snapshot-refresh-button[aria-busy="true"] .snapshot-refresh-spinner',
            'aria-busy="false"',
            'visibility: hidden',
            'visibility: visible',
            'button.setAttribute("aria-busy", isBusy ? "true" : "false")',
            'button.disabled = isBusy',
            'SNAPSHOT_REFRESH_LABEL = "Refrescar lista de snapshots"',
            'SNAPSHOT_REFRESH_BUSY_LABEL = "Sincronizando snapshots..."',
        ):
            self.assertIn(marker, self.source)

        refresh = re.search(r"async function refreshSnapshotCatalog\(\) \{.*?\n    \}\n\n    function showSnapshotOperationError", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        self.assertIn("setSnapshotCatalogRefreshBusy(true, syncRequest);", refresh_source)
        self.assertIn("setSnapshotCatalogRefreshBusy(false, syncRequest);", refresh_source)
        self.assertNotIn("modalOverlay", refresh_source)
        self.assertNotIn("appLoader", refresh_source)

        close_modal = re.search(r"function closeSnapshotsModal\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(close_modal)
        self.assertIn("setSnapshotCatalogRefreshBusy(false);", close_modal.group(0))

    def test_snapshot_explorer_has_bounded_navigation_and_safe_states(self):
        self.assertIn("const SNAPSHOT_CACHE_LIMIT = 8", self.source)
        self.assertIn("const SNAPSHOT_PREFETCH_LIMIT = 2", self.source)
        self.assertIn("while (state.directoryCache.size > SNAPSHOT_CACHE_LIMIT)", self.source)
        self.assertIn("state.prefetchControllers.size >= SNAPSHOT_PREFETCH_LIMIT", self.source)
        for state in ("indexing", "empty", "no-match", "error", "stale", "canceled", "deleted", "blocked", "oversized"):
            self.assertTrue(f'{state}:' in self.source or f'"{state}":' in self.source)
        self.assertIn('data-state="${escapeHtml(kind)}"', self.source)
        for key in ("ArrowUp", "ArrowDown", "Home", "End", "Enter", "Escape"):
            self.assertIn(key, self.source)

    def test_snapshot_directory_pagination_has_accessible_bounded_control(self):
        for marker in (
            "const SNAPSHOT_PAGE_SIZE = 200",
            "const SNAPSHOT_MAX_DIRECTORY_ENTRIES = 10_000",
            "directoryLoadedLimit: SNAPSHOT_PAGE_SIZE",
            "directoryHasMore: false",
            "loadingMore: false",
            "data-snapshot-load-more",
            "Cargar más",
            'aria-busy="${state.loadingMore ? "true" : "false"}"',
            '${state.loadingMore ? "disabled" : ""}',
            ".snapshot-pagination-control",
            "justify-content: center",
        ):
            self.assertIn(marker, self.source)

        render = re.search(r"function renderSnapshotBrowser\(.*?\n    \}\n\n    function selectSnapshot", self.source, re.S)
        self.assertIsNotNone(render)
        render_source = render.group(0)
        self.assertIn("!state.searchActive && state.directoryHasMore", render_source)
        self.assertIn("state.loadingMore", render_source)
        self.assertIn("${loadMoreMarkup}", render_source)

    def test_snapshot_directory_load_more_increases_limit_replaces_response_and_stops_at_cap(self):
        browse = re.search(r"async function browseSnapshot\(.*?\n    \}\n\n    async function loadMoreSnapshotDirectory", self.source, re.S)
        self.assertIsNotNone(browse)
        browse_source = browse.group(0)
        for marker in (
            "const requestedLimit = cachedDirectory ? cachedDirectory.loadedLimit : SNAPSHOT_PAGE_SIZE",
            "max_entries: requestedLimit",
            "state.entries = resultEntries.slice(0, SNAPSHOT_MAX_DIRECTORY_ENTRIES)",
            "state.directoryHasMore = resultEntries.length >= requestedLimit && requestedLimit < SNAPSHOT_MAX_DIRECTORY_ENTRIES",
            "cacheSnapshotDirectory(state.currentSnapshot, normalizedPath, state.entries, requestedLimit, state.directoryHasMore, state.listingComplete)",
        ):
            self.assertIn(marker, browse_source)

        load_more = re.search(r"async function loadMoreSnapshotDirectory\(.*?\n    \}\n\n    async function searchSnapshot", self.source, re.S)
        self.assertIsNotNone(load_more)
        load_more_source = load_more.group(0)
        for marker in (
            "const nextLimit = Math.min(currentLimit + SNAPSHOT_PAGE_SIZE, SNAPSHOT_MAX_DIRECTORY_ENTRIES)",
            "state.currentPath",
            "state.loadingMore = true",
            "max_entries: nextLimit",
            "state.entries = resultEntries.slice(0, SNAPSHOT_MAX_DIRECTORY_ENTRIES)",
            "state.directoryHasMore = resultEntries.length >= nextLimit && nextLimit < SNAPSHOT_MAX_DIRECTORY_ENTRIES",
            "cacheSnapshotDirectory(snapshotId, normalizedPath, state.entries, nextLimit, state.directoryHasMore, state.listingComplete)",
            "state.loadingMore = false",
        ):
            self.assertIn(marker, load_more_source)

    def test_snapshot_directory_pagination_resets_on_navigation_and_search_and_prefetch_stays_small(self):
        for function_name, end_marker in (
            ("function selectSnapshot", "function moveSnapshotFocus"),
            ("async function browseSnapshot", "async function loadMoreSnapshotDirectory"),
            ("async function searchSnapshot", "function classifySnapshotDownloadFailure"),
        ):
            block = re.search(rf"{re.escape(function_name)}\(.*?\n    \}}\n\n    {re.escape(end_marker)}", self.source, re.S)
            self.assertIsNotNone(block)
            self.assertIn("resetSnapshotDirectoryState(state)", block.group(0))

        prefetch = re.search(r"function prefetchSnapshotDirectories\(.*?\n    \}\n\n    function openRestoreFromSnapshotModal", self.source, re.S)
        self.assertIsNotNone(prefetch)
        prefetch_source = prefetch.group(0)
        self.assertIn("max_entries: SNAPSHOT_PAGE_SIZE", prefetch_source)
        self.assertIn("resultEntries.length >= SNAPSHOT_PAGE_SIZE", prefetch_source)
        self.assertIn("loadedLimit: normalizedLimit", self.source)

    def test_snapshot_browser_distinguishes_confirmed_empty_from_failed_listing(self):
        success = re.search(r"function isSuccessfulSnapshotResult\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(success)
        self.assertIn("if (!normalized || normalized.error) return false", success.group(0))
        self.assertIn("listing_complete", success.group(0))

        render = re.search(r"function renderSnapshotBrowser\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(render)
        render_source = render.group(0)
        self.assertIn("!visibleEntries.length && !notice", render_source)
        self.assertIn("Listado confirmado", render_source)
        self.assertIn("El listado quedó incompleto y no se puede confirmar que la carpeta esté vacía.", render_source)

        for marker in (
            "classifySnapshotListingFailure",
            "El listado supera el límite configurado.",
            "El listado tardó demasiado porque contiene muchos elementos.",
            'kind: "incomplete"',
            "state.listingComplete = false",
        ):
            self.assertIn(marker, self.source)

    def test_snapshot_authentication_failure_explains_secret_action_without_init_guidance(self):
        for marker in (
            "SNAPSHOT_AUTHENTICATION_ERROR_MESSAGE",
            "No se pudo desbloquear el repositorio Restic.",
            "contraseña o clave de Restic",
            "diferente de la usada para crear el repositorio",
            "secreto de Restic configurado para el target en Settings",
            "No ejecutes restic init a menos que confirmes que el repositorio no existe.",
            'code === "authentication"',
            "wrong\\s+\\S+\\s+or\\s+no\\s+key\\s+found",
            "failure.listing_error_code = normalizedSyncResult.listing_error_code;",
            "classifySnapshotListingFailure(error)",
            "retry: true",
        ):
            self.assertIn(marker, self.source)

        classifier = re.search(r"function classifySnapshotListingFailure\(result\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(classifier)
        classifier_source = classifier.group(0)
        self.assertIn('code === "authentication"', classifier_source)
        self.assertIn("SNAPSHOT_AUTHENTICATION_ERROR_MESSAGE", classifier_source)

        refresh = re.search(r"async function refreshSnapshotCatalog\(\) \{.*?\n    \}\n\n    function showSnapshotOperationError", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        self.assertIn("normalizeSnapshotContract(syncResult)", refresh_source)
        self.assertIn("listingFailure ?", refresh_source)
        self.assertIn("renderSnapshotState(state.catalogStatus, state.catalogError, { retry: true })", refresh_source)

    def test_cold_browse_results_do_not_start_prefetch_jobs(self):
        browse = re.search(r"async function browseSnapshot\(.*?\n    \}\n\n    async function searchSnapshot", self.source, re.S)
        self.assertIsNotNone(browse)
        browse_source = browse.group(0)
        self.assertRegex(
            browse_source,
            r'if \(isWarmSnapshotResult\(result\)\) \{\s*prefetchSnapshotDirectories\(',
        )
        self.assertIn("result.cache_hit === true", self.source)
        self.assertIn('["redis", "local", "memory"]', self.source)

    def test_small_snapshot_search_uses_local_results_before_remote_dispatch(self):
        search = re.search(r"async function searchSnapshot\(.*?\n    \}\n\n    function classifySnapshotDownloadFailure", self.source, re.S)
        self.assertIsNotNone(search)
        search_source = search.group(0)
        self.assertIn("state.entries.length < SNAPSHOT_MAX_ENTRIES", search_source)
        self.assertIn("const useLocalSearch", search_source)
        self.assertIn("finishSnapshotRequest(request);\n        return;", search_source)
        self.assertIn("normalized.path.toLowerCase().includes(needle)", search_source)
        self.assertIn("normalized.name.toLowerCase().includes(needle)", search_source)

    def test_snapshot_explorer_escapes_server_content_and_never_previews_files(self):
        for marker in (
            "escapeHtml(state.target.name)",
            "escapeHtml(id)",
            "escapeHtml(entry.path)",
            "escapeHtml(message)",
            "escapeHtml(displayName)",
        ):
            self.assertIn(marker, self.source)
        self.assertIn("Los contenidos de archivos nunca se renderizan", self.source)
        self.assertNotIn("innerHTML = result.b64_content", self.source)

    def test_jobs_polling_renders_current_state_and_preserves_log_refresh_path(self):
        render_jobs = re.search(
            r"function renderJobs\(content\) \{.*?\n    let _jobsRenderTableBody",
            self.source,
            re.S,
        )
        self.assertIsNotNone(render_jobs)
        render_source = render_jobs.group(0)
        body_start = render_source.index("const renderTableBody = () => {")
        body_end = render_source.index("const renderTable = () => {", body_start)
        body_source = render_source[body_start:body_end]
        self.assertIn("const pagedJobs = state.jobs || [];", body_source)
        self.assertNotIn("const pagedJobs = state.jobs || [];", render_source.split("const renderTableBody", 1)[0])
        for removed in ("jobs.filter(j => {", "jobs.sort((a, b) => {", "startedAt < startedBound", "jobs.slice(startIdx"):
            self.assertNotIn(removed, body_source)

        polling = re.search(
            r"function startJobsPolling\(\) \{.*?\n    function stopJobsPolling",
            self.source,
            re.S,
        )
        self.assertIsNotNone(polling)
        polling_source = polling.group(0)
        self.assertNotIn("apiGet(", polling_source)
        for marker in (
            "if (jobsListRequest) {",
            "scheduleNext();",
            "const { changed } = await requestJobsPage();",
            "elapsed > JOBS_POLL_SLOW_MS ? Math.min(jobsPollDelayMs * 2, JOBS_POLL_MAX_MS) : JOBS_POLL_BASE_MS",
            "jobsPollDelayMs = Math.min(jobsPollDelayMs * 2, JOBS_POLL_MAX_MS);",
            "setTimeout(tick, jobsPollDelayMs)",
            "if (changed) renderJobsTableIfVisible();",
        ):
            self.assertIn(marker, polling_source)
        apply_page = re.search(r"function applyJobsPage\(jobsResp, query\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(apply_page)
        self.assertIn("if (query !== jobsListQuery()) return false;", apply_page.group(0))
        self.assertIn("state.jobs = mergedJobs;", apply_page.group(0))
        self.assertIn("jobsListTotal = total;", apply_page.group(0))
        self.assertIn("jobsListLoaded = true;", apply_page.group(0))
        renderer = re.search(r"function renderJobsTableIfVisible\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(renderer)
        self.assertIn("if (_jobsRenderTableBody)", renderer.group(0))
        self.assertIn("renderJobs(content);", renderer.group(0))
        self.assertIn("renderJobLogs(job);", self.source)
        self.assertIn("await refreshAll();", self.source)
        self.assertIn('["succeeded", "failed", "canceled", "cancelled"]', self.source)
        self.assertNotIn('apiGet("/api/v1/jobs")', self.source)
        self.assertNotIn("apiGet('/api/v1/jobs')", self.source)
        self.assertNotIn("apiGet(`/api/v1/jobs`)", self.source)

    def test_dashboard_content_keeps_recent_tables_from_flex_shrinking(self):
        dashboard = re.search(r"function renderDashboard\(content\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(dashboard)
        markup = dashboard.group(0)
        wrapper = markup.find('<div class="dashboard-content">')
        workers = markup.find("Workers recientes")
        jobs = markup.find("Jobs recientes")
        self.assertGreaterEqual(wrapper, 0)
        self.assertLess(wrapper, workers)
        self.assertLess(workers, jobs)
        self.assertLess(jobs, markup.rfind("</div>"))
        self.assertRegex(self.app_css, r"\.dashboard-content\s*\{[^}]*flex:\s*0\s+0\s+auto;")

    def test_jobs_list_refresh_uses_metadata_and_preserves_loaded_detail_projection(self):
        refresh = re.search(r"async function refreshAll\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        self.assertNotIn("include_logs=true", self.source)
        self.assertIn("const routeView = currentRouteView();", refresh_source)
        self.assertIn("shouldFetchJobsDuringRefresh(routeView) ? fetchDashboardJobs() : Promise.resolve(null)", refresh_source)
        self.assertIn("applyDashboardJobs(dashboardJobs);", refresh_source)
        dashboard_fetch = re.search(r"async function fetchDashboardJobs\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(dashboard_fetch)
        self.assertIn('apiGet("/api/v1/jobs?limit=5")', dashboard_fetch.group(0))
        self.assertIn("apiGet(`/api/v1/jobs/stats?tz_offset=${browserTimezoneOffsetMinutes()}`)", dashboard_fetch.group(0))
        self.assertIn("state.jobs = mergeJobList((dashboard.jobs && dashboard.jobs.items) || []);", self.source)
        self.assertIn("if (dashboard.stats) state.jobStats = dashboard.stats;", self.source)
        dashboard = re.search(r"function renderDashboard\(content\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(dashboard)
        self.assertIn("const stats = state.jobStats || EMPTY_JOB_STATS;", dashboard.group(0))
        self.assertIn("buildActivityDays(stats.by_day)", dashboard.group(0))
        self.assertNotIn("state.jobs.filter(", dashboard.group(0))
        self.assertNotIn("state.jobs.length", dashboard.group(0).split("Jobs recientes", 1)[0])
        self.assertIn("function mergeJobList(items)", self.source)
        self.assertIn("currentLogJobSnapshot", self.source)
        self.assertIn('for (const key of ["result_summary", "storage_context", "progress", "log_lines"])', self.source)
        self.assertIn("mergeJobProjection(job);", self.source)
        self.assertIn("const mergedJobs = mergeJobList(newJobs);", self.source)
        self.assertIn("state.jobs = mergedJobs;", self.source)

    def test_job_list_fetch_is_route_scoped_and_target_save_does_not_wait_for_it(self):
        target_save = re.search(
            r"function openEditTargetModal\(target\) \{.*?\n    function closeTargetModal",
            self.source,
            re.S,
        )
        self.assertIsNotNone(target_save)
        self.assertIn("await apiPatch(`/api/v1/targets/${target.id}`, targetUpdate);", target_save.group(0))
        self.assertIn("await refreshAll();", target_save.group(0))

        render_jobs = re.search(r"function renderJobs\(content\) \{.*?\n    let _jobsRenderTableBody", self.source, re.S)
        self.assertIsNotNone(render_jobs)
        self.assertIn("startJobsPolling();", render_jobs.group(0))

        functions = []
        for pattern in (
            r"function loadPageData\(view, force = false\) \{.*?\n    \}",
            r"function currentRouteView\(\) \{.*?\n    \}",
            r"function route\(\) \{.*?\n    \}",
            r"function shouldFetchJobsDuringRefresh\(view\) \{.*?\n    \}",
            r"function browserTimezoneOffsetMinutes\(now = new Date\(\)\) \{.*?\n    \}",
            r"async function fetchDashboardJobs\(\) \{.*?\n    \}",
            r"function applyDashboardJobs\(dashboard\) \{.*?\n    \}",
            r"async function refreshDashboardJobs\(\) \{.*?\n    \}",
            r"    const TARGET_LOG_JOB_COMMANDS = .*?;\n    const TARGET_LOG_JOB_LOOKUP_LIMIT = .*?;",
            r"function getLastJobForTarget\(targetId, jobs\) \{.*?\n    \}",
            r"async function openLatestTargetLogs\(targetId\) \{.*?\n    \}",
            r"async function refreshAll\(\) \{.*?\n    \}",
            r"async function withBtnLoading\(btn, asyncFn, loadingText\) \{.*?\n    \}",
            r"    let jobsTableState = .*?\n    async function reloadJobs\(\) \{.*?\n    \}",
            r"    function clearJobsRefreshNotice\(\) \{.*?\n    function stopJobsPolling\(\) \{.*?\n    \}",
        ):
            match = re.search(pattern, self.source, re.S)
            self.assertIsNotNone(match, pattern)
            functions.append(match.group(0))

        script = "\n\n".join(functions)
        for original, scaled in (
            ("const JOBS_POLL_BASE_MS = 3000;", "const JOBS_POLL_BASE_MS = 20;"),
            ("const JOBS_POLL_MAX_MS = 30000;", "const JOBS_POLL_MAX_MS = 80;"),
            ("const JOBS_POLL_SLOW_MS = 2000;", "const JOBS_POLL_SLOW_MS = 10;"),
        ):
            self.assertIn(original, script)
            script = script.replace(original, scaled)
        script += r'''
const state = {
  currentUser: "test",
  currentRole: "admin",
  currentView: "dashboard",
  jobs: [],
  jobStats: null,
  workers: [],
  targets: [],
  storageProfiles: [],
  retentionPolicies: [],
  secrets: [],
  localUsers: [],
  settings: {},
  pageDataLoaded: {},
  pageDataPending: {},
};
const NAV_ITEMS = ["dashboard", "targets", "jobs"].map(id => ({ id, roles: ["admin", "operator", "viewer"] }));
const window = { location: { hash: "#/targets" } };
const document = { getElementById: () => null };
const escapeHtml = value => String(value);
const LOG_POLL_MAX_FAILURES = 5;
const LOG_POLL_FAILURE_WINDOW_MS = 60000;
let _jobsRenderTableBody = null;
let holdJobsRequest = false;
let failJobsRequest = false;
let slowJobsRequestMs = 0;
let releaseJobs = null;
let nextJobs = [];
let nextTotal = null;
const calls = [];
const openedLogs = [];
const alerts = [];
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function waitFor(predicate, label, timeout = 1000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await sleep(5);
  }
  throw new Error(`timed out waiting for ${label}`);
}
const jobCalls = () => calls.filter(path => path.startsWith("/api/v1/jobs?"));
const statsCalls = () => calls.filter(path => path.startsWith("/api/v1/jobs/stats?"));
const apiGet = async path => {
  if (path.startsWith("/api/v1/jobs/stats")) {
    calls.push(path);
    return { total: 42, by_status: { succeeded: 40, failed: 2 }, by_day: [] };
  }
  if (path.startsWith("/api/v1/jobs")) {
    calls.push(path);
    if (!path.includes("?") || !/[?&]limit=\d+/.test(path)) throw new Error(`unbounded jobs request: ${path}`);
    if (holdJobsRequest) return new Promise(resolve => { releaseJobs = resolve; });
    if (slowJobsRequestMs) await sleep(slowJobsRequestMs);
    if (failJobsRequest) throw new Error("jobs unavailable");
    return { items: nextJobs, total: nextTotal === null ? nextJobs.length : nextTotal };
  }
  if (path === "/api/v1/auth/me") return { username: "test", role: "admin" };
  if (path === "/api/v1/targets") return { items: [{ id: "target-1" }] };
  if (["/api/v1/workers", "/api/v1/storage-profiles", "/api/v1/retention-policies", "/api/v1/secrets", "/api/v1/admin/users"].includes(path)) return { items: [] };
  if (path === "/api/v1/config/public") return {};
  if (path === "/api/v1/settings/rclone-remote") return {};
  if (path === "/api/v1/version") return { version: "test" };
  return {};
};
const mergeJobList = items => items;
const mergeJobProjection = job => job;
const renderVersionBanner = () => {};
const renderJobs = () => {};
const normalizeJobStatus = status => status;
const alert = message => alerts.push(message);
const openLogPanel = (jobId, targetId) => openedLogs.push([jobId, targetId]);
function setView(view) {
  state.currentView = view;
  if (view !== "jobs") stopJobsPolling();
  if (view === "jobs") startJobsPolling();
}

async function main() {
  holdJobsRequest = true;
  const saveButton = { disabled: false, innerHTML: "Save target" };
  let deadline;
  const targetSaveResult = await Promise.race([
    withBtnLoading(saveButton, () => refreshAll(), "Saving").then(() => "resolved"),
    new Promise(resolve => { deadline = setTimeout(() => resolve("blocked"), 250); }),
  ]);
  clearTimeout(deadline);
  if (targetSaveResult !== "resolved") throw new Error("target save waited for the jobs request");
  if (saveButton.disabled || saveButton.innerHTML !== "Save target") throw new Error("target save button did not leave its loading state");
  if (calls.some(path => path.startsWith("/api/v1/jobs"))) throw new Error("Targets refresh requested job history");

  holdJobsRequest = false;
  calls.length = 0;
  nextJobs = [
    { id: "older-target-job", target_id: "target-1", command: "backup.run", updated_at: "2026-01-01T00:00:00Z" },
    { id: "newer-other-target", target_id: "other", command: "backup.run", updated_at: "2026-04-01T00:00:00Z" },
    { id: "newer-ineligible-job", target_id: "target-1", command: "snapshots.list", updated_at: "2026-05-01T00:00:00Z" },
    { id: "latest-target-job", target_id: "target-1", command: "restore.dry_run", updated_at: "2026-03-01T00:00:00Z" },
  ];
  await openLatestTargetLogs("target-1");
  if (jobCalls().length !== 1 || jobCalls()[0] !== "/api/v1/jobs?target_id=target-1&limit=25") throw new Error(`deliberate Logs action did not perform exactly one bounded target lookup: ${JSON.stringify(calls)}`);
  if (openedLogs[0]?.[0] !== "latest-target-job" || openedLogs[0]?.[1] !== "target-1") throw new Error("deliberate Logs action did not open the latest eligible target job");

  calls.length = 0;
  nextJobs = [];
  await openLatestTargetLogs("target-1");
  if (alerts.length !== 1 || !alerts[0].includes("No hay ejecuciones elegibles")) throw new Error("missing target jobs were not surfaced to the user");

  calls.length = 0;
  failJobsRequest = true;
  await openLatestTargetLogs("target-1");
  failJobsRequest = false;
  if (alerts.length !== 2 || !alerts[1].includes("jobs unavailable")) throw new Error("jobs lookup failure was not surfaced to the user");

  calls.length = 0;
  state.currentView = "dashboard";
  window.location.hash = "#/dashboard";
  nextJobs = [{ id: "dashboard-initial" }];
  await refreshAll();
  if (jobCalls().length !== 1 || jobCalls()[0] !== "/api/v1/jobs?limit=5") throw new Error(`initial Dashboard did not fetch exactly one recent-jobs page: ${JSON.stringify(calls)}`);
  if (statsCalls().length !== 1 || statsCalls()[0] !== `/api/v1/jobs/stats?tz_offset=${-new Date().getTimezoneOffset()}`) throw new Error(`initial Dashboard did not fetch stats once with the browser offset: ${JSON.stringify(calls)}`);
  if (state.jobs[0]?.id !== "dashboard-initial") throw new Error("initial Dashboard did not receive current jobs");
  if (state.jobStats?.total !== 42) throw new Error("initial Dashboard did not store server stats");

  window.location.hash = "#/targets";
  route();
  calls.length = 0;
  nextJobs = [{ id: "dashboard-after-targets" }];
  window.location.hash = "#/dashboard";
  route();
  await sleep(0);
  if (jobCalls().length !== 1 || statsCalls().length !== 1) throw new Error("switching to Dashboard did not fetch exactly one recent page and one stats call");
  if (state.jobs[0]?.id !== "dashboard-after-targets") throw new Error("switching to Dashboard did not refresh job data");

  calls.length = 0;
  state.jobs = [];
  nextJobs = [{ id: "jobs-screen-initial" }];
  nextTotal = 9105;
  window.location.hash = "#/jobs";
  await refreshAll();
  await sleep(0);
  if (jobCalls().length !== 1 || jobCalls()[0] !== "/api/v1/jobs?limit=25&offset=0&sort=submitted_at&order=desc") throw new Error(`Jobs screen did not issue exactly one bounded page request: ${JSON.stringify(calls)}`);
  if (state.jobs[0]?.id !== "jobs-screen-initial") throw new Error("Jobs polling did not populate the initial page");
  if (!jobsListLoaded || jobsListTotal !== 9105) throw new Error("Jobs page did not record the server total");

  await waitFor(() => jobCalls().length >= 2, "a second poll tick");
  if (jobsPollDelayMs !== JOBS_POLL_BASE_MS) throw new Error("fast poll did not keep the base interval");
  holdJobsRequest = true;
  await waitFor(() => releaseJobs !== null, "the held poll request");
  const heldCalls = jobCalls().length;
  await sleep(JOBS_POLL_BASE_MS * 4);
  if (jobCalls().length !== heldCalls) throw new Error(`poll ticks overlapped an in-flight request: ${jobCalls().length - heldCalls} extra calls`);
  if (requestJobsPage() !== jobsListRequest) throw new Error("manual page request did not reuse the in-flight request");
  if (jobCalls().length !== heldCalls) throw new Error("manual page request started a second request while one was in flight");
  holdJobsRequest = false;
  nextJobs = [];
  nextTotal = 0;
  releaseJobs({ items: [], total: 0 });
  releaseJobs = null;
  await waitFor(() => jobsListTotal === 0, "the released response to apply");
  if (state.jobs.length !== 0) throw new Error("released response did not replace the page");

  slowJobsRequestMs = JOBS_POLL_SLOW_MS * 3;
  await waitFor(() => jobsPollDelayMs === JOBS_POLL_BASE_MS * 2, "slow response backoff");
  await waitFor(() => jobsPollDelayMs === JOBS_POLL_BASE_MS * 4, "second slow response backoff");
  slowJobsRequestMs = 0;
  await waitFor(() => jobsPollDelayMs === JOBS_POLL_BASE_MS, "fast response reset");
  failJobsRequest = true;
  await waitFor(() => jobsPollDelayMs === JOBS_POLL_BASE_MS * 2 && jobsPollFailures === 1, "failure backoff");
  failJobsRequest = false;
  await waitFor(() => jobsPollDelayMs === JOBS_POLL_BASE_MS, "recovery after failure");
  if (jobsPollDelayMs > JOBS_POLL_MAX_MS) throw new Error("backoff exceeded its ceiling");
  stopJobsPolling();
  await sleep(JOBS_POLL_BASE_MS * 3);
  const afterStop = jobCalls().length;
  await sleep(JOBS_POLL_BASE_MS * 3);
  if (jobCalls().length !== afterStop) throw new Error("polling continued after stopJobsPolling");
  process.stdout.write("route-scoped jobs refresh passed");
}

main().catch(error => {
  stopJobsPolling();
  console.error(error);
  process.exitCode = 1;
});
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "route-scoped jobs refresh passed")

    def test_job_logs_render_progress_storage_context_and_normalize_in_progress(self):
        for marker in (
            "function normalizeJobStatus(status)",
            'if (value === "running") return "in_progress"',
            "function renderJobProgress(job)",
            "role=\"progressbar\"",
            "job-progress-fill indeterminate",
            "function renderJobStorageContext(job)",
            "function jobPhaseLabel(phase)",
            'initializing: "Inicializando"',
            'finalizing: "Finalizando"',
            'phaseLabel ? `Estado: ${phaseLabel}`',
            "Storage no configurado",
            "Repositorio no configurado",
            "j.updated_at",
            "j.progress",
            "renderJobProgress(job)",
            "renderJobStorageContext(job)",
            "function pollJobLogs(jobId)",
            "apiGet(`/api/v1/jobs/${encodeURIComponent(jobId)}/logs`)",
            "function pollAccordionJobLogs(jobId)",
            "apiGet(`/api/v1/jobs/${encodeURIComponent(jobId)}/logs`)",
            "setInterval(fetchJob, 2000)",
            "setInterval(fetchJobLogs, 3000)",
        ):
            self.assertIn(marker, self.source)
        self.assertIn(".job-progress-fill.indeterminate", self.css)
        self.assertIn("prefers-reduced-motion", self.css)

    def test_job_detail_keeps_summary_in_header_outside_scrolling_log_list(self):
        for marker in (
            'class="job-detail-summary" data-job-detail-summary',
            "function renderJobDetailSummary(job, root)",
            "renderJobDetailSummary(job, root);",
            'renderJobDetailSummary(job, container.closest(".log-panel"));',
        ):
            self.assertIn(marker, self.source)
        for marker in (
            ".job-detail-summary",
            ".job-detail-summary > .job-progress",
            ".job-detail-summary > .job-storage-context",
            "align-items: stretch",
            "width: 100%",
            ".log-panel .log-container",
        ):
            self.assertIn(marker, self.css)

        target_renderer = re.search(
            r"function renderTargetJobLogsInto\(job, root\) \{.*?\n    \}",
            self.source,
            re.S,
        )
        accordion_renderer = re.search(
            r"function renderJobLogsInto\(job, container\) \{.*?\n    \}",
            self.source,
            re.S,
        )
        self.assertIsNotNone(target_renderer)
        self.assertIsNotNone(accordion_renderer)
        for renderer in (target_renderer.group(0), accordion_renderer.group(0)):
            self.assertIn("renderJobDetailSummary", renderer)
            self.assertNotIn("renderJobProgress(job)", renderer)
            self.assertNotIn("renderJobStorageContext(job)", renderer)

        for markup in (
            re.search(r"function targetLogPanelMarkup\(jobId, targetId\) \{.*?\n    \}", self.source, re.S),
            re.search(r"contentDiv\.innerHTML = `.*?data-job-detail-summary.*?`;", self.source, re.S),
        ):
            self.assertIsNotNone(markup)
            self.assertIn("data-job-detail-summary", markup.group(0))

    def test_job_lease_diagnostics_render_observed_and_unknown_facts(self):
        for marker in (
            "function renderJobLeaseDiagnostics(job)",
            "worker_lease_expired",
            "diagnostic_code",
            "diagnostic_category",
            "last_renewal",
            "Causa raíz no confirmada",
            "renderJobLeaseDiagnostics(job)",
            "job-lease-diagnostics",
        ):
            self.assertIn(marker, self.source)
        renderer = re.search(r"function renderJobLeaseDiagnostics\(job\) \{.*?\n      \}", self.source, re.S)
        self.assertIsNotNone(renderer)
        for marker in (
            "fmtDate(observed.lease_issued_at)",
            "fmtDate(observed.lease_expires_at)",
            "fmtDate(observed.last_repository_update_at)",
            "fmtDate(observed.last_progress_at)",
            "fmtDate(observed.worker_last_seen_at)",
        ):
            self.assertIn(marker, renderer.group(0))

    def test_job_lease_error_copy_explains_timeout_without_claiming_a_root_cause(self):
        renderer = re.search(r"function renderJobLeaseDiagnostics\(job\) \{.*?\n     \}", self.source, re.S)
        self.assertIsNotNone(renderer)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = r'''
const escapeHtml = value => String(value);
const fmtDate = value => value || "No disponible";
''' + renderer.group(0) + r'''
const html = renderJobLeaseDiagnostics({
  id: "job-1", target_id: "target-1", worker_id: "worker-1", command: "backup.run",
  result_summary: { diagnostic_code: "worker_lease_expired", diagnostic_category: "worker_interrupted" },
  diagnostics: {
    code: "worker_lease_expired", category: "worker_interrupted",
    message: "Control Plane observed lease expiry; worker/runtime cause unknown.",
    unknown: ["terminal_result", "root_cause"],
    observed: {
      job_id: "job-1", target_id: "target-1", worker_id: "worker-1", command: "backup.run",
      lease_issued_at: "2026-10-19T15:01:00Z", lease_expires_at: "2026-10-19T15:06:00Z",
      last_repository_update_at: "2026-10-19T15:01:00Z", last_progress_at: null,
      last_progress_sequence: 0, worker_last_seen_at: "2026-10-19T20:06:00Z",
      worker_status: "online", last_renewal: null,
    },
  },
});
const expected = [
  "El worker no confirmó el fin del job",
  "El plazo de reserva (lease) venció antes de que el Control Plane recibiera el resultado final",
  "El job sigue pendiente de confirmación: una pérdida de comunicación no demuestra que el backup haya fallado",
  "No se pudo determinar si el worker se detuvo, quedó esperando o perdió comunicación con el Control Plane",
  "<summary>Detalles técnicos</summary>",
  "worker_lease_expired",
  "Sin evidencia de renovación recibida por el Control Plane",
];
const missing = expected.filter(text => !html.includes(text));
if (missing.length) throw new Error(`missing user-facing lease explanation: ${missing.join("; ")}`);
if (html.indexOf("<details") < 0 || html.indexOf("worker_lease_expired") < html.indexOf("<details")) throw new Error("technical details are not behind the disclosure");
process.stdout.write("lease error explanation passed");
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "lease error explanation passed")

    def test_retention_guide_explains_union_prune_and_updates_draft_or_selected_policy_summary(self):
        retention = re.search(r"function renderRetention\(content\) \{.*?\n    \}\n\n    function renderSecrets", self.source, re.S)
        self.assertIsNotNone(retention)
        retention_source = retention.group(0)
        for marker in (
            "retention-layout",
            "retention-guide",
            "Las reglas se combinan como una <strong>unión</strong>",
            "no crean backups ni prometen snapshots",
            "<strong>prune</strong> libera datos sin referencias",
            "data-retention-policy-id",
            "is-selected",
            'addEventListener("input", showDraftSummary)',
            "selectRetentionPolicy",
        ):
            self.assertIn(marker, retention_source)
        for marker in (
            "function retentionPolicySummary(policy)",
            "Conservará los últimos",
            "cubrirá hasta",
            "snapshots disponibles",
            "retentionPolicyFromForm",
            "renderRetentionGuideSummary",
        ):
            self.assertIn(marker, self.source)
        for marker in (
            ".retention-layout",
            "grid-template-columns: minmax(0, 1fr) minmax(260px, 340px)",
            ".retention-guide { position: static; }",
            "@media (max-width: 960px)",
            ".retention-policy-row.is-selected",
        ):
            self.assertIn(marker, self.css)

    def test_log_polling_surfaces_bounded_fetch_failures_in_both_panels(self):
        for marker in (
            "LOG_POLL_MAX_FAILURES = 5",
            "LOG_POLL_FAILURE_WINDOW_MS = 60000",
            "renderLogPollingError",
            "Reintentar",
            "La consulta se detuvo tras varios fallos.",
            "pollAccordionJobLogs",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("} catch (error) { }", self.source)

    def test_jobs_polling_surfaces_bounded_refresh_failures_without_error_details(self):
        jobs = re.search(
            r"let jobsTableState = .*?\n    function stopJobsPolling",
            self.source,
            re.S,
        )
        self.assertIsNotNone(jobs)
        jobs_source = jobs.group(0)
        polling = re.search(
            r"function startJobsPolling\(\) \{.*?\n    function stopJobsPolling",
            self.source,
            re.S,
        )
        self.assertIsNotNone(polling)
        polling_source = polling.group(0)
        for marker in (
            'id="jobsRefreshNotice"',
            'role="status"',
            'aria-live="polite"',
            "renderJobsRefreshNotice",
            "clearJobsRefreshNotice",
            "retryJobsPolling",
            "data-jobs-refresh-retry",
        ):
            self.assertIn(marker, jobs_source)
        for marker in (
            "LOG_POLL_MAX_FAILURES",
            "LOG_POLL_FAILURE_WINDOW_MS",
            "No se pudo actualizar Jobs. Reintentando",
            "La actualizacion de Jobs se detuvo tras varios fallos.",
            "jobsPollFailures += 1;",
            "jobsPolling = false;",
        ):
            self.assertIn(marker, polling_source)
        self.assertNotIn("error.message", polling_source)
        self.assertNotIn("ignore polling errors", polling_source)

    def test_hidden_jobs_refresh_notice_overrides_snapshot_status_display(self):
        self.assertIn('.snapshot-status[hidden] { display: none !important; }', self.source)
        self.assertIn('id="jobsRefreshNotice"', self.source)
        self.assertIn("notice.hidden = false;", self.source)

    def test_cron_preview_uses_backend_effective_schedule_and_explicit_timezone(self):
        for marker in (
            "/api/v1/scheduler/preview",
            "effective_cron_expression",
            "cron_source",
            "scheduler_timezone",
            "next_scheduled_at",
            "target_id",
            "target_context",
            "timeZone,",
            "state.schedulerTimezone",
        ):
            self.assertIn(marker, self.source)
        target_schedule = re.search(r"function renderTargetCronLabel\(target\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(target_schedule)
        self.assertIn("targetCronExpression(target)", target_schedule.group(0))
        self.assertIn("target.next_scheduled_at", target_schedule.group(0))
        preview = re.search(r"async function updateCronPreview\(inputId, previewId, targetId, targetContext\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(preview)
        self.assertIn("api/v1/scheduler/preview", preview.group(0))
        self.assertNotIn("cronNextRun(expr)", preview.group(0))

    def test_timestamps_and_activity_buckets_use_browser_timezone(self):
        helpers = []
        for name in ("fmtDate", "fmtSchedulerDate", "browserTimezoneOffsetMinutes", "buildActivityDays"):
            helper = re.search(rf"(    function {name}\(.*?\n    \}})", self.source, re.S)
            self.assertIsNotNone(helper, name)
            helpers.append(helper.group(1))
        self.assertNotIn("function zonedDateKey", self.source)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = "\n".join(helpers) + r'''
process.env.TZ = "Pacific/Auckland";
const state = { schedulerTimezone: "America/Bogota" };
const instant = "2024-01-01T00:30:00Z";
const formatted = fmtDate(instant);
const expectedBrowser = new Date(instant).toLocaleString("es-CO", {
  dateStyle: "short", timeStyle: "short",
});
if (formatted !== expectedBrowser) throw new Error(`unexpected browser-local date: ${formatted}`);

const equivalentOffset = "2023-12-31T19:30:00-05:00";
if (new Date(equivalentOffset).getTime() !== new Date(instant).getTime()) {
  throw new Error("UTC and explicit-offset timestamps did not represent the same instant");
}
if (fmtDate(equivalentOffset) !== formatted) throw new Error("explicit-offset timestamp was parsed incorrectly");

const expectedScheduler = new Intl.DateTimeFormat("es-CO", {
  timeZone: state.schedulerTimezone, dateStyle: "short", timeStyle: "short",
}).format(new Date(instant));
if (fmtSchedulerDate(instant, state.schedulerTimezone) !== expectedScheduler) {
  throw new Error("scheduler timestamp did not retain its configured timezone");
}

if (browserTimezoneOffsetMinutes(new Date(instant)) !== 780) {
  throw new Error(`activity buckets did not request the browser offset: ${browserTimezoneOffsetMinutes(new Date(instant))}`);
}
const days = buildActivityDays([
  { date: "2023-12-31", total: 0, success: 0, failed: 0 },
  { date: "2024-01-01", total: 2, success: 1, failed: 1 },
]);
const lastDay = days[days.length - 1];
if (days.length !== 2 || lastDay.total !== 2 || lastDay.failed !== 1 || lastDay.success !== 1) {
  throw new Error(`activity chart did not keep the server buckets: ${JSON.stringify(lastDay)}`);
}
if (lastDay.label !== new Date("2024-01-01T00:00:00Z").toLocaleDateString("es", { timeZone: "UTC", weekday: "short" })) {
  throw new Error(`activity chart labelled the server day incorrectly: ${lastDay.label}`);
}

state.schedulerTimezone = "Invalid/Timezone";
if (fmtDate(instant) !== expectedBrowser) throw new Error("event timestamp followed the scheduler timezone");
state.schedulerTimezone = null;
if (fmtDate(instant) !== expectedBrowser) throw new Error("event timestamp fell back from the browser timezone");
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cron_preview_has_no_unreachable_browser_schedule_helpers(self):
        for helper in (
            "function parseCronExpression",
            "function cronMatchesParts",
            "function zonedCronParts",
            "function cronNextRun",
        ):
            self.assertNotIn(helper, self.source)
        self.assertIn("function cronDescribe", self.source)
        self.assertIn("/api/v1/scheduler/preview", self.source)

    def test_jobs_history_renders_and_filters_job_origin(self):
        for marker in (
            "jobTriggerInfo",
            "fmtJobTrigger",
            'manual: { cls:',
            'schedule: { cls:',
            'automatic: { cls:',
            'interactive: { cls:',
            "Desconocido",
            'data-col="origin"',
            'sortAttrs("trigger")',
            "j.trigger",
            'Origen${sortIcon("trigger")}',
            'colspan="8"',
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn('sortAttrs("origin")', self.source)
        self.assertNotIn('sortAttrs("target")', self.source)
        trigger_info = re.search(r"function jobTriggerInfo\(trigger\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(trigger_info)
        trigger_renderer = re.search(r"function fmtJobTrigger\(trigger\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(trigger_renderer)
        self.assertIn("escapeHtml", trigger_renderer.group(0))

    def test_jobs_show_target_name_above_muted_worker_name(self):
        self.assertIn('class="job-identity"', self.source)
        self.assertIn('class="job-identity-target"', self.source)
        self.assertIn('class="job-identity-worker"', self.source)
        self.assertIn('j.target_name', self.source)
        self.assertIn('j.worker_name', self.source)
        self.assertIn('Target / Worker', self.source)
        self.assertIn('.job-identity-target { font-size: 13px; font-weight: 600;', self.source)
        self.assertIn('.job-identity-worker { color: var(--muted-foreground); font-size: 11px;', self.source)

    def test_jobs_search_field_queries_target_or_worker_name(self):
        self.assertIn('["target_worker", filters.target]', self.source)
        self.assertIn('<input type="text" class="dt-filter" data-col="target"', self.source)
        self.assertIn('aria-label="Buscar target o worker"', self.source)

    def test_jobs_filter_controls_use_typed_values_and_date_bounds(self):
        render_jobs = re.search(
            r"function renderJobs\(content\) \{.*?\n    let _jobsRenderTableBody",
            self.source,
            re.S,
        )
        self.assertIsNotNone(render_jobs)
        render_source = render_jobs.group(0)
        for marker in (
            '<input type="text" class="dt-filter" data-col="id"',
            '<input type="text" class="dt-filter" data-col="target"',
            '<select class="dt-filter" data-col="command"',
            '<select class="dt-filter" data-col="origin"',
            '<select class="dt-filter" data-col="status"',
            '<input type="datetime-local" class="dt-filter" data-col="started"',
            '<input type="datetime-local" class="dt-filter" data-col="finished"',
            'aria-label="Buscar target o worker"',
            'aria-label="Inicio desde"',
            'placeholder="Inicio desde"',
            'aria-label="Fin hasta"',
            'placeholder="Fin hasta"',
            '<option value="">Todos</option>',
            "const uniqueJobFilterValues =",
            "new Set(values",
            "...JOB_COMMAND_OPTIONS",
            "...JOB_TRIGGER_OPTIONS",
            "status: JOB_STATUS_OPTIONS.slice()",
            "jobTriggerLabel(value)",
            "jobStatusLabel(value)",
            "escapeHtml(value)",
            "!valueSet.has(jobsTableState.filters[column])",
            "select.value = valueSet.has(jobsTableState.filters[column])",
            "JOBS_FILTER_DEBOUNCE_MS",
            "clearTimeout(filterDebounceTimer)",
            'input.addEventListener("input"',
            'select.addEventListener("change"',
            "jobsTableState.page = 1;",
            "void reloadJobs();",
        ):
            self.assertIn(marker, render_source)
        for removed in (
            "function parseJobTimestamp(value)",
            "startedAt < startedBound",
            "finishedAt > finishedBound",
            '<select class="dt-filter" data-col="target"',
        ):
            self.assertNotIn(removed, render_source)

    def test_jobs_table_builds_server_query_and_shows_loading_before_empty_state(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        functions = []
        for pattern in (
            r"    let jobsTableState = .*?\n    async function reloadJobs\(\) \{.*?\n    \}",
            r"function renderJobs\(content\) \{.*?\n    let _jobsRenderTableBody = null;",
            r"    function clearJobsRefreshNotice\(\) \{.*?\n    function stopJobsPolling\(\) \{.*?\n    \}",
        ):
            match = re.search(pattern, self.source, re.S)
            self.assertIsNotNone(match, pattern)
            functions.append(match.group(0))
        script = "\n\n".join(functions) + r'''
const state = { currentView: "jobs", jobs: [], targets: [{ id: "t-2", name: "Zeta" }, { id: "t-1", name: "Alpha" }] };
const LOG_POLL_MAX_FAILURES = 5;
const LOG_POLL_FAILURE_WINDOW_MS = 60000;
const escapeHtml = value => String(value);
const normalizeJobStatus = status => status;
const jobTriggerLabel = value => value;
const jobStatusLabel = value => value;
const fmtJobCommand = value => String(value);
const fmtJobTrigger = value => String(value);
const fmtJobStatus = value => String(value);
const fmtDate = value => String(value || "");
const mergeJobList = items => items;
const toggleJobLogAccordion = () => {};
const withBtnLoading = async (btn, fn) => fn();
const apiPost = async () => ({});
const confirm = () => true;
const tbody = { attributes: {}, innerHTML: "", setAttribute(name, value) { this.attributes[name] = value; } };
const selects = {};
const makeSelect = () => ({ innerHTML: "", value: "" });
const content = {
  innerHTML: "",
  querySelector(selector) {
    if (selector === "#jobsTbody") return tbody;
    const match = /select\.dt-filter\[data-col="(\w+)"\]/.exec(selector);
    if (match) return selects[match[1]] || (selects[match[1]] = makeSelect());
    return null;
  },
  querySelectorAll() { return []; },
};
const document = { getElementById: id => (id === "content" ? content : null) };
const calls = [];
let pending = [];
const apiGet = path => {
  calls.push(path);
  return new Promise((resolve, reject) => pending.push({ resolve, reject }));
};
const flush = () => new Promise(resolve => setTimeout(resolve, 0));

async function main() {
  const query = jobsListQuery({ page: 3, pageSize: 50, sortKey: "started_at", sortDir: "asc", filters: { id: "ab", target: "t-1", command: "backup.run", status: "failed", origin: "schedule", started: "2026-01-10T07:00", finished: "" } });
  const params = new URLSearchParams(query);
  const expected = { limit: "50", offset: "100", sort: "started_at", order: "asc", id: "ab", target_worker: "t-1", command: "backup.run", status: "failed", trigger: "schedule", started_from: new Date("2026-01-10T07:00").toISOString() };
  for (const [key, value] of Object.entries(expected)) {
    if (params.get(key) !== value) throw new Error(`query param ${key} was ${params.get(key)}, expected ${value}`);
  }
  if (params.has("finished_to")) throw new Error("empty filters must not be sent");

  renderJobs(content);
  if (calls.length !== 1 || calls[0] !== "/api/v1/jobs?limit=25&offset=0&sort=submitted_at&order=desc") throw new Error(`initial render did not request the first page: ${JSON.stringify(calls)}`);
  if (tbody.attributes["aria-busy"] !== "true") throw new Error("tbody was not marked busy while loading");
  if (!tbody.innerHTML.includes("snapshot-skeleton-row")) throw new Error("loading state did not render skeleton rows");
  if (tbody.innerHTML.includes("Sin jobs que coincidan con los filtros")) throw new Error("empty message rendered before the response arrived");
  if (!content.innerHTML.includes('data-col="target" aria-label="Buscar target o worker"')) throw new Error("target/worker text search missing");
  if (!selects.status.innerHTML.includes('<option value="in_progress">')) throw new Error("status filter lost its fixed options");

  pending.shift().resolve({ items: [], total: 0 });
  await flush();
  if (tbody.attributes["aria-busy"] !== "false") throw new Error("tbody stayed busy after the response");
  if (!tbody.innerHTML.includes("Sin jobs que coincidan con los filtros")) throw new Error("empty response did not render the empty state");
  if (tbody.innerHTML.includes("snapshot-skeleton-row")) throw new Error("skeleton remained after the response");

  jobsTableState.filters.status = "failed";
  const reload = reloadJobs();
  await flush();
  if (tbody.attributes["aria-busy"] !== "true" || !tbody.innerHTML.includes("snapshot-skeleton-row")) throw new Error("reload did not show the loading state");
  if (calls.length !== 2 || !calls[1].includes("status=failed")) throw new Error(`reload did not send the new filter: ${JSON.stringify(calls)}`);
  pending.shift().resolve({ items: [{ id: "job-1234abcd", target_id: "t-1", target_name: "Alpha", worker_id: "w-1", worker_name: "worker-act-twc-11", command: "backup.run", trigger: "manual", status: "failed", started_at: "2026-01-10T07:00:00Z" }], total: 77 });
  await reload;
  await flush();
  if (!tbody.innerHTML.includes("job-row-job-1234abcd")) throw new Error("page rows were not rendered after reload");
  if (!tbody.innerHTML.includes('<span class="job-identity-target">Alpha</span><span class="job-identity-worker">worker-act-twc-11</span>')) throw new Error("target and worker names were not stacked");
  if (jobsListTotal !== 77) throw new Error("server total was not stored");

  jobsTableState.page = 1;
  const failing = reloadJobs();
  await flush();
  pending.shift().reject(new Error("boom"));
  await failing;
  await flush();
  if (tbody.attributes["aria-busy"] !== "false") throw new Error("failed reload left the table busy");
  if (!tbody.innerHTML.includes("No se pudo cargar la lista de jobs.")) throw new Error("failed reload did not explain the missing data");
  if (tbody.innerHTML.includes("Sin jobs que coincidan con los filtros")) throw new Error("failed reload rendered the empty state as if the server had answered");
  stopJobsPolling();
  process.stdout.write("jobs loading state passed");
}

main().catch(error => {
  stopJobsPolling();
  console.error(error);
  process.exitCode = 1;
});
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "jobs loading state passed")

    def test_open_job_logs_use_sse_with_bounded_polling_fallback_for_both_panels(self):
        for marker in (
            "function connectJobEvents(jobId",
            "new EventSource(`/api/v1/jobs/${encodeURIComponent(jobId)}/events`",
            "source.onmessage",
            "JSON.parse(event.data)",
            "source.onerror",
            "source.close()",
            "function mergeJobProjection(job)",
            "data-job-live-state",
            "En vivo",
            "Actualizando por polling",
            "function startTargetJobEvents(jobId)",
            "function startAccordionJobEvents(jobId)",
            "pollJobLogs(jobId)",
            "pollAccordionJobLogs(jobId)",
            "stopTargetJobEvents();",
            "stopAccordionJobEvents();",
            "if (isTerminalJobStatus(job.status))",
        ):
            self.assertIn(marker, self.source)

        target = re.search(r"function startTargetJobEvents\(jobId\) \{.*?\n    \}\n\n    function pollJobLogs", self.source, re.S)
        self.assertIsNotNone(target)
        self.assertIn("renderJobLogs(job)", target.group(0))
        self.assertIn("void refreshAll();", target.group(0))

        accordion = re.search(r"function startAccordionJobEvents\(jobId\) \{.*?\n    \}\n\n    function toggleJobLogAccordion", self.source, re.S)
        self.assertIsNotNone(accordion)
        self.assertIn("renderJobLogsInto(job, container)", accordion.group(0))
        self.assertIn("mergeJobProjection(job)", accordion.group(0))

    def test_job_live_state_uses_accessible_dot_without_visible_status_text(self):
        indicators = re.findall(
            r'<span class="job-live-indicator"[^>]*data-job-live-state[^>]*></span>',
            self.source,
        )
        self.assertEqual(len(indicators), 2)
        for indicator in indicators:
            self.assertIn('data-state="connecting"', indicator)
            self.assertIn('role="status"', indicator)
            self.assertIn('aria-live="polite"', indicator)
            self.assertIn('aria-label="Conectando…"', indicator)
            self.assertIn('title="Conectando…"', indicator)

        live_state = re.search(r"function setJobLiveState\(root, text, stateName\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(live_state)
        self.assertNotIn("textContent", live_state.group(0))
        for marker in ('status.dataset.state = stateName || "idle"', 'status.setAttribute("aria-label", label)', "status.title = label"):
            self.assertIn(marker, live_state.group(0))
        for marker in (
            ".job-live-indicator",
            '.job-live-indicator[data-state="connecting"]',
            '.job-live-indicator[data-state="fallback"]',
            '.job-live-indicator[data-state="live"]',
            '.job-live-indicator[data-state="terminal"]',
            '.job-live-indicator[data-state="error"]',
            "@keyframes job-live-pulse",
            "prefers-reduced-motion",
            "width: 8px",
            "height: 8px",
        ):
            self.assertIn(marker, self.css)
        self.assertNotIn('data-job-live-state role="status" aria-live="polite">Conectando…</span>', self.source)


    def test_live_file_browser_is_opt_in_read_only_safe_and_bounded(self):
        for marker in ("live_access_enabled", "Habilitar acceso live", "Ver archivos en vivo", "function openLiveBrowser(target)", "/live/entries", "/live/file", "URL.createObjectURL", "Solo lectura", 'role="tree"', 'role="treeitem"', 'source.addEventListener("resync_required"', "browser.reconnects > 4", "escapeHtml(entry.name)", "status.textContent = text", "await resp.json()", "error.code", "error.reason", "function liveBrowserErrorMessage", "function preflightLiveBrowserEvents", "Accept: \"text/event-stream\"", "source_unavailable", "helper_start_failed", "Acceso restringido: este volumen está protegido por permisos del sistema y no se puede leer en modo seguro."):
            self.assertIn(marker, self.source)
        self.assertNotIn("innerHTML = result.b64_content", self.source)
        self.assertNotIn("contenteditable", self.source.lower())
        for marker in (".live-browser-status[data-state=\"connected\"]", ".live-browser-status[data-state=\"resync\"]", ".live-browser-status[data-state=\"restricted\"]", ".live-browser-status[data-state=\"error\"]", ".live-browser-entry:focus-visible"):
            self.assertIn(marker, self.css)

    def test_live_file_browser_entries_are_sequenced_cancellable_and_path_safe(self):
        entries = re.search(r"async function loadLiveBrowserEntries\(.*?\n    \}\n\n    async function preflightLiveBrowserEvents", self.source, re.S)
        self.assertIsNotNone(entries)
        entries_source = entries.group(0)
        for marker in (
            "normalizeLiveBrowserPath(path)",
            "browser.requestedPath = normalizedPath;",
            "browser.entriesRequest?.controller.abort();",
            "new AbortController()",
            "sequence: ++browser.entriesSequence",
            "signal: request.controller.signal",
            "isCurrentLiveBrowserEntriesRequest(browser, request)",
            "browser.path = normalizedPath;",
            "browser.nextCursor = result.next_cursor || null;",
        ):
            self.assertIn(marker, entries_source)

        guard = re.search(r"function isCurrentLiveBrowserEntriesRequest\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(guard)
        for marker in ("browser === liveBrowserState", "browser.entriesRequest === request", "browser.entriesSequence === request.sequence", "browser.requestedPath === request.path"):
            self.assertIn(marker, guard.group(0))
        self.assertIn("if (!browser || browser.entriesRequest || !browser.nextCursor) return;", self.source)

    def test_live_file_browser_events_are_coalesced_and_resync_requires_manual_refresh(self):
        refresh = re.search(r"function scheduleLiveBrowserRefresh\(.*?\n    \}\n\n    function isCurrentLiveBrowserEntriesRequest", self.source, re.S)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        for marker in ("browser.refreshPending = true", "if (browser.refreshTimer) return", "if (browser.entriesRequest) return", "browser.requestedPath", "setTimeout", "}, 250);"):
            self.assertIn(marker, refresh_source)

        changed = re.search(r'source.addEventListener\("changed".*?\n      \}\);', self.source, re.S)
        self.assertIsNotNone(changed)
        self.assertIn("scheduleLiveBrowserRefresh(browser);", changed.group(0))
        resync = re.search(r'source.addEventListener\("resync_required".*?\n      \}\);', self.source, re.S)
        self.assertIsNotNone(resync)
        resync_source = resync.group(0)
        self.assertIn("clearLiveBrowserRefresh(browser);", resync_source)
        self.assertIn("browser.degraded = true;", resync_source)
        self.assertNotIn("loadLiveBrowserEntries", resync_source)
        self.assertIn('id="liveBrowserRefresh"', self.source)
        self.assertIn(">Actualizar</button>", self.source)
        self.assertIn("loadLiveBrowserEntries(browser.requestedPath)", self.source)
        self.assertIn("Cambios live no sincronizados; la lista se mantiene estable. Actualiza manualmente.", self.source)

    def test_live_file_browser_cleanup_aborts_entries_and_clears_refresh(self):
        close = re.search(r"function closeLiveBrowser\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(close)
        close_source = close.group(0)
        for marker in ("browser.closed = true", "clearLiveBrowserRefresh(browser);", "browser.entriesRequest?.controller.abort();", "browser.entriesRequest = null;", "browser.entriesSequence += 1", "liveBrowserState = null;"):
            self.assertIn(marker, close_source)
        cleanup = re.search(r"function clearLiveBrowserRefresh\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(cleanup)
        self.assertIn("clearTimeout(browser.refreshTimer)", cleanup.group(0))
        self.assertIn("browser.refreshPending = false", cleanup.group(0))


class StorageCardsUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.css = UI_CSS_PATH.read_text(encoding="utf-8")

    def test_storage_renders_card_grid_with_filters_sort_and_pagination(self):
        storage = re.search(r"function renderStorage\(content\) \{.*?\n    let storageModalState", self.source, re.S)
        self.assertIsNotNone(storage)
        storage_source = storage.group(0)
        for marker in (
            "storageTableState",
            "storage-grid",
            "storage-card",
            'data-storage-filter="name"',
            'data-storage-filter="backend"',
            "storageCardsContainer",
            "storageEmptyState",
            "Sin storage profiles",
            "Sin profiles que coincidan",
            "Mostrando ",
        ):
            self.assertIn(marker, storage_source)
        self.assertNotIn('<tbody id="storageTbody"', storage_source)

    def test_storage_pagination_resets_page_on_filter_change_and_sorts(self):
        storage = re.search(r"function renderStorage\(content\) \{.*?\n    let storageModalState", self.source, re.S)
        self.assertIsNotNone(storage)
        storage_source = storage.group(0)
        for marker in (
            'storageTableState.page = 1',
            "storageTableState.filters[input.dataset.storageFilter] = input.value",
            "storageTableState.sortKey",
            "storageTableState.sortDir",
            "storagePageSize",
            "renderStorageCards()",
        ):
            self.assertIn(marker, storage_source)
        self.assertIn("storageTableState.page = parseInt(btn.dataset.storagePage, 10)", self.source)

    def test_storage_cards_have_independent_about_state_and_no_polling(self):
        storage = re.search(r"function renderStorage\(content\) \{.*?\n    let storageModalState", self.source, re.S)
        self.assertIsNotNone(storage)
        storage_source = storage.group(0)
        for marker in (
            "storageAboutState",
            "about-unsupported",
            "transient-failure",
            "not-configured",
            "About not supported",
            "No configurado",
            "Refrescar",
            "storage-profiles/${encodeURIComponent(profileId)}/about",
        ):
            self.assertIn(marker, storage_source)
        self.assertNotIn("setInterval", storage_source)
        self.assertNotIn("setTimeout", storage_source)

    def test_storage_cards_escape_displayed_values_and_never_render_secrets(self):
        card = re.search(r"function renderStorageCard\(profile\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(card)
        card_source = card.group(0)
        for marker in (
            "escapeHtml(profile.name)",
            "escapeHtml(profile.backend_type)",
            "escapeHtml(about.error)",
        ):
            self.assertIn(marker, card_source)
        self.assertNotIn("environment", card_source)
        self.assertNotIn("secret_refs", card_source)
        self.assertNotIn("RCLONE_REMOTE", card_source)

    def test_storage_card_grid_css_is_responsive_and_reuses_pagination(self):
        css = self.css
        for marker in (
            ".storage-grid",
            ".storage-card",
            "repeat(auto-fill, minmax(",
        ):
            self.assertIn(marker, css)
        self.assertIn("@media (max-width: 768px)", css)
        self.assertNotIn("storage-grid", self.source.split("renderStorage(content)", 1)[0])


class WorkerUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.css = UI_CSS_PATH.read_text(encoding="utf-8")

    def test_worker_labels_use_key_value_rows_with_validation_and_clear_action(self):
        for marker in (
            "renderWorkerLabelRows",
            "data-label-key",
            "data-label-value",
            "data-add-label",
            "data-remove-label",
            "data-clear-labels",
            "repetida",
            "La clave de cada label es obligatoria",
            "aria-invalid",
            "role=\"dialog\"",
            "aria-modal=\"true\"",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("editLabelsTextarea", self.source)
        self.assertNotIn("JSON invalido:", self.source)

    def test_worker_labels_are_sorted_and_polling_detects_heartbeat_label_changes(self):
        self.assertIn('Object.keys(labels).sort((a, b) => a.localeCompare(b, "es"))', self.source)
        self.assertIn("function workerFingerprint(worker)", self.source)
        self.assertIn("worker.labels[key]", self.source)
        self.assertIn("newWorkers.map(workerFingerprint)", self.source)

    def test_enrollment_compose_contains_shared_tmp_mount(self):
        compose = re.search(r"const composeYml = `.*?`;", self.source, re.S)
        self.assertIsNotNone(compose)
        compose_source = compose.group(0)
        self.assertIn("- /var/run/docker.sock:/var/run/docker.sock", compose_source)
        self.assertIn("- /tmp:/tmp", compose_source)
        self.assertIn("- worker_state:/data", compose_source)
        self.assertIn("    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n      - /tmp:/tmp\n      - worker_state:/data", compose_source)

    def test_worker_admin_actions_are_confirmed_and_have_refresh_feedback(self):
        for marker in (
            "data-revoke-worker",
            "data-delete-worker",
            "window.confirm",
            "/api/v1/admin/workers/${encodeURIComponent(workerId)}/revoke",
            "apiDelete(`/api/v1/workers/${encodeURIComponent(workerId)}`)",
            "workersNotice",
            "await refreshAll();",
            "Worker revocado",
            "worker-disabled-row",
        ):
            self.assertIn(marker, self.source)
        self.assertIn(".worker-notice", self.css)
        self.assertIn(".pill.danger", self.css)

    def test_worker_enrollment_native_tabs_assets_and_secret_handling(self):
        for marker in (
            'role="tablist" aria-label="Opciones de despliegue del worker"',
            'role="tab" aria-selected="${addWorkerState.activeTab === "compose" ? "true" : "false"}',
            'data-tab="compose"',
            'data-tab="native"',
            'Worker nativo',
            'nativePlatformSelect',
            'linux-amd64',
            'linux-arm64',
            'windows-amd64',
            'checksums',
            'assetForSelectedNativePlatform',
            'renderNativeWorkerTab',
            'No se infiere desde el navegador',
            'vaultline-worker enroll',
            'copyEnrollmentSecret',
            'Copiar secreto',
            'Copiar comandos',
            'Token de un solo uso en el comando',
            'SHA256',
            'SHA256SUMS',
            'No hay assets nativos publicados para esta version',
            'state.latestAssets',
        ):
            self.assertIn(marker, self.source)
        commands_block = re.search(r"function renderNativeWorkerTab\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(commands_block)
        tab_source = commands_block.group(0)
        # The token is embedded on purpose (single-use, short-lived); the old
        # "never paste it into commands" guidance must be gone.
        self.assertIn("addWorkerState.enrollment", tab_source)
        self.assertIn("--bootstrap-token ${secret}", tab_source)
        self.assertIn("NATIVE_WORKER_TOKEN_PLACEHOLDER", tab_source)
        self.assertIn("NATIVE_WORKER_ASSET_URL_PLACEHOLDER", tab_source)
        self.assertIn("Generar credencial", tab_source)
        self.assertNotIn("WORKER_ENROLLMENT_TOKEN", tab_source)
        for stale in ("prompt oculto", "No lo pegues en comandos", "Secreto separado"):
            self.assertNotIn(stale, self.source)
        self.assertIn('const NATIVE_WORKER_TOKEN_PLACEHOLDER = "<BOOTSTRAP_TOKEN>";', self.source)
        self.assertIn('const NATIVE_WORKER_ASSET_URL_PLACEHOLDER = "<ASSET_URL>";', self.source)
        self.assertIn("ttl_minutes };", self.source)
        self.assertIn("${escapeHtml(commands)}", tab_source)

    def test_worker_enrollment_native_instructions_preserve_platform_contract(self):
        for marker in (
            "dvb-worker",
            "/etc/docker-volume-backup/worker.env",
            "/var/lib/docker-volume-backup",
            "docker-volume-backup-worker.service",
            "VaultlineWorker",
            "ProgramData\\\\Vaultline\\\\worker",
            "glibc >= 2.17",
            "SmartScreen",
            "Los ejecutables de Restic y rclone se incluyen con el worker nativo",
            "rclone.conf</code> y las credenciales del repositorio en secretos separados",
            "Instala por separado las demás herramientas externas (tar, gpg, aws, ssh y scp)",
            "espera el enrolamiento",
        ):
            self.assertIn(marker, self.source)
        self.assertNotIn("navigator.platform", self.source)
        self.assertNotIn("userAgent", self.source)

    def test_worker_enrollment_native_tab_teaches_install_then_enroll_service_flow(self):
        platforms = re.search(r"const NATIVE_WORKER_PLATFORMS = \{.*?\n    \};", self.source, re.S)
        self.assertIsNotNone(platforms)
        platforms_source = platforms.group(0)
        # The MSI is the primary Windows download; the portable exe is secondary.
        self.assertIn('"windows-amd64": { label: "Windows x64 instalador (.msi)"', platforms_source)
        self.assertIn('key: "windows-amd64-msi"', platforms_source)
        self.assertIn('secondaryKey: "windows-amd64"', platforms_source)
        self.assertIn("Ejecutable portable (.exe) para diagnostico", platforms_source)
        self.assertIn('key: "linux-amd64"', platforms_source)
        self.assertIn('key: "linux-arm64"', platforms_source)
        self.assertNotIn("Windows x64 portable (.exe)", platforms_source)

        selector = re.search(r"function assetForSelectedNativePlatform\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(selector)
        self.assertIn("assets[info.key]", selector.group(0))
        self.assertIn("assets[info.secondaryKey]", selector.group(0))

        tab = re.search(r"function renderNativeWorkerTab\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(tab)
        tab_source = tab.group(0)
        windows = re.search(r"const windowsCommands = `(.*?)`;", tab_source, re.S)
        linux = re.search(r"const linuxCommands = `(.*?)`;", tab_source, re.S)
        self.assertIsNotNone(windows)
        self.assertIsNotNone(linux)
        windows_commands = windows.group(1)
        linux_commands = linux.group(1)

        # Copy-paste contract: every line is a command (no `#` comments, no blank
        # lines), ordered download -> install -> enroll with token -> status.
        for name, block in (("windows", windows_commands), ("linux", linux_commands)):
            with self.subTest(block=name):
                lines = block.split("\n")
                self.assertTrue(all(line.strip() for line in lines), lines)
                self.assertFalse(any(line.lstrip().startswith("#") for line in lines), lines)
                self.assertIn("enroll --control-plane-url ${cpUrl} --bootstrap-token ${secret}", block)
                self.assertIn("${assetUrl}", block)

        self.assertIn("Invoke-WebRequest -Uri ${assetUrl} -OutFile $msi", windows_commands)
        self.assertIn("$msi = \"$env:TEMP\\\\${packageName}\"", windows_commands)
        # msiexec returns immediately in PowerShell: the block must wait for the install.
        self.assertIn("Start-Process msiexec.exe -ArgumentList ('/i \"{0}\" /qn /norestart' -f $msi) -Wait", windows_commands)
        # A fresh install updates the machine PATH only; the session must reload it.
        self.assertIn('$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine")', windows_commands)
        # Defender's Behavior:Win32/SuspRclone.A kills rclone mid-backup; the
        # exclusion is one explicit line scoped to the single installed file.
        self.assertIn('Add-MpPreference -ExclusionPath "C:\\\\Program Files\\\\Vaultline\\\\rclone.exe"', windows_commands)
        self.assertEqual(windows_commands.count("Add-MpPreference"), 1)
        self.assertNotIn("-ExclusionProcess", windows_commands)
        self.assertNotIn('-ExclusionPath "C:\\\\Program Files\\\\Vaultline"', windows_commands)
        self.assertIn("vaultline-worker enroll --control-plane-url ${cpUrl} --bootstrap-token ${secret}", windows_commands)
        self.assertIn("vaultline-worker service status", windows_commands)
        self.assertLess(windows_commands.index("Invoke-WebRequest"), windows_commands.index("msiexec.exe"))
        self.assertLess(windows_commands.index("msiexec.exe"), windows_commands.index("$env:Path ="))
        self.assertLess(windows_commands.index("$env:Path ="), windows_commands.index("Add-MpPreference"))
        self.assertLess(windows_commands.index("Add-MpPreference"), windows_commands.index("enroll --control-plane-url"))
        self.assertLess(windows_commands.index("enroll --control-plane-url"), windows_commands.index("service status"))
        self.assertNotIn("Add-MpPreference", linux_commands)

        self.assertIn("curl -fsSL -o ${packageName} ${assetUrl}", linux_commands)
        self.assertIn("sudo dpkg -i ${packageName}", linux_commands)
        self.assertIn("sudo vaultline-worker enroll --control-plane-url ${cpUrl} --bootstrap-token ${secret}", linux_commands)
        self.assertIn("sudo systemctl status docker-volume-backup-worker.service --no-pager", linux_commands)
        self.assertLess(linux_commands.index("curl -fsSL"), linux_commands.index("dpkg -i"))
        self.assertLess(linux_commands.index("dpkg -i"), linux_commands.index("enroll --control-plane-url"))
        self.assertLess(linux_commands.index("enroll --control-plane-url"), linux_commands.index("systemctl status"))

        # Guidance lives in the panel text, not inside the command block.
        self.assertIn("espera el enrolamiento", tab_source)
        self.assertIn("Run as administrator", tab_source)
        self.assertIn("journalctl -u docker-volume-backup-worker.service -f", tab_source)
        self.assertIn('data-code-id="modalCode">Copiar comandos</button>', tab_source)

        # The service is already running after install: no manual enable/start and no Task Scheduler.
        for stale in (
            "Task Scheduler",
            "ScheduledTask",
            "systemctl enable --now",
            "sudo editor",
            "No usa Windows SCM",
            "no arranca ni enrola automaticamente",
        ):
            self.assertNotIn(stale, tab_source)
        self.assertIn("SmartScreen", tab_source)
        # The WHY of the Defender exclusion lives in the panel text, not the block.
        self.assertIn("Behavior:Win32/SuspRclone.A", tab_source)
        self.assertIn("rclone stdio connection already closed", tab_source)
        self.assertIn("acotada a un unico archivo instalado", tab_source)
        self.assertIn("sin reiniciar", tab_source)
        self.assertIn("sin necesidad de volver a enrolar", tab_source)
        self.assertIn("worker.env es opcional", tab_source)
        self.assertIn("vaultline-worker.exe daemon", tab_source)
        self.assertIn("service install", tab_source)

    def test_worker_enrollment_renewal_preserves_stable_id_and_reuses_compose_flow(self):
        for marker in (
            "data-renew-enrollment",
            "Renovar enrollment",
            "/api/v1/admin/workers/${encodeURIComponent(workerId)}/enrollment",
            "worker_id",
            "readonly",
            "TTL (minutos)",
            "Si el worker ya existe",
            "Este enrollment conserva el mismo",
            "El token pendiente anterior se invalida",
            "- /tmp:/tmp",
        ):
            self.assertIn(marker, self.source)
        workers = re.search(r"function renderWorkers\(content\) \{.*?\n    let _editLabelsWorkerId", self.source, re.S)
        self.assertIsNotNone(workers)
        self.assertIn("openRenewWorkerModal(btn.dataset.renewEnrollment)", workers.group(0))

    def test_settings_submenus_group_options_and_switch_panels_accessibly(self):
        for marker in (
            'role="tablist" aria-label="Secciones de configuración"',
            'data-settings-tab="backups"',
            'data-settings-tab="control-plane"',
            'data-settings-tab="notifications"',
            'data-settings-panel="backups"',
            'data-settings-panel="control-plane"',
            'data-settings-panel="notifications"',
            'id="notificationChannelManager"',
        ):
            self.assertIn(marker, self.source)

        for panel_id, expected_controls in (
            ("settingsPanelBackups", ("settingsRepoBase", "settingsRcloneSecret", "settingsGlobalCron", "settingsPasswordSecret")),
            ("settingsPanelControlPlane", ("settingsPublicUrl", "settingsSnapshotListingMaxOutput")),
            ("settingsPanelNotifications", ("notificationChannelManager",)),
        ):
            panel = re.search(rf'<section id="{panel_id}".*?</section>', self.source, re.S)
            self.assertIsNotNone(panel)
            for control in expected_controls:
                self.assertIn(f'id="{control}"', panel.group(0))

        helper = re.search(r"function initializeSettingsSubmenus\(root\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(helper)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = helper.group(0) + r'''
const tabs = ["backups", "control-plane", "notifications"].map((name, index) => {
  const attributes = { "aria-selected": String(index === 0) };
  const listeners = {};
  return {
    dataset: { settingsTab: name },
    tabIndex: index === 0 ? 0 : -1,
    addEventListener(type, listener) { listeners[type] = listener; },
    setAttribute(name, value) { attributes[name] = value; },
    getAttribute(name) { return attributes[name]; },
    focus() { this.focused = true; },
    attributes,
    listeners,
  };
});
const panels = ["backups", "control-plane", "notifications"].map((name, index) => ({
  dataset: { settingsPanel: name },
  hidden: index !== 0,
}));
const saveActions = { hidden: false };
const root = {
  querySelectorAll(selector) {
    if (selector === '[role="tab"][data-settings-tab]') return tabs;
    if (selector === '[role="tabpanel"][data-settings-panel]') return panels;
    return [];
  },
  querySelector(selector) { return selector === "#settingsFormActions" ? saveActions : null; },
};
const assert = (condition, message) => { if (!condition) throw new Error(message); };
initializeSettingsSubmenus(root);
assert(tabs[0].getAttribute("aria-selected") === "true" && tabs[0].tabIndex === 0, "backups should be selected by default");
assert(!panels[0].hidden && panels[1].hidden && panels[2].hidden, "only the selected panel should be visible");
tabs[2].listeners.click();
assert(tabs[2].getAttribute("aria-selected") === "true" && panels[2].hidden === false, "click should open notifications");
assert(saveActions.hidden, "global settings save action should be hidden on notifications");
let prevented = false;
tabs[2].listeners.keydown({ key: "ArrowLeft", preventDefault() { prevented = true; } });
assert(prevented && tabs[1].getAttribute("aria-selected") === "true", "ArrowLeft should select the previous tab");
assert(tabs[1].focused && !panels[1].hidden && !saveActions.hidden, "keyboard selection should focus the panel tab and restore save actions");
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hidden_settings_validation_reveals_control_before_focus(self):
        form = re.search(r'<form id="settingsForm"[^>]*>', self.source)
        self.assertIsNotNone(form)
        self.assertIn("novalidate", form.group(0))
        for marker in (
            'focusSettingsControl("settingsTabBackups", "settingsRepoBase")',
            'focusSettingsControl("settingsTabControlPlane", "settingsSnapshotListingMaxOutput")',
        ):
            self.assertIn(marker, self.source)

        helper = re.search(r"function focusSettingsControl\(tabId, controlId\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(helper)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = helper.group(0) + r'''
let tabSelected = false;
let panelVisible = false;
let fieldFocused = false;
const tab = {
  getAttribute(name) { return name === "aria-selected" && tabSelected ? "true" : "false"; },
  click() { tabSelected = true; panelVisible = true; },
};
const field = { focus() { if (!panelVisible) throw new Error("focused a hidden control"); fieldFocused = true; } };
global.document = { getElementById(id) {
  if (id === "settingsTabControlPlane") return tab;
  if (id === "settingsSnapshotListingMaxOutput") return field;
  return null;
} };
focusSettingsControl("settingsTabControlPlane", "settingsSnapshotListingMaxOutput");
if (!tabSelected || !fieldFocused) throw new Error("validation target was not revealed before focus");
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_settings_exposes_snapshot_listing_limit_in_mib_and_wires_bytes_patch(self):
        settings = re.search(
            r"function renderSettings\(content\) \{.*?\n    \}\n\n    async function refreshAll",
            self.source,
            re.S,
        )
        self.assertIsNotNone(settings)
        settings_source = settings.group(0)
        for marker in (
            "snapshot_explorer_listing_max_output_bytes",
            "snapshotListingMaxMiB",
            "Number(settings.snapshot_explorer_listing_max_output_bytes",
            'id="settingsSnapshotListingMaxOutput"',
            'type="number" min="1" max="16" step="1"',
            "restic ls --json",
            "No cambia el límite de 8 MiB para descargar archivos",
            'role="alert"',
            "listingLimitMiB * 1024 * 1024",
            'apiPatch("/api/v1/settings"',
            "El límite del listado debe ser un número entero entre 1 y 16 MiB.",
        ):
            self.assertIn(marker, settings_source)

    def test_settings_manages_multiple_webhook_and_smtp_channels_safely(self):
        settings_loader = re.search(r'else if \(view === "settings"\) \{.*?\n      \}', self.source, re.S)
        self.assertIsNotNone(settings_loader)
        self.assertIn('"notificationChannels", "/api/v1/notification-channels"', settings_loader.group(0))

        manager = re.search(r"function renderNotificationChannelManager\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(manager)
        manager_source = manager.group(0)
        for marker in (
            "state.notificationChannels",
            "channel.config_summary",
            "webhook",
            "smtp",
            "apiPost(\"/api/v1/notification-channels\"",
            "apiPatch(`/api/v1/notification-channels/",
            "apiDelete(`/api/v1/notification-channels/",
            "STARTTLS",
            "Nueva contraseña SMTP",
            "URL del webhook",
            "dialog.showModal()",
            "data-notification-test-form",
            "test-email",
        ):
            self.assertIn(marker, manager_source)
        self.assertNotIn("config_ciphertext", manager_source)
        self.assertIn("renderNotificationChannelManager()", self.source)
        for marker in (".notification-feedback", ".notification-feedback.is-error", ".notification-test-modal:not([open])"):
            self.assertIn(marker, self.css)

    def _run_smtp_test_dialog_node_check(self, assertions, expected_stdout):
        renderer = re.search(r"function renderNotificationChannelManager\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(renderer)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = SMTP_TEST_DIALOG_MOCK_PRELUDE + renderer.group(0) + assertions
        # The combined script exceeds the Windows command-line limit; run from a temp file.
        with tempfile.TemporaryDirectory(prefix="smtp-test-dialog-check-") as tmp:
            script_path = Path(tmp) / "smtp_test_dialog_check.js"
            script_path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                ["node", str(script_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, expected_stdout)

    def test_notification_smtp_test_dialog_sends_explicit_configured_subset_and_feedback(self):
        self._run_smtp_test_dialog_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  const expected = [
    'data-notification-test-open="smtp-1"',
    'data-notification-test-form="smtp-1"',
    'Enviar correo de prueba',
    'oncall@example.test',
    'admin@example.test',
    'Canal creado.',
    'notification-feedback is-success',
    'data-notification-test-recipient',
    'type="checkbox"',
    'data-notification-test-mode="configured"',
    'data-notification-test-mode="custom"',
    'type="email"',
    'for="notificationTestCustom-smtp-1"',
    'id="notificationTestCustom-smtp-1"',
    'envío de prueba',
    'reporte ficticio de TESTING',
  ];
  const missing = expected.filter(value => !markup.includes(value));
  if (missing.length) throw new Error(`missing rendered notification UI: ${missing.join("; ")}`);
  if ((markup.match(/data-notification-test-open=/g) || []).length !== 1) throw new Error("only SMTP channels should offer a test email");
  if (!markup.includes("notification-test-modal")) throw new Error("SMTP test confirmation is not in a modal");
  const checkboxTags = markup.match(/<input[^>]*data-notification-test-recipient[^>]*>/g) || [];
  if (checkboxTags.length !== 2) throw new Error("configured recipients must render as selectable checkboxes");
  if (checkboxTags.some(tag => tag.includes("checked"))) throw new Error("configured recipients must start unchecked");
  if (markup.includes("type=" + '"email" checked')) throw new Error("unexpected preselection");
  if (apiCalls.length) throw new Error("test email sent before explicit modal confirmation");
  events["open:click"]();
  if (!dialog.open) throw new Error("test email button did not open its confirmation modal");
  events["close:click"]();
  if (dialog.open) throw new Error("cancel did not close the confirmation modal");
  events["open:click"]();
  checkboxes[0].checked = true;
  await events["submit"]({ preventDefault() {} });
  if (dialog.open) throw new Error("successful test send did not close the modal");
  if (apiCalls.length !== 1 || apiCalls[0].path !== "/api/v1/notification-channels/smtp-1/test-email") throw new Error(`wrong test email request: ${JSON.stringify(apiCalls)}`);
  if (JSON.stringify(apiCalls[0].body) !== JSON.stringify({ recipients: ["oncall@example.test"] })) throw new Error("explicit selection must be sent, not {}; got " + JSON.stringify(apiCalls[0].body));
  if (state.notificationChannelFeedback.message !== "Correo de prueba enviado a oncall@example.test, admin@example.test.") throw new Error("success feedback did not report the actual recipient list");
  if (!markup.includes("notification-feedback is-success")) throw new Error("send success was not rendered as a styled feedback chip");
  process.stdout.write("configured subset payload and feedback chip passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "configured subset payload and feedback chip passed",
        )

    def test_notification_smtp_test_dialog_blocks_empty_selection_and_clears_transient_state_on_reopen(self):
        self._run_smtp_test_dialog_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["open:click"]();
  checkboxes[0].checked = true;
  checkboxes[1].checked = true;
  modeRadios[1].checked = true;
  modeRadios[0].checked = false;
  events["change"]({ target: modeRadios[1] });
  customInput.value = "externo@example.test";
  events["close:click"]();
  events["open:click"]();
  if (checkboxes.some(box => box.checked || box.disabled)) throw new Error("reopen must clear recipient checkbox transient state");
  if (customInput.value !== "" || customInput.disabled !== true) throw new Error("reopen must clear the custom email field");
  if (customGroup.hidden !== true) throw new Error("reopen must hide the custom email field");
  if (modeRadios[0].checked !== true || modeRadios[1].checked) throw new Error("reopen must return to the configured mode");
  await events["submit"]({ preventDefault() {} });
  if (apiCalls.length) throw new Error("empty selection must not send a test email");
  if (errorElement.hidden === true || !errorElement.textContent) throw new Error("empty selection must be reported in the modal error area");
  if (sendButton.disabled) throw new Error("send button must be re-enabled after a blocked send");
  if (dialog.open !== true) throw new Error("blocked send must keep the modal open");
  process.stdout.write("empty selection blocked and reopened modal cleared passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "empty selection blocked and reopened modal cleared passed",
        )

    def test_notification_smtp_test_dialog_custom_mode_is_exclusive_and_sends_single_custom_email(self):
        self._run_smtp_test_dialog_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["open:click"]();
  checkboxes[0].checked = true;
  modeRadios[1].checked = true;
  modeRadios[0].checked = false;
  events["change"]({ target: modeRadios[1] });
  if (customInput.disabled !== false) throw new Error("custom field must be enabled in custom mode");
  if (checkboxes.some(box => box.disabled !== true)) throw new Error("configured checkboxes must be disabled in custom mode");
  customInput.value = "sin-arroba";
  await events["submit"]({ preventDefault() {} });
  if (apiCalls.length) throw new Error("invalid custom email must not reach the server");
  if (errorElement.hidden === true || !errorElement.textContent) throw new Error("invalid custom email must show a modal error");
  if (sendButton.disabled) throw new Error("send button must be re-enabled after an invalid custom email");
  if (dialog.open !== true) throw new Error("invalid custom email must keep the modal open");
  customInput.value = "externo@example.test";
  await events["submit"]({ preventDefault() {} });
  if (dialog.open) throw new Error("successful custom send did not close the modal");
  if (apiCalls.length !== 1) throw new Error(`wrong number of custom sends: ${JSON.stringify(apiCalls)}`);
  const payload = apiCalls[0].body;
  if (JSON.stringify(payload) !== JSON.stringify({ recipients: ["externo@example.test"] })) throw new Error("custom mode must send only the custom email; got " + JSON.stringify(payload));
  if (JSON.stringify(payload).includes("oncall@example.test")) throw new Error("custom mode must not include configured addresses");
  if (state.notificationChannelFeedback.message !== "Correo de prueba enviado a oncall@example.test, admin@example.test.") throw new Error("success feedback did not report the actual recipients returned by the server");
  process.stdout.write("exclusive custom mode sent one custom email passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "exclusive custom mode sent one custom email passed",
        )

    def test_notification_setup_guide_documents_channels_and_is_linked(self):
        guide = NOTIFICATION_GUIDE_PATH.read_text(encoding="utf-8")
        readme = README_PATH.read_text(encoding="utf-8")
        for marker in (
            "Webhook",
            "SMTP",
            "STARTTLS",
            "correo de prueba",
            "backup.failed",
            "primeras y últimas líneas",
            "Contenido omitido",
            "archivo adjunto",
            "log completo retenido",
            "control_plane_public_url",
            "un intento",
            "redes internas",
        ):
            self.assertIn(marker.lower(), guide.lower())
        self.assertIn("[Notificaciones de backups fallidos](doc/backup-failure-notifications.md)", readme)

    def test_restore_ownership_ui_uses_simple_spanish_choices_and_preserves_payload_contract(self):
        restore = re.search(r"function openRestoreFromSnapshotModal\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(restore)
        restore_source = restore.group(0)
        self.assertIn('spec.stable_key || spec.volume_key || ""', self.source)
        for marker in (
            'getRestoreOwnershipVolumeCandidates(state.target)',
            'const restoreServiceControls = isNativeTarget ? "" : `',
            '${restoreServiceControls}',
            'name="restore_overwrite"',
            'name="restore_mode"',
            'Conservar archivos existentes',
            'Reemplazar archivos existentes',
            'Detener servicios antes de restaurar',
            'Mantener servicios activos',
            'Conservar permisos del backup',
            'Aplicar permisos manuales',
            'data-restore-mapping',
            'id="rsOwnershipAdvanced"',
            'Configuración avanzada',
            'id="rsOwnershipStatus"',
            'aria-live="polite"',
            'id="rsOwnershipConfirm"',
            'Confirmo que revisé las opciones y acepto reemplazar los datos del target.',
            'Acción destructiva:',
            'id="rsConfirm" aria-describedby="rsOwnershipStatus rsRestoreWarning" disabled',
            'const syncRestoreButton = policy =>',
            'const ready = Boolean(policy) && Boolean(confirmation?.checked);',
            'restoreButton.disabled = !ready;',
            'if (confirmation) confirmation.addEventListener("change", preview);',
            'const firstInvalid = overlay.querySelector(\'[aria-invalid="true"]\');',
            'Para continuar, valida la configuración y confirma que aceptas reemplazar los datos.',
            'La configuración es válida, pero debes confirmar que aceptas reemplazar los datos para continuar.',
            'Corrige la sintaxis del JSON y vuelve a validar la configuración.',
            'restore_ownership: policy',
            'policy.confirmation = "confirmed"',
            'force_overwrite: forceOverwrite',
            'const stopContainers = !isNativeTarget && document.getElementById("rsMode")?.value === "cold";',
            'stop_containers: stopContainers',
            'if (policy.mode === "map" && values.length === 1) request.chown = values[0];',
            'Los volúmenes restaurados se detectaron automáticamente.',
            'Ruta restaurada:',
            'data-mapping-key="${escapeHtml(candidate.key)}"',
            'data-mapping-label="${escapeHtml(candidate.name)}"',
            'placeholder="1000:1000…"',
            'aria-describedby="rsMappingHint${index} rsMappingError${index}"',
            'class="restore-mapping-hint"',
            'Formato: UID:GID',
            'Este target no expone claves estables de volúmenes.',
            'no generaremos claves a partir de rutas del host',
        ):
            self.assertIn(marker, restore_source)
        self.assertIn('data-restore-mapping', restore_source)
        self.assertNotIn('class="restore-mapping-key"', restore_source)
        self.assertNotIn('Los restores nativos no detienen contenedores ni usan Kubernetes.', restore_source)
        self.assertNotIn('name="restore_mode" autocomplete="off" disabled', restore_source)
        service_controls = re.search(r'const restoreServiceControls = isNativeTarget \? "" : `(.*?)`;', restore_source, re.S)
        self.assertIsNotNone(service_controls)
        self.assertIn('legend>Servicios durante la restauración</legend>', service_controls.group(1))
        manual = re.search(r'<div id="rsOwnershipManual".*?</div>`\n        :', restore_source, re.S)
        self.assertIsNotNone(manual)
        self.assertNotIn('<code', manual.group(0))
        for marker in (
            'Restore ownership request JSON',
            'Use preserve or explicit numeric UID:GID mappings',
            'Validated request; confirmation required',
            'I confirm this preserve or numeric UID:GID request',
            'Decline',
            'Preview request',
            'Confirm restore',
            'Cold restore',
            'Hot restore',
        ):
            self.assertNotIn(marker, restore_source)
        self.assertIn('"schema_version":1,"mode":"preserve","mappings":{}', restore_source)
        self.assertIn('compose:proyecto:volumen', restore_source)
        self.assertIn('UID:GID', restore_source)
        self.assertIn('renderRestoreOwnershipEvidence(job)', self.source)

    def test_restore_ownership_candidate_helper_creates_one_mapping_row_per_stable_volume(self):
        helper = re.search(
            r"(function getRestoreOwnershipVolumeCandidates\(target\) \{.*?\n    \})\n\n    function openRestoreFromSnapshotModal",
            self.source,
            re.S,
        )
        self.assertIsNotNone(helper)
        restore = re.search(r"function openRestoreFromSnapshotModal\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(restore)
        restore_source = restore.group(0)
        self.assertIn('volumeCandidates.map((candidate, index) => `<div class="restore-mapping-row">', restore_source)
        self.assertIn('data-restore-mapping', restore_source)

        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = helper.group(1) + r'''
const target = {
  volume_targets: ["/restore/one", "/restore/two"],
  runtime_volumes: {
    first_source: { bind: "/restore/one", name: "first", stable_key: "compose:project:first" },
    second_source: { bind: "/restore/two", name: "second", stable_key: "compose:project:second" },
    ignored_source: { bind: "/ignored", name: "ignored", stable_key: "compose:project:ignored" },
  },
};
const candidates = getRestoreOwnershipVolumeCandidates(target);
const rows = candidates.map((candidate, index) => ({ id: `rsMapping${index}`, key: candidate.key }));
process.stdout.write(JSON.stringify({ candidates, rows }));
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual([candidate["key"] for candidate in output["candidates"]], [
            "compose:project:first",
            "compose:project:second",
        ])
        self.assertEqual(len(output["rows"]), 2)
        self.assertEqual([row["id"] for row in output["rows"]], ["rsMapping0", "rsMapping1"])

    def test_restore_snapshot_modal_keeps_scrollable_body_and_usable_footer_focus(self):
        styles_start = self.css.index(".restore-snapshot-modal {")
        styles_end = self.css.index("@media (max-width: 560px)", styles_start)
        restore_styles = self.css[styles_start:styles_end]
        for marker in (
            '.restore-snapshot-modal .modal-body { min-height: 0; overflow-y: auto;',
            '.restore-snapshot-modal .modal-footer { flex-shrink: 0;',
            '.restore-snapshot-modal :is(input, select, textarea, button):focus-visible',
            '.restore-snapshot-confirmation { display: flex;',
            '.restore-choice:focus-within',
            '.restore-mapping-row {',
            'grid-template-columns: minmax(0, 1fr) minmax(170px, .45fr);',
            '.restore-mapping-input input {',
            'min-height: 40px;',
            'font: 600 14px/1.2 var(--font-mono);',
            '.restore-mapping-input input:hover',
            '.restore-mapping-input input:focus-visible',
            '.restore-mapping-input input[aria-invalid="true"]',
            '.restore-mapping-hint {',
        ):
            self.assertIn(marker, self.css)
        self.assertNotIn('transition: all', restore_styles)

    def test_volume_selector_has_keyboard_focus_and_visible_inventory_warning_styles(self):
        for marker in (
            ".volume-group-header:focus-visible",
            ".volume-card:focus-within",
            ".volume-card .volume-checkbox",
            ".volume-inline-warning",
            ".volume-selection-count",
        ):
            self.assertIn(marker, self.css)

    def test_edit_target_modal_uses_responsive_two_column_layout_and_keeps_create_scoped(self):
        for marker in (
            '<div class="modal edit-target-modal" role="dialog"',
            'id="editTargetForm" class="edit-target-form"',
            'class="edit-target-layout"',
            'class="edit-target-settings"',
            'class="edit-target-volume-panel"',
            'class="modal-footer edit-target-footer"',
            'form="editTargetForm"',
        ):
            self.assertIn(marker, self.source)

        modal = re.search(r"\.edit-target-modal \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(modal)
        modal_source = modal.group(0)
        for marker in (
            "width: min(1080px, calc(100vw - 32px));",
            "max-width: 1080px;",
            "max-height: min(900px, calc(100vh - 32px));",
            "overflow: hidden;",
        ):
            self.assertIn(marker, modal_source)

        layout = re.search(r"\.edit-target-layout \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(layout)
        layout_source = layout.group(0)
        for marker in (
            "display: grid;",
            "grid-template-columns: minmax(0, 1.1fr) minmax(340px, .9fr);",
            "min-width: 0;",
        ):
            self.assertIn(marker, layout_source)
        self.assertRegex(self.css, r"(?s)@media \(max-width: 900px\) \{.*?\.edit-target-layout \{ grid-template-columns: 1fr;")

        for marker in (
            ".edit-target-modal .modal-body",
            "overflow-y: auto;",
            ".edit-target-modal .modal-footer",
            "position: sticky;",
            ".edit-target-volume-panel .volume-list",
            "overflow-x: hidden;",
        ):
            self.assertIn(marker, self.css)

    def test_volume_selector_uses_one_responsive_scroll_container(self):
        for marker in (
            'id="targetVolumeList" class="volume-list"',
            'id="editTargetVolumeList" class="volume-list"',
        ):
            self.assertIn(marker, self.source)

        volume_list = re.search(r"^\.volume-list \{.*?\n\}", self.css, re.M | re.S)
        self.assertIsNotNone(volume_list)
        volume_list_source = volume_list.group(0)
        for marker in (
            "height: clamp(240px, 42vh, 420px);",
            "min-height: 0;",
            "max-height: none;",
            "overflow-y: auto;",
            "scrollbar-gutter: stable;",
            "overscroll-behavior: contain;",
        ):
            self.assertIn(marker, volume_list_source)

        volume_group = re.search(r"\.volume-group \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(volume_group)
        self.assertIn("flex: 0 0 auto;", volume_group.group(0))

        volume_group_body = re.search(r"\.volume-group-body \{.*?\n\}", self.css, re.S)
        self.assertIsNotNone(volume_group_body)
        volume_group_body_source = volume_group_body.group(0)
        self.assertIn("grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));", volume_group_body_source)
        self.assertIn("gap: 8px;", volume_group_body_source)
        self.assertIn("max-height: none;", volume_group_body_source)
        self.assertIn("overflow: visible;", volume_group_body_source)
        self.assertNotIn("overflow-y", volume_group_body_source)
        self.assertRegex(
            self.css,
            r"@media \(max-width: 640px\) \{\s*\.volume-group-body \{ grid-template-columns: 1fr; \}\s*\}",
        )


class PageScopedLoaderUiStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def test_home_refresh_only_requests_shared_and_dashboard_data(self):
        nav_items = re.search(r"const NAV_ITEMS = \[.*?\n    \];", self.source, re.S)
        current_view = re.search(r"function currentRouteView\(\) \{.*?\n    \}", self.source, re.S)
        should_fetch_jobs = re.search(r"function shouldFetchJobsDuringRefresh\(view\) \{.*?\n    \}", self.source, re.S)
        dashboard_jobs = re.search(r"async function fetchDashboardJobs\(\) \{.*?\n    \}", self.source, re.S)
        page_loader = re.search(r"function loadPageData\(view, force = false\) \{.*?\n    \}", self.source, re.S)
        refresh = re.search(r"async function refreshAll\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(nav_items)
        self.assertIsNotNone(current_view)
        self.assertIsNotNone(should_fetch_jobs)
        self.assertIsNotNone(dashboard_jobs)
        self.assertIsNotNone(refresh)
        refresh_source = refresh.group(0)
        page_loader_source = page_loader.group(0) if page_loader else "function loadPageData() { return null; }"
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")

        script = (
            '"use strict";\n'
            + nav_items.group(0)
            + "\n"
            + current_view.group(0)
            + "\n"
            + should_fetch_jobs.group(0)
            + "\n"
            + dashboard_jobs.group(0)
            + "\n"
            + page_loader_source
            + "\n"
            + refresh.group(0)
            + r'''
const calls = [];
const state = {
  currentUser: "admin", currentRole: "admin", currentView: "dashboard",
  workers: [], targets: [], jobs: [], jobStats: null, storageProfiles: [],
  retentionPolicies: [], secrets: [], localUsers: [], settings: {},
  schedulerTimezone: null, workerInventories: {}, pageDataLoaded: {}, pageDataPending: {},
};
const window = { location: { hash: "#/dashboard" } };
const browserTimezoneOffsetMinutes = () => 0;
const apiGet = async path => {
  calls.push(path);
  if (path === "/api/v1/workers" || path === "/api/v1/targets" || path === "/api/v1/config/public") return { items: [], scheduler_timezone: "America/Bogota" };
  if (path === "/api/v1/version") return { version: "test" };
  if (path === "/api/v1/jobs?limit=5") return { items: [], total: 0 };
  if (path.startsWith("/api/v1/jobs/stats?")) return { total: 0, by_status: {}, by_day: [] };
  return { items: [] };
};
const applyDashboardJobs = () => {};
const renderVersionBanner = () => {};
const route = () => {};
(async () => {
  await refreshAll();
  const pageOnly = [
    "/api/v1/retention-policies", "/api/v1/secrets",
    "/api/v1/admin/users", "/api/v1/settings", "/api/v1/settings/rclone-remote",
    "/api/v1/version/latest",
  ];
  if (calls.some(path => pageOnly.includes(path))) throw new Error(`home made page-only requests: ${JSON.stringify(calls)}`);
  if (calls.includes("/api/v1/auth/me")) throw new Error(`refreshAll repeated auth: ${JSON.stringify(calls)}`);
  for (const path of ["/api/v1/workers", "/api/v1/targets", "/api/v1/jobs?limit=5", "/api/v1/config/public", "/api/v1/storage-profiles"]) {
    if (!calls.includes(path)) throw new Error(`missing home dependency ${path}: ${JSON.stringify(calls)}`);
  }
  process.stdout.write(JSON.stringify(calls));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("/api/v1/auth/me", refresh_source)
        self.assertNotIn("/api/v1/version/latest", refresh_source)
        self.assertIn("loadPageData(routeView", refresh_source)

    def test_page_data_loader_is_cached_by_view_and_role_aware(self):
        nav_items = re.search(r"const NAV_ITEMS = \[.*?\n    \];", self.source, re.S)
        loader = re.search(r"function loadPageData\(view, force = false\) \{.*?\n    \}", self.source, re.S)
        current_view = re.search(r"function currentRouteView\(\) \{.*?\n    \}", self.source, re.S)
        set_view = re.search(r"function setView\(view\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(nav_items)
        self.assertIsNotNone(loader)
        self.assertIsNotNone(current_view)
        self.assertIsNotNone(set_view)
        self.assertIn("loadPageData(view)", set_view.group(0))
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")

        script = (
            '"use strict";\n'
            + nav_items.group(0)
            + r'''
const calls = [];
const state = {
  currentRole: "admin",
  pageDataLoaded: {},
  pageDataPending: {},
  storageProfiles: [],
  retentionPolicies: [],
  secrets: [],
  localUsers: [],
  settings: {},
  rcloneRemote: null,
};
const window = { location: { hash: "#/dashboard" } };
const apiGet = async path => {
  calls.push(path);
  if (["/api/v1/storage-profiles", "/api/v1/retention-policies", "/api/v1/secrets", "/api/v1/admin/users"].includes(path)) return { items: [{ path }] };
  if (path === "/api/v1/settings/rclone-remote") return { remote_name: "remote" };
  if (path === "/api/v1/settings") return { restic_password_secret_id: "secret-1" };
  return { scheduler_timezone: "America/Bogota" };
};
'''
            + loader.group(0)
            + "\n"
            + current_view.group(0)
            + r'''
(async () => {
  await loadPageData("dashboard");
  if (calls.length !== 0) throw new Error(`dashboard requested page data: ${JSON.stringify(calls)}`);
  await loadPageData("settings");
  const settingsData = ["/api/v1/settings", "/api/v1/settings/rclone-remote", "/api/v1/secrets", "/api/v1/notification-channels"];
  if (JSON.stringify(calls) !== JSON.stringify(settingsData)) throw new Error(`settings did not load only its dependencies: ${JSON.stringify(calls)}`);
  await loadPageData("settings");
  if (calls.length !== 4) throw new Error("cached settings data was fetched again");
  await loadPageData("targets");
  const targetData = [...settingsData, "/api/v1/storage-profiles", "/api/v1/retention-policies"].sort();
  if (JSON.stringify([...calls].sort()) !== JSON.stringify(targetData)) throw new Error(`targets did not load only its dependencies: ${JSON.stringify(calls)}`);
  await loadPageData("targets");
  if (calls.length !== 6) throw new Error("cached target page data was fetched again");
  await loadPageData("secrets");
  if (calls.length !== 6) throw new Error("secrets page ignored the shared in-memory cache");
  await loadPageData("users");
  if (calls[6] !== "/api/v1/admin/users") throw new Error(`users page loaded unrelated data: ${JSON.stringify(calls)}`);
  await loadPageData("users", true);
  if (calls.length !== 8 || calls[7] !== "/api/v1/admin/users") throw new Error("forced page refresh did not reload only the active dataset");
  state.currentRole = "operator";
  state.pageDataLoaded = {};
  state.pageDataPending = {};
  calls.length = 0;
  await loadPageData("targets");
  if (JSON.stringify([...calls].sort()) !== JSON.stringify(["/api/v1/storage-profiles", "/api/v1/retention-policies"].sort())) throw new Error(`operator target page requested admin data: ${JSON.stringify(calls)}`);
  state.currentRole = "viewer";
  state.pageDataLoaded = {};
  state.pageDataPending = {};
  calls.length = 0;
  window.location.hash = "#/secrets";
  if (currentRouteView() !== "dashboard") throw new Error("viewer deep-link was not rejected before rendering");
  await loadPageData("secrets", true);
  if (calls.length !== 0) throw new Error("viewer caused a request to the admin-only secrets API");
  process.stdout.write(JSON.stringify(calls));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class SettingsUiAdjustmentsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.css = UI_CSS_PATH.read_text(encoding="utf-8")
        cls.app_css = UI_APP_CSS_PATH.read_text(encoding="utf-8")

    def test_notifications_tab_hides_settings_save_controls_without_empty_separator(self):
        initializer = re.search(r"function initializeSettingsSubmenus\(root\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(initializer)
        self.assertIn("if (saveActions) saveActions.hidden = hideSave;", initializer.group(0))
        self.assertIn("if (saveButton) saveButton.hidden = hideSave;", initializer.group(0))
        self.assertRegex(
            self.css,
            r"#settingsFormSave\[hidden\]\s*,\s*#settingsFormActions\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;",
        )

    def test_settings_sidebar_link_is_pinned_after_operations_and_before_session(self):
        nav_items = re.search(r"const NAV_ITEMS = \[.*?\n    \];", self.source, re.S)
        renderer = re.search(r"function renderSidebar\(\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(nav_items)
        self.assertIsNotNone(renderer)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = nav_items.group(0) + r'''
const ICONS = {};
const state = { currentRole: "admin", currentView: "settings", systemVersion: "3.15.1" };
const sidebar = { innerHTML: "", querySelector: () => ({ addEventListener() {} }) };
const document = { getElementById: id => id === "sidebar" ? sidebar : null };
const escapeHtml = value => String(value);
''' + renderer.group(0) + r'''
const failures = [];
renderSidebar();
let footerStart = sidebar.innerHTML.indexOf('<div class="footer">');
let operations = sidebar.innerHTML.slice(0, footerStart);
let footer = sidebar.innerHTML.slice(footerStart);
let settingsIndex = footer.indexOf('data-view="settings"');
let sessionIndex = footer.indexOf('<div class="section-label">Sesion</div>');
if (operations.includes('data-view="settings"')) failures.push("Settings remains in the operations list");
if (settingsIndex < 0 || settingsIndex > sessionIndex) failures.push("Settings is not placed before the Session section in the footer");
if (!footer.includes('<a class="item active" href="#/settings" data-view="settings">')) failures.push("Settings lost its active state in the footer");
state.currentRole = "viewer";
renderSidebar();
if (sidebar.innerHTML.includes('data-view="settings"')) failures.push("non-admin role sees the Settings link");
if (failures.length) throw new Error(failures.join("; "));
process.stdout.write("sidebar Settings placement passed");
'''
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "sidebar Settings placement passed")
        self.assertRegex(self.app_css, r"\.sidebar \.footer\s*\{\s*margin-top:\s*auto;")


if __name__ == "__main__":
    unittest.main()
