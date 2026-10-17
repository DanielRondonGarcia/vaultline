import io
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.control_plane.auth import AuthService, AuthUser, ROLE_ADMIN, ROLE_VIEWER
from src.control_plane.main import ControlPlaneRequestHandler


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "src" / "control_plane" / "ui" / "index.html"


class UserPasswordResetRouteTests(unittest.TestCase):
    @staticmethod
    def make_handler(path, body, service, authorized=True):
        handler = object.__new__(ControlPlaneRequestHandler)
        handler.path = path
        raw_body = body.encode("utf-8")
        handler.headers = {"Content-Length": str(len(raw_body))}
        handler.rfile = io.BytesIO(raw_body)
        role = ROLE_ADMIN if authorized else ROLE_VIEWER
        handler._current_session = Mock(
            return_value={"username": "admin1" if authorized else "viewer1", "role": role, "must_change_password": False}
        )
        handler._auth_service = Mock(return_value=service)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        return handler

    def make_service(self):
        service = AuthService(
            users={
                "viewer1": AuthUser(
                    username="viewer1",
                    password="InitialPassword2026",
                    role=ROLE_VIEWER,
                    must_change_password=False,
                    password_scheme="plain",
                )
            },
            session_secret=b"test-session-secret",
            users_file_path=Path("in-memory-users.json"),
        )
        service._save_users_file = Mock()
        return service

    def test_reset_route_preserves_percent_and_dollar_password_characters(self):
        for new_password in (
            "Synthetic%Password2026",
            "Synthetic$Password2026",
            "Synthetic%$Password2026",
        ):
            with self.subTest(new_password=new_password):
                service = self.make_service()
                handler = self.make_handler(
                    "/api/v1/admin/users/viewer1/reset-password",
                    json.dumps({"new_password": new_password}),
                    service,
                )

                handler.do_POST()

                handler.send_response.assert_called_once_with(200)
                self.assertIsNotNone(service.authenticate("viewer1", new_password))
                self.assertIsNone(service.authenticate("viewer1", "InitialPassword2026"))
                user = service.get_user("viewer1")
                self.assertEqual(user.role, ROLE_VIEWER)
                self.assertTrue(user.must_change_password)
                response_body = json.loads(handler.wfile.getvalue().decode("utf-8"))
                self.assertEqual(response_body["user"]["role"], ROLE_VIEWER)
                self.assertTrue(response_body["user"]["must_change_password"])

    def test_reset_route_does_not_change_password_without_admin_authorization(self):
        service = self.make_service()
        handler = self.make_handler(
            "/api/v1/admin/users/viewer1/reset-password",
            json.dumps({"new_password": "ReplacementPassword2026"}),
            service,
            authorized=False,
        )

        handler.do_POST()

        handler.send_response.assert_called_once_with(403)
        self.assertEqual(json.loads(handler.wfile.getvalue().decode("utf-8")), {"error": "forbidden"})
        self.assertIsNotNone(service.authenticate("viewer1", "InitialPassword2026"))
        self.assertIsNone(service.authenticate("viewer1", "ReplacementPassword2026"))


class UserPasswordResetUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = UI_PATH.read_text(encoding="utf-8")

    def test_users_view_offers_safe_password_reset_for_manageable_non_admin_users(self):
        loader = re.search(r"function loadPageData\(view, force = false\) \{.*?\n    \}", self.source, re.S)
        renderer = re.search(r"function renderUsers\(content\) \{.*?\n    \}", self.source, re.S)
        self.assertIsNotNone(loader)
        self.assertIsNotNone(renderer)
        if not shutil.which("node"):
            self.skipTest("Node.js is not installed")

        script = (
            '"use strict";\n'
            + r"""
const state = {
  currentRole: "admin",
  localUsers: [],
  userManagement: null,
  pageDataLoaded: {},
  pageDataPending: {},
};
const apiCalls = [];
const apiRequests = [];
const passwordCalls = [];
const response = {
  items: [
    { username: "viewer1", role: "viewer", must_change_password: false, created_at: "2026-01-01" },
    { username: "operator1", role: "operator", must_change_password: true, created_at: "2026-01-01" },
    { username: "admin1", role: "admin", must_change_password: false, created_at: "2026-01-01" },
  ],
  management: { manageable: true, source: "file" },
};
const apiGet = async path => { apiCalls.push(path); return response; };
const apiPost = async (path, body) => {
  apiRequests.push({ path, body });
  if (path.endsWith("/reset-password")) passwordCalls.push({ path, body });
  return { ok: true };
};
const escapeHtml = value => String(value);
const fmtDate = value => String(value);
const withBtnLoading = async (_button, action) => action();
const createValues = {
  usrName: { value: "synthetic-user" },
  usrPassword: { value: "Synthetic%$Create2026" },
  usrRole: { value: "viewer" },
};
let createHandler = null;
const createForm = { addEventListener: (event, handler) => { if (event === "submit") createHandler = handler; } };
let content;
const document = {
  getElementById: id => id === "createUserForm"
    ? (content.innerHTML.includes('id="createUserForm"') ? createForm : null)
    : (createValues[id] || null),
};
let refreshAllCalls = 0;
const refreshAll = async () => { refreshAllCalls += 1; };
let submitHandler = null;
const resetForm = {
  dataset: { resetPasswordUser: "viewer1" },
  elements: { new_password: { value: "Synthetic%$Password2026" } },
  addEventListener: (event, handler) => { if (event === "submit") submitHandler = handler; },
};
content = {
  innerHTML: "",
  querySelectorAll: selector => selector === 'form[data-reset-password-user]' && content.innerHTML.includes('data-reset-password-user=') ? [resetForm] : [],
};
let setViewCalls = 0;
const setView = view => { if (view === "users") setViewCalls += 1; };
"""
            + loader.group(0)
            + "\n"
            + renderer.group(0)
            + r"""
(async () => {
  await loadPageData("users");
  const failures = [];
  if (JSON.stringify(state.userManagement) !== JSON.stringify(response.management)) failures.push("users loader discarded management metadata");
  renderUsers(content);
  if (!content.innerHTML.includes('data-reset-password-user="viewer1"')) failures.push("manageable non-admin row has no reset action");
  if (!content.innerHTML.includes('data-reset-password-user="operator1"')) failures.push("operator row has no reset action");
  if (content.innerHTML.includes('data-reset-password-user="admin1"')) failures.push("admin row received a non-admin reset action");
  if (!content.innerHTML.includes('type="password"')) failures.push("reset form does not mask the new password");
  if (!content.innerHTML.includes("Restablecer contraseña")) failures.push("reset action is not clearly labeled");
  if (content.innerHTML.includes("Synthetic%$Password2026")) failures.push("password was rendered into the page markup");
  if (!submitHandler) failures.push("reset form has no submit handler");
  if (submitHandler) await submitHandler({ preventDefault() {}, submitter: {} });
  if (passwordCalls.length !== 1) failures.push(`expected one reset request, got ${passwordCalls.length}`);
  if (passwordCalls[0]?.path !== "/api/v1/admin/users/viewer1/reset-password") failures.push(`wrong reset endpoint: ${JSON.stringify(passwordCalls)}`);
  if (passwordCalls[0]?.body?.new_password !== "Synthetic%$Password2026") failures.push("reset request changed the literal password characters");
  if (passwordCalls[0]?.body && Object.prototype.hasOwnProperty.call(passwordCalls[0].body, "role")) failures.push("reset request unexpectedly edits the user's role");
  if (apiCalls.filter(path => path === "/api/v1/admin/users").length !== 2) failures.push("successful reset did not refresh the user list");
  if (setViewCalls !== 1) failures.push("successful reset did not rerender the Users view");
  if (!content.innerHTML.includes('id="createUserForm"')) failures.push("existing admin create-user form disappeared");
  if (!createHandler) failures.push("existing create-user form lost its submit handler");
  if (createHandler) await createHandler({ preventDefault() {}, submitter: {} });
  const createRequests = apiRequests.filter(request => request.path === "/api/v1/admin/users");
  if (createRequests.length !== 1) failures.push("existing create-user endpoint was not called exactly once");
  if (JSON.stringify(createRequests[0]?.body) !== JSON.stringify({ username: "synthetic-user", password: "Synthetic%$Create2026", role: "viewer" })) failures.push("existing create-user payload changed");
  if (refreshAllCalls !== 1) failures.push("existing create-user flow no longer refreshes the page data");

  state.userManagement = { manageable: false, source: "environment" };
  renderUsers(content);
  if (content.innerHTML.includes("data-reset-password-user=")) failures.push("environment-backed users showed an unavailable reset action");
  state.userManagement = { manageable: true, source: "file" };
  state.currentRole = "viewer";
  renderUsers(content);
  if (content.innerHTML.includes("data-reset-password-user=")) failures.push("non-admin viewer saw a password-reset action");
  if (failures.length) throw new Error(failures.join("; "));
  process.stdout.write("password reset UI behavior passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        )
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "password reset UI behavior passed")


if __name__ == "__main__":
    unittest.main()
