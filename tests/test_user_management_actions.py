import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.control_plane.auth import AuthService
from src.control_plane.main import ControlPlaneRequestHandler


class UserManagementRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.users_path = Path(self.temp.name) / "users.json"
        users = [
            {"username": name, "password": "OriginalPassword2026", "role": role}
            for name, role in (("admin1", "admin"), ("admin2", "admin"),
                               ("operator1", "operator"), ("viewer1", "viewer"))
        ]
        self.users_path.write_text(json.dumps(users), encoding="utf-8")
        self.environment = {"CONTROL_PLANE_USERS_FILE": str(self.users_path),
                            "CONTROL_PLANE_USERS_JSON": "", "CONTROL_PLANE_SESSION_KEY": "test-key"}
        self.service = self.reload_service()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ControlPlaneRequestHandler)
        self.server.application = SimpleNamespace(auth_service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.cookies = {name: "cp_session=" + self.service.issue_session_token(self.service.get_user(name))
                        for name in ("admin1", "admin2", "operator1", "viewer1")}

    def reload_service(self):
        with patch.dict(os.environ, self.environment):
            return AuthService.from_runtime()

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, actor="admin1"):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["Cookie"] = self.cookies[actor]
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, json.loads(raw) if raw else None
        connection.close()
        return result

    def listed_users(self):
        status, payload = self.request("GET", "/api/v1/admin/users")
        self.assertEqual(status, 200)
        return payload["items"]

    def test_delete_removes_other_user_persistently_and_revokes_session(self):
        self.assertEqual(self.request("DELETE", "/api/v1/admin/users/operator1"), (204, None))
        self.assertNotIn("operator1", [user["username"] for user in self.listed_users()])
        self.assertEqual(self.request("GET", "/api/v1/auth/me", actor="operator1"),
                         (401, {"error": "authentication required"}))
        self.server.application.auth_service = self.reload_service()
        self.assertNotIn("operator1", [user["username"] for user in self.listed_users()])

    def test_change_role_preserves_password_and_updates_existing_session(self):
        status, payload = self.request("POST", "/api/v1/admin/users/admin2/update", {"role": "viewer"})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "user": {"username": "admin2", "role": "viewer",
                         "must_change_password": False, "password_scheme": "plain"}})
        self.assertEqual(self.request("GET", "/api/v1/admin/users", actor="admin2"),
                         (403, {"error": "forbidden"}))
        self.server.application.auth_service = self.reload_service()
        status, session = self.request("GET", "/api/v1/auth/me", actor="admin2")
        self.assertEqual(status, 200)
        self.assertEqual(session["role"], "viewer")
        status, _ = self.request("POST", "/api/v1/auth/login",
                                 {"username": "admin2", "password": "OriginalPassword2026"}, actor=None)
        self.assertEqual(status, 200)

    def test_own_account_and_unauthorized_actions_leave_users_unchanged(self):
        before = self.listed_users()
        cases = [
            ("DELETE", "admin1", None, "admin1", 403, "cannot delete your own account"),
            ("POST", "admin1/update", {"role": "viewer"}, "admin1", 403, "cannot change your own role"),
        ]
        for actor in ("operator1", "viewer1", None):
            status = 403 if actor else 401
            error = "forbidden" if actor else "authentication required"
            cases.extend([("DELETE", "admin2", None, actor, status, error),
                          ("POST", "admin2/update", {"role": "viewer"}, actor, status, error)])
        for method, target, body, actor, status, error in cases:
            with self.subTest(method=method, target=target, actor=actor):
                self.assertEqual(self.request(method, "/api/v1/admin/users/" + target, body, actor),
                                 (status, {"error": error}))
                self.assertEqual(self.listed_users(), before)
                self.server.application.auth_service = self.reload_service()
                self.assertEqual(self.listed_users(), before)

    def test_invalid_or_missing_user_changes_do_not_mutate_users(self):
        before = self.listed_users()
        for method, target, body, status, error in (
            ("DELETE", "missing", None, 404, "user not found"),
            ("POST", "viewer1/update", {"role": "superuser"}, 400, "invalid role"),
            ("POST", "missing/update", {"role": "viewer"}, 400, "user not found"),
        ):
            with self.subTest(method=method, target=target):
                self.assertEqual(self.request(method, "/api/v1/admin/users/" + target, body),
                                 (status, {"error": error}))
                self.assertEqual(self.listed_users(), before)

    def test_environment_backed_users_remain_read_only(self):
        self.server.application.auth_service.users_file_path = None
        before = self.listed_users()
        message = "local user administration requires CONTROL_PLANE_USERS_FILE and is disabled for environment-backed users"
        for method, target, body in (("DELETE", "viewer1", None),
                                     ("POST", "viewer1/update", {"role": "operator"})):
            self.assertEqual(self.request(method, "/api/v1/admin/users/" + target, body),
                             (400, {"error": message}))
            self.assertEqual(self.listed_users(), before)

    def test_existing_update_password_flag_on_self_still_works(self):
        status, payload = self.request("POST", "/api/v1/admin/users/admin1/update",
                                       {"role": "admin", "must_change_password": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["user"]["role"], "admin")
        self.assertTrue(payload["user"]["must_change_password"])


class UserManagementUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_role_and_delete_actions_and_safety(self):
        source = (Path(__file__).resolve().parents[1] / "src/control_plane/ui/index.html").read_text(encoding="utf-8")
        renderer = re.search(r"function renderUsers\(content\) \{.*?\n    \}", source, re.S).group(0)
        script = r'''
const assert = require("node:assert/strict");
const state = {currentRole:"admin",currentUser:"admin1",userManagement:{manageable:true},localUsers:[
  {username:"admin1",role:"admin"},{username:"admin2",role:"admin"},{username:"operator1",role:"operator"}]};
const escapeHtml = x => String(x), fmtDate = () => "-";
let confirmResult = false, refreshes = 0, renders = 0;
const calls = [], handlers = {}, dialogs = {};
const confirm = () => confirmResult;
const apiPost = async (path, body) => calls.push({path,body});
const apiDelete = async path => calls.push({path});
const loadPageData = async (view, force) => {assert.equal(view,"users");assert.equal(force,true);refreshes++;};
const setView = view => {assert.equal(view,"users");renders++;};
const withBtnLoading = async (btn, action) => action();
const form = {dataset:{changeRoleUser:"admin2",roleDialogId:"changeRoleDialog1"},elements:{role:{value:"viewer"}},
  addEventListener:(event, fn)=>handlers.role=fn};
const button = {dataset:{deleteUser:"operator1"},addEventListener:(event,fn)=>handlers.delete=fn};
const content = {innerHTML:"",querySelectorAll:selector=>{
  if(selector==='form[data-change-role-user]') return [form];
  if(selector==='button[data-delete-user]') return [button];
  if(selector==='button[data-open-role-dialog]') return [{dataset:{openRoleDialog:"changeRoleDialog1"},addEventListener:(e,fn)=>handlers.open=fn}];
  if(selector==='button[data-close-role-dialog]') return [{dataset:{closeRoleDialog:"changeRoleDialog1"},addEventListener:(e,fn)=>handlers.close=fn}];
  return [];
}};
dialogs.changeRoleDialog1={open:false,showModal(){this.open=true;},close(){this.open=false;}};
const document={getElementById:id=>dialogs[id]||null};
''' + renderer + r'''
(async()=>{
  renderUsers(content);
  assert(content.innerHTML.includes('data-change-role-user="admin2"'));
  assert(content.innerHTML.includes('data-delete-user="admin2"'));
  assert(!content.innerHTML.includes('data-change-role-user="admin1"'));
  assert(!content.innerHTML.includes('data-delete-user="admin1"'));
  assert(content.innerHTML.includes('value="admin" selected'));
  handlers.open(); assert(dialogs.changeRoleDialog1.open);
  handlers.close(); assert(!dialogs.changeRoleDialog1.open);
  await handlers.role({preventDefault(){},submitter:{}});
  assert.deepEqual(calls,[{path:"/api/v1/admin/users/admin2/update",body:{role:"viewer"}}]);
  await handlers.delete({currentTarget:button}); assert.equal(calls.length,1);
  confirmResult=true; await handlers.delete({currentTarget:button});
  assert.deepEqual(calls[1],{path:"/api/v1/admin/users/operator1"});
  assert.equal(refreshes,2); assert.equal(renders,2);
  state.userManagement.manageable=false; renderUsers(content);
  assert(!content.innerHTML.includes("data-delete-user="));
  assert(!content.innerHTML.includes("data-change-role-user="));
  state.userManagement.manageable=true;
  for(const role of ["viewer","operator"]){state.currentRole=role;renderUsers(content);
    assert(!content.innerHTML.includes("data-delete-user="));assert(!content.innerHTML.includes("data-change-role-user="));}
  process.stdout.write("user management UI passed");
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(["node", "--input-type=commonjs", "--eval", script], capture_output=True,
                                text=True, encoding="utf-8", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "user management UI passed")
        self.assertEqual(result.stderr, "")
