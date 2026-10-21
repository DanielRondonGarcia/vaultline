import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "index.html"
UI_CSS_PATH = Path(__file__).resolve().parents[1] / "src" / "control_plane" / "ui" / "styles" / "components.css"
NOTIFICATION_GUIDE_PATH = Path(__file__).resolve().parents[1] / "doc" / "backup-failure-notifications.md"


CHANNEL_MODAL_MOCK_PRELUDE = r'''
// Browser-like harness for the notification channel create/edit modal.
const escapeHtml = value => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;");
const state = {
  notificationChannels: [
    { id: "smtp-1", name: "Sistemas", channel_type: "smtp", enabled: true,
      config_summary: { host: "mail.internal", port: 587, from: "backup@example.test", to: ["oncall@example.test", "admin@example.test"], security: "starttls", authentication_configured: true } },
    { id: "webhook-1", name: "Avisos", channel_type: "webhook", enabled: true,
      config_summary: { destination: "https://hooks.example.test" } },
  ],
  notificationChannelEditor: null,
  notificationChannelFeedback: null,
  pageDataLoaded: {},
};
const toNodeList = values => {
  const list = { length: values.length };
  list.forEach = callback => { for (let index = 0; index < list.length; index += 1) callback(list[index], index, list); };
  list.item = index => (index >= 0 && index < list.length) ? list[index] : null;
  for (let index = 0; index < values.length; index += 1) list[index] = values[index];
  return list;
};
let markup = "";
const events = {};
const apiCalls = [];
const focusLog = [];
let showCalls = 0;
let closeCalls = 0;
const fieldValues = {};
const errorElement = { hidden: true, textContent: "" };
const submitButton = { disabled: false, textContent: "" };
const nameInput = { value: "", focus() { focusLog.push("name"); } };
const webhookUrlInput = { value: "", focus() { focusLog.push("webhookUrl"); } };
const smtpUsernameInput = { value: "", focus() { focusLog.push("smtpUsername"); } };
const makeNode = (label, extra) => Object.assign({
  focus() { focusLog.push(label); },
  addEventListener(name, handler) { events[`${label}:${name}`] = handler; },
}, extra || {});
const addButton = makeNode("add");
const editButton = makeNode("edit", { dataset: { notificationEdit: "smtp-1" } });
const cancelButton = makeNode("cancel");
const exitButton = makeNode("x");
const formStub = {
  addEventListener(name, handler) { events[name] = handler; },
  querySelector(selector) {
    if (selector === 'button[type="submit"]') return submitButton;
    if (selector === "#notificationChannelName") return nameInput;
    if (selector === "#notificationWebhookUrl") return webhookUrlInput;
    if (selector === "#notificationSmtpUsername") return smtpUsernameInput;
    return null;
  },
  querySelectorAll() { return toNodeList([]); },
};
const dialogStub = {
  open: false,
  showModal() { this.open = true; showCalls += 1; },
  close() { this.open = false; closeCalls += 1; },
  addEventListener(name, handler) { events[`dialog:${name}`] = handler; },
  querySelector(selector) { return selector === "[data-notification-channel-error]" ? errorElement : null; },
};
const typeSelect = { value: "webhook" };
typeSelect.addEventListener = (name, handler) => { events[`type:${name}`] = handler; };
const selectorMap = {
  "#addNotificationChannel": addButton,
  "#cancelNotificationChannelEdit": cancelButton,
  "#closeNotificationChannelEdit": exitButton,
  "#notificationChannelForm": formStub,
  "#notificationChannelDialog": dialogStub,
  "#notificationChannelType": typeSelect,
  "#notificationChannelName": nameInput,
  "[data-notification-channel-error]": errorElement,
  '[data-notification-edit="smtp-1"]': editButton,
};
const container = {
  set innerHTML(value) {
    // Replacing the container subtree discards the previously open dialog and recreates its controls.
    dialogStub.open = false;
    submitButton.disabled = false;
    errorElement.hidden = true;
    errorElement.textContent = "";
    markup = value;
  },
  querySelector(selector) { return selectorMap[selector] || null; },
  querySelectorAll(selector) {
    if (selector === "[data-notification-edit]") return toNodeList([editButton]);
    return toNodeList([]);
  },
};
const document = { getElementById(id) { return id === "notificationChannelManager" ? container : null; } };
let getImpl = async () => ({ items: [] });
let postImpl = async (path, body) => { apiCalls.push({ method: "POST", path, body }); return {}; };
let patchImpl = async (path, body) => { apiCalls.push({ method: "PATCH", path, body }); return {}; };
const apiGet = async path => { apiCalls.push({ method: "GET", path }); return getImpl(path); };
const apiPost = (path, body) => postImpl(path, body);
const apiPatch = (path, body) => patchImpl(path, body);
const apiDelete = async () => ({});
function FormDataShim() {
  const snapshot = Object.assign({}, fieldValues);
  this.get = name => Object.prototype.hasOwnProperty.call(snapshot, name) ? String(snapshot[name]) : "";
  this.getAll = () => [];
}
globalThis.FormData = FormDataShim;
const fail = message => { throw new Error(message); };
const postCalls = () => apiCalls.filter(call => call.method === "POST");
'''


class NotificationChannelModalUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")
        cls.css = UI_CSS_PATH.read_text(encoding="utf-8")

    def _run_modal_node_check(self, assertions, expected_stdout):
        renderer = re.search(r"function renderNotificationChannelManager\(.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(renderer)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")
        script = CHANNEL_MODAL_MOCK_PRELUDE + renderer.group(0) + assertions
        with tempfile.TemporaryDirectory(prefix="notification-channel-modal-check-") as tmp:
            script_path = Path(tmp) / "channel_modal_check.js"
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

    def test_create_editor_is_a_centered_modal_outside_the_grid_without_inline_card(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  if (showCalls !== 0) fail("showModal must not run without an editor state");
  if (markup.includes("notificationChannelDialog")) fail("no editor dialog may render without an editor state");
  events["add:click"]();
  if (showCalls !== 1) fail("create must open its native modal exactly once");
  if (markup.includes("notification-channel-editor")) fail("the editor card must not render inline in the settings grid");
  const required = [
    '<dialog id="notificationChannelDialog" class="modal notification-channel-modal"',
    'aria-labelledby="notificationChannelTitle"',
    'id="notificationChannelTitle">Agregar canal</h3>',
    'Las credenciales se cifran antes de guardarse.',
    '<button type="button" class="close" id="closeNotificationChannelEdit" aria-label="Cerrar">',
    '<form id="notificationChannelForm"',
    'id="cancelNotificationChannelEdit">Cancelar</button>',
    '<button type="submit" class="btn primary">Crear canal</button>',
    'data-notification-channel-error role="alert"',
    '<input id="notificationChannelName"',
  ];
  const missing = required.filter(value => !markup.includes(value));
  if (missing.length) fail("missing create modal markup: " + missing.join("; "));
  const sectionEnd = markup.indexOf("</section>");
  const dialogStart = markup.indexOf('<dialog id="notificationChannelDialog"');
  if (sectionEnd === -1 || dialogStart === -1 || sectionEnd > dialogStart) fail("editor dialog must be attached outside the settings grid");
  const formStart = markup.indexOf('<form id="notificationChannelForm"');
  const submitAt = markup.indexOf('<button type="submit"', formStart);
  const formEnd = markup.indexOf("</form>", formStart);
  if (formStart === -1 || submitAt === -1 || formEnd === -1 || submitAt > formEnd) fail("the submit action must live inside the form footer");
  if (!/<input id="notificationChannelName"[^>]*required/.test(markup)) fail("channel name must stay required");
  process.stdout.write("create modal ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "create modal ok",
        )

    def test_edit_editor_modal_prefills_safe_values_never_secrets_and_disables_type(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["edit:click"]();
  if (showCalls !== 1) fail("edit must open its native modal");
  const required = [
    'id="notificationChannelTitle">Editar canal</h3>',
    "Los campos secretos no se rellenan ni se vuelven a mostrar.",
    '<button type="submit" class="btn primary">Guardar cambios</button>',
    'id="cancelNotificationChannelEdit">Cancelar</button>',
    '<select id="notificationChannelType" name="channel_type" disabled',
    'value="mail.internal"',
    'value="587"',
    'value="backup@example.test"',
    'value="oncall@example.test, admin@example.test"',
    "Dejar vacío para conservar",
  ];
  const missing = required.filter(value => !markup.includes(value));
  if (missing.length) fail("missing edit modal markup: " + missing.join("; "));
  const passwordTag = markup.match(/<input id="notificationSmtpPassword"[^>]*>/);
  if (!passwordTag || passwordTag[0].includes("value=")) fail("SMTP password must never be rendered back");
  const usernameTag = markup.match(/<input id="notificationSmtpUsername"[^>]*>/);
  if (!usernameTag || usernameTag[0].includes("value=")) fail("SMTP username must never be rendered back");
  if (markup.includes("notification-channel-editor")) fail("edit must not fall back to the inline card");
  process.stdout.write("edit modal ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "edit modal ok",
        )

    def test_cancel_close_and_escape_discard_state_and_restore_trigger_focus(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["add:click"]();
  events["cancel:click"]();
  if (closeCalls !== 1) fail("cancel must request the dialog close");
  events["dialog:close"]();
  if (state.notificationChannelEditor !== null) fail("cancel must discard the editor state");
  if (markup.includes("notificationChannelDialog")) fail("editor dialog must not render after cancel");
  if (focusLog.length !== 1 || focusLog[0] !== "add") fail("cancel must restore focus to the add trigger; got " + JSON.stringify(focusLog));
  events["edit:click"]();
  events["dialog:close"]();
  if (focusLog[1] !== "edit") fail("edit close must restore focus to the edit trigger; got " + JSON.stringify(focusLog));
  events["add:click"]();
  events["x:click"]();
  if (closeCalls !== 2) fail("header X must also request the dialog close");
  events["dialog:close"]();
  if (focusLog[2] !== "add") fail("X close must restore the add trigger focus; got " + JSON.stringify(focusLog));
  events["add:click"]();
  const prevented = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  events["dialog:cancel"](prevented);
  if (prevented.defaultPrevented) fail("escape must stay free to close while idle");
  events["dialog:close"]();
  if (state.notificationChannelEditor !== null) fail("escape-close must discard the editor state");
  process.stdout.write("cancel close escape ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "cancel close escape ok",
        )

    def test_create_type_switch_preserves_name_and_keeps_the_dialog_open(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["add:click"]();
  nameInput.value = "NOC";
  typeSelect.value = "smtp";
  events["type:change"]();
  if (state.notificationChannelEditor.channelType !== "smtp") fail("type switch must update the editor channel type");
  if (state.notificationChannelEditor.name !== "NOC") fail("type switch must preserve the entered name");
  if (showCalls !== 2) fail("type switch must keep the dialog open through the rerender");
  if (!markup.includes("Servidor SMTP")) fail("type switch must render the SMTP fields in the still-open dialog");
  process.stdout.write("type switch ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "type switch ok",
        )

    def test_create_submit_posts_exact_payload_finally_closes_and_restores_focus(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["add:click"]();
  Object.assign(fieldValues, { name: "NOC", url: "https://hooks.example.test/backup" });
  await events["submit"]({ preventDefault() {} });
  const posts = postCalls();
  if (posts.length !== 1 || posts[0].path !== "/api/v1/notification-channels") fail("wrong create request: " + JSON.stringify(apiCalls));
  if (JSON.stringify(posts[0].body) !== JSON.stringify({ name: "NOC", channel_type: "webhook", config: { url: "https://hooks.example.test/backup" } })) fail("create payload mismatch: " + JSON.stringify(posts[0].body));
  if (apiCalls.filter(call => call.method === "GET").length !== 1) fail("successful save must reload the channels once");
  if (state.notificationChannelEditor !== null) fail("successful create must close the editor");
  if (state.notificationChannelFeedback?.message !== "Canal creado." || state.notificationChannelFeedback?.type !== "success") fail("success must reuse the background feedback chip");
  if (markup.includes("notificationChannelDialog")) fail("editor dialog must not render after save");
  if (!markup.includes("notification-feedback is-success")) fail("background success chip must be visible in markup");
  if (focusLog.length !== 1 || focusLog[0] !== "add") fail("successful save must restore trigger focus; got " + JSON.stringify(focusLog));
  process.stdout.write("create submit ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "create submit ok",
        )

    def test_webhook_validation_error_is_visible_in_modal_and_blocks_the_post(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["add:click"]();
  fieldValues.name = "NOC";
  fieldValues.url = "";
  await events["submit"]({ preventDefault() {} });
  if (apiCalls.length) fail("validation failure must not call the API");
  if (errorElement.hidden || errorElement.textContent !== "La URL del webhook es obligatoria.") fail("error must be visible in the modal alert; got " + JSON.stringify(errorElement));
  if (!dialogStub.open) fail("validation failure must keep the modal open");
  if (!focusLog.includes("webhookUrl")) fail("validation failure must focus the invalid field; got " + JSON.stringify(focusLog));
  if (submitButton.disabled) fail("validation failure must not disable the submit button");
  process.stdout.write("create validation ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "create validation ok",
        )

    def test_smtp_pairing_blocks_in_modal_then_sends_single_complete_payload(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["add:click"]();
  typeSelect.value = "smtp";
  events["type:change"]();
  Object.assign(fieldValues, {
    name: "Correo",
    host: "smtp.example.test",
    port: "587",
    from: "backup@example.test",
    to: "oncall@example.test, admin@example.test",
    security: "starttls",
    username: "usuario",
    password: "",
  });
  await events["submit"]({ preventDefault() {} });
  if (apiCalls.length) fail("user/password pairing error must not call the API");
  if (errorElement.hidden || errorElement.textContent !== "Para cambiar la autenticación SMTP, indica usuario y contraseña.") fail("pairing error must be visible in the modal alert; got " + errorElement.textContent);
  if (!dialogStub.open) fail("pairing error must keep the modal open");
  if (!focusLog.includes("smtpUsername")) fail("pairing error must focus the credentials field; got " + JSON.stringify(focusLog));
  fieldValues.password = "secreta";
  await events["submit"]({ preventDefault() {} });
  const posts = postCalls();
  if (posts.length !== 1) fail("retry after fixing credentials must send exactly one create; got " + JSON.stringify(apiCalls));
  const body = posts[0].body;
  if (body.channel_type !== "smtp" || body.name !== "Correo") fail("smtp create payload header mismatch: " + JSON.stringify(body));
  if (body.config.host !== "smtp.example.test" || body.config.port !== 587 || body.config.security !== "starttls") fail("smtp create payload config mismatch: " + JSON.stringify(body.config));
  if (body.config.username !== "usuario" || body.config.password !== "secreta") fail("paired credentials must be sent together");
  if (state.notificationChannelEditor !== null) fail("smtp create success must close the editor");
  process.stdout.write("smtp credentials rule ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "smtp credentials rule ok",
        )

    def test_server_error_is_visible_in_modal_and_preserves_the_editor(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  patchImpl = async (path, body) => { apiCalls.push({ method: "PATCH", path, body }); throw new Error("boom"); };
  events["edit:click"]();
  fieldValues.name = "Sistemas";
  await events["submit"]({ preventDefault() {} });
  const patches = apiCalls.filter(call => call.method === "PATCH");
  if (patches.length !== 1 || patches[0].path !== "/api/v1/notification-channels/smtp-1") fail("wrong edit request: " + JSON.stringify(apiCalls));
  if (patches[0].body.name !== "Sistemas") fail("edit must keep the channel name");
  if (errorElement.hidden || !errorElement.textContent.includes("boom")) fail("server error must be visible in the modal alert");
  if (!dialogStub.open) fail("server error must keep the modal open");
  if (!state.notificationChannelEditor) fail("server error must preserve the editor state");
  if (submitButton.disabled) fail("server error must re-enable the submit button");
  process.stdout.write("server error ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "server error ok",
        )

    def test_quick_double_submit_sends_a_single_save_request(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  events["add:click"]();
  Object.assign(fieldValues, { name: "NOC", url: "https://hooks.example.test/backup" });
  const first = events["submit"]({ preventDefault() {} });
  await events["submit"]({ preventDefault() {} });
  await first;
  if (postCalls().length !== 1) fail("double submit must not duplicate the create; got " + JSON.stringify(apiCalls));
  if (apiCalls.filter(call => call.method === "GET").length !== 1) fail("double submit must reload channels exactly once");
  if (state.notificationChannelEditor !== null) fail("editor must close after the single save");
  if (submitButton.disabled) fail("submit button must be re-enabled");
  process.stdout.write("double submit ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "double submit ok",
        )

    def test_close_is_blocked_while_saving_and_success_completes_exactly_once(self):
        self._run_modal_node_check(
            r'''
(async () => {
  renderNotificationChannelManager();
  let release;
  postImpl = async (path, body) => {
    apiCalls.push({ method: "POST", path, body });
    return new Promise(resolve => { release = () => resolve({}); });
  };
  events["add:click"]();
  Object.assign(fieldValues, { name: "NOC", url: "https://hooks.example.test/backup" });
  const pending = events["submit"]({ preventDefault() {} });
  events["cancel:click"]();
  if (closeCalls !== 0) fail("cancel must be blocked while a save is pending");
  const prevented = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  events["dialog:cancel"](prevented);
  if (!prevented.defaultPrevented) fail("escape must be blocked while a save is pending");
  release();
  await pending;
  if (postCalls().length !== 1) fail("release must complete exactly one save");
  if (apiCalls.filter(call => call.method === "GET").length !== 1) fail("successful save must reload channels once");
  if (state.notificationChannelEditor !== null) fail("successful save must still close the editor");
  if (!state.notificationChannelFeedback?.message) fail("successful save must publish the feedback chip");
  process.stdout.write("pending close guard ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
''',
            "pending close guard ok",
        )

    def test_channel_modal_styles_and_docs_are_scoped_and_documented(self):
        for marker in (
            ".notification-channel-modal {",
            ".notification-channel-modal:not([open]) { display: none; }",
            ".notification-channel-modal::backdrop",
            ".notification-channel-modal .modal-body",
            "max-height: min(",
            "overflow-y: auto",
            ".notification-channel-modal .modal-footer",
        ):
            self.assertIn(marker, self.css)
        self.assertIn(".notification-channel-modal .form-grid { grid-template-columns: 1fr", self.css)
        doc = NOTIFICATION_GUIDE_PATH.read_text(encoding="utf-8")
        self.assertIn("ventana modal", doc)
        self.assertIn("Esc", doc)