import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


WXS = Path("packaging/windows/vaultline-worker.wxs")
WIX_NS = "http://wixtoolset.org/schemas/v4/wxs"
WIX_UTIL_NS = "http://wixtoolset.org/schemas/v4/wxs/util"
GUID_RE = re.compile(r"^\{?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}?$")


def _tag(local: str) -> str:
    return f"{{{WIX_NS}}}{local}"


def _util(local: str) -> str:
    return f"{{{WIX_UTIL_NS}}}{local}"


class WorkerMsiPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert WXS.exists(), f"{WXS} is required"
        cls.source = WXS.read_text(encoding="utf-8")
        cls.root = ET.parse(WXS).getroot()
        packages = cls.root.findall(_tag("Package"))
        assert len(packages) == 1, "exactly one Package element is expected"
        cls.package = packages[0]

    def find_all(self, local: str):
        return self.root.iter(_tag(local))

    def test_source_uses_wix_v4_schema_and_documents_install_then_enroll_flow(self):
        self.assertEqual(self.root.tag, _tag("Wix"))
        self.assertIn("http://wixtoolset.org/schemas/v4/wxs", self.source)
        self.assertNotIn("http://schemas.microsoft.com/wix/2006/wi", self.source)
        self.assertIn("vaultline-worker enroll --control-plane-url", self.source)

    def test_package_is_per_machine_versioned_from_build_with_stable_upgrade_code(self):
        self.assertEqual(self.package.get("Name"), "Vaultline Worker")
        self.assertEqual(self.package.get("Manufacturer"), "Vaultline")
        self.assertEqual(self.package.get("Version"), "$(var.Version)")
        self.assertEqual(self.package.get("Scope"), "perMachine")
        self.assertEqual(self.package.get("Compressed"), "yes")
        self.assertRegex(self.package.get("InstallerVersion", ""), r"^\d+$")
        self.assertGreaterEqual(int(self.package.get("InstallerVersion")), 500)
        self.assertRegex(self.package.get("UpgradeCode", ""), GUID_RE)

        upgrades = list(self.find_all("MajorUpgrade"))
        self.assertEqual(len(upgrades), 1)
        self.assertTrue(upgrades[0].get("DowngradeErrorMessage"))
        self.assertEqual(upgrades[0].get("Schedule"), "afterInstallInitialize")

        media = list(self.find_all("MediaTemplate"))
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].get("EmbedCab"), "yes")

    def test_installs_frozen_exe_under_program_files_x64(self):
        standard_dirs = {node.get("Id"): node for node in self.find_all("StandardDirectory")}
        self.assertIn("ProgramFiles64Folder", standard_dirs)
        install_dir = standard_dirs["ProgramFiles64Folder"].find(_tag("Directory"))
        self.assertIsNotNone(install_dir)
        self.assertEqual(install_dir.get("Id"), "INSTALLFOLDER")
        self.assertEqual(install_dir.get("Name"), "Vaultline")

        files = {node.get("Id"): node for node in self.find_all("File")}
        self.assertEqual(set(files), {"WorkerExe", "ResticExe", "RcloneExe", "ResticLicenseFile", "RcloneLicenseFile"})
        self.assertEqual(files["WorkerExe"].get("Source"), "$(var.WorkerExe)")
        self.assertIn("WorkerExe", self.source)
        self.assertRegex(self.source, r"\?define WorkerExe\s*=")
        self.assertEqual(files["ResticLicenseFile"].get("Source"), "$(var.ResticLicense)")
        self.assertEqual(files["ResticLicenseFile"].get("Name"), "restic-v0.19.1-LICENSE")
        self.assertEqual(files["RcloneLicenseFile"].get("Source"), "$(var.RcloneLicense)")
        self.assertEqual(files["RcloneLicenseFile"].get("Name"), "rclone-v1.75.2-COPYING")
        self.assertIn("ResticLicense", self.source)
        self.assertIn("RcloneLicense", self.source)

    def test_installs_restic_and_rclone_beside_the_worker_as_stable_components(self):
        # Defender's Behavior:Win32/SuspRclone.A kills rclone when it runs from the
        # per-start %TEMP%\_MEIxxxx extraction; the tools need a stable, excludable
        # path that the frozen worker prefers over its PyInstaller bundle.
        standard_dirs = {node.get("Id"): node for node in self.find_all("StandardDirectory")}
        install_dir = standard_dirs["ProgramFiles64Folder"].find(_tag("Directory"))
        components = {node.get("Id"): node for node in install_dir.findall(_tag("Component"))}
        self.assertEqual(set(components), {"WorkerExecutable", "ResticExecutable", "RcloneExecutable"})

        expected = {
            "ResticExecutable": ("ResticExe", "restic.exe", "$(var.ResticExe)"),
            "RcloneExecutable": ("RcloneExe", "rclone.exe", "$(var.RcloneExe)"),
        }
        for component_id, (file_id, name, source) in expected.items():
            with self.subTest(component=component_id):
                component = components[component_id]
                self.assertRegex(component.get("Guid", ""), GUID_RE)
                self.assertIsNone(component.get("Permanent"), "tools must be removed on uninstall")
                files = component.findall(_tag("File"))
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].get("Id"), file_id)
                self.assertEqual(files[0].get("Name"), name)
                self.assertEqual(files[0].get("Source"), source)
                self.assertEqual(files[0].get("KeyPath"), "yes")
                self.assertIsNone(component.find(_tag("ServiceInstall")))
        guids = [component.get("Guid") for component in self.find_all("Component") if component.get("Guid")]
        self.assertEqual(len(guids), len(set(guids)), "component GUIDs must be unique")
        self.assertRegex(self.source, r"\?define ResticExe\s*=")
        self.assertRegex(self.source, r"\?define RcloneExe\s*=")
        self.assertIn("-d ResticExe=", self.source)
        self.assertIn("-d RcloneExe=", self.source)
        self.assertIn("SuspRclone", self.source)

    def test_registers_auto_start_local_system_service_hosting_service_run(self):
        installs = list(self.find_all("ServiceInstall"))
        self.assertEqual(len(installs), 1)
        service = installs[0]
        self.assertEqual(service.get("Name"), "VaultlineWorker")
        self.assertEqual(service.get("DisplayName"), "Vaultline Worker")
        self.assertEqual(service.get("Account"), "LocalSystem")
        self.assertEqual(service.get("Start"), "auto")
        self.assertEqual(service.get("Type"), "ownProcess")
        self.assertEqual(service.get("ErrorControl"), "normal")
        self.assertEqual(service.get("Arguments"), "service run")
        self.assertEqual(service.get("Vital"), "yes")
        self.assertIn("enroll", service.get("Description", ""))
        self.assertIsNone(service.get("Password"))

        controls = list(self.find_all("ServiceControl"))
        self.assertEqual(len(controls), 1)
        control = controls[0]
        self.assertEqual(control.get("Name"), "VaultlineWorker")
        self.assertEqual(control.get("Start"), "install")
        self.assertEqual(control.get("Stop"), "both")
        self.assertEqual(control.get("Remove"), "uninstall")
        self.assertEqual(control.get("Wait"), "yes")

        # The service must live in the component that carries the executable.
        exe_component = next(
            component for component in self.find_all("Component") if component.find(_tag("File")) is not None
        )
        self.assertIsNotNone(exe_component.find(_tag("ServiceInstall")))
        self.assertIsNotNone(exe_component.find(_tag("ServiceControl")))

    def test_adds_install_dir_to_system_path(self):
        environments = [node for node in self.find_all("Environment") if node.get("Name") == "PATH"]
        self.assertEqual(len(environments), 1)
        path = environments[0]
        self.assertEqual(path.get("System"), "yes")
        self.assertEqual(path.get("Action"), "set")
        self.assertEqual(path.get("Part"), "last")
        self.assertEqual(path.get("Value"), "[INSTALLFOLDER]")

    def test_state_dir_lives_under_program_data_and_survives_uninstall(self):
        standard_dirs = {node.get("Id"): node for node in self.find_all("StandardDirectory")}
        self.assertIn("CommonAppDataFolder", standard_dirs)
        vendor = standard_dirs["CommonAppDataFolder"].find(_tag("Directory"))
        self.assertIsNotNone(vendor)
        self.assertEqual(vendor.get("Name"), "Vaultline")
        worker = vendor.find(_tag("Directory"))
        self.assertIsNotNone(worker)
        self.assertEqual(worker.get("Name"), "worker")
        logs = worker.find(_tag("Directory"))
        self.assertIsNotNone(logs)
        self.assertEqual(logs.get("Name"), "logs")

        state_components = [
            component
            for component in self.find_all("Component")
            if component.find(_tag("CreateFolder")) is not None
        ]
        self.assertGreaterEqual(len(state_components), 2, "state dir and logs dir need CreateFolder components")
        for component in state_components:
            self.assertEqual(component.get("Permanent"), "yes")
            self.assertIsNone(component.find(_tag("RemoveFolder")))
            self.assertIsNone(component.find(_tag("RemoveFile")))
            self.assertRegex(component.get("Guid", ""), GUID_RE)
        self.assertNotIn("RemoveFolder", self.source)

    def test_state_and_logs_dirs_are_restricted_to_system_and_administrators(self):
        # %ProgramData% lets BUILTIN\Users create files; without an explicit ACL
        # any local user could plant enrollment files for the LocalSystem service.
        expected_sddl = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        state_components = {
            component.get("Id"): component
            for component in self.find_all("Component")
            if component.find(_tag("CreateFolder")) is not None
        }
        self.assertEqual(set(state_components), {"WorkerStateDir", "WorkerLogsDir"})
        for component_id, component in state_components.items():
            with self.subTest(component=component_id):
                folder = component.find(_tag("CreateFolder"))
                permission_ex = folder.findall(_tag("PermissionEx"))
                self.assertEqual(len(permission_ex), 1)
                self.assertEqual(permission_ex[0].get("Sddl"), expected_sddl)
                self.assertTrue(expected_sddl.startswith("D:P"), "D:P must protect the DACL from inheritance")
                self.assertNotRegex(expected_sddl, r";;;(?:BU|AU)(?:\)|$)")
                self.assertEqual(folder.findall(_tag("Permission")), [])
                self.assertEqual(component.get("Permanent"), "yes")

    def test_service_recovery_restarts_on_failure_through_util_extension(self):
        self.assertIn(f'xmlns:util="{WIX_UTIL_NS}"', self.source)
        configs = list(self.root.iter(_util("ServiceConfig")))
        self.assertEqual(len(configs), 1)
        config = configs[0]
        self.assertEqual(config.get("ServiceName"), "VaultlineWorker")
        for action in ("FirstFailureActionType", "SecondFailureActionType", "ThirdFailureActionType"):
            self.assertEqual(config.get(action), "restart", action)
        self.assertEqual(config.get("RestartServiceDelayInSeconds"), "10")
        self.assertEqual(config.get("ResetPeriodInDays"), "1")

        exe_component = next(
            component for component in self.find_all("Component") if component.find(_tag("File")) is not None
        )
        self.assertIs(exe_component.find(_util("ServiceConfig")), config)

    def test_package_ships_no_secrets_or_configuration(self):
        lowered = self.source.lower()
        for forbidden in ("token", "secret", "credential", "password", "worker.env", "worker.json"):
            self.assertNotIn(forbidden, lowered)
        self.assertEqual(len(list(self.find_all("File"))), 5)
        self.assertEqual(len(list(self.find_all("IniFile"))), 0)
        self.assertEqual(len(list(self.find_all("RegistryValue"))), 0)

    def test_every_component_is_referenced_by_the_single_feature(self):
        features = list(self.find_all("Feature"))
        self.assertEqual(len(features), 1)
        referenced = {node.get("Id") for node in features[0].iter(_tag("ComponentRef"))}
        referenced |= {node.get("Id") for node in features[0].iter(_tag("ComponentGroupRef"))}
        groups = {node.get("Id"): node for node in self.find_all("ComponentGroup")}
        for group_id in list(referenced):
            if group_id in groups:
                referenced |= {node.get("Id") for node in groups[group_id].iter(_tag("ComponentRef"))}
                referenced |= {node.get("Id") for node in groups[group_id].iter(_tag("Component"))}
        component_ids = {node.get("Id") for node in self.find_all("Component")}
        self.assertTrue(component_ids, "components are required")
        self.assertTrue(all(component_id for component_id in component_ids), "every Component needs an explicit Id")
        self.assertEqual(component_ids - referenced, set())


if __name__ == "__main__":
    unittest.main()
