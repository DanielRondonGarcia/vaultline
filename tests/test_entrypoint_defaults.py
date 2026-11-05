import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[1] / "src" / "entrypoint.sh"
ENV_BLOCK_START = "cat <<EOF > env.sh"


def env_sh_block_lines():
    """Return the heredoc body of the entrypoint's env defaulting block, so the
    test always exercises the same defaults the real entrypoint writes."""
    with open(ENTRYPOINT, encoding="utf-8", newline="") as handle:
        lines = handle.read().splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(ENV_BLOCK_START))
    except StopIteration:
        raise AssertionError(
            "src/entrypoint.sh no longer writes env.sh through the expected 'cat <<EOF > env.sh' block"
        )
    body = []
    for line in lines[start + 1 :]:
        if line == "EOF":
            return body
        body.append(line)
    raise AssertionError("unterminated env.sh heredoc block in src/entrypoint.sh")


class EntrypointStrategyDefaultsTests(unittest.TestCase):
    """T4 correction (S1, S3): the standalone Docker entrypoint must default the
    backup strategy to Restic now that TAR is removed. An explicit selection
    must be preserved verbatim: explicit Restic keeps Restic behavior, and an
    explicit TAR value still reaches the runtime, which rejects it with a clear
    error (covered by tests/test_restic_only.py). No Docker, no data, no
    external paths: only the entrypoint's env.sh defaulting block runs under
    bash inside a temporary directory."""

    @classmethod
    def setUpClass(cls):
        cls.bash = shutil.which("bash")
        if cls.bash is None:
            raise unittest.SkipTest("bash is required to exercise the entrypoint defaults")

    def run_entrypoint_env_defaults(self, env_overrides=None, extra_env_names=()):
        workdir = tempfile.mkdtemp(prefix="entrypoint-defaults-")
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        names = ("BACKUP_STRATEGY", "RESTORE_BACKUP_STRATEGY", *extra_env_names)
        harness = (
            "set -e\n"
            "cat <<EOF > env.sh\n"
            + "\n".join(env_sh_block_lines())
            + "\nEOF\n"
            "source env.sh\n"
            + "".join(
                'printf \'{0}=%s\\n\' "${0}"\n'.format(name)
                for name in names
            )
        )
        script = Path(workdir) / "run.sh"
        script.write_bytes(harness.encode("utf-8"))
        env = os.environ.copy()
        for name in ("BACKUP_STRATEGY", "RESTORE_BACKUP_STRATEGY", *extra_env_names):
            env.pop(name, None)
        env.pop("RESTORE_MODE", None)
        for name, value in (env_overrides or {}).items():
            env[name] = value
        completed = subprocess.run(
            [self.bash, "./run.sh"],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"entrypoint env block failed (exit {completed.returncode}):\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        resolved = {}
        for line in completed.stdout.splitlines():
            for name in ("BACKUP_STRATEGY", "RESTORE_BACKUP_STRATEGY", *extra_env_names):
                if line.startswith(name + "="):
                    resolved[name] = line[len(name) + 1 :]
        self.assertEqual(
            sorted(resolved),
            sorted(["BACKUP_STRATEGY", "RESTORE_BACKUP_STRATEGY", *extra_env_names]),
            f"unexpected env.sh defaults output: {completed.stdout!r}",
        )
        return resolved

    def test_default_backup_and_restore_strategy_are_restic_without_explicit_selection(self):
        resolved = self.run_entrypoint_env_defaults()

        self.assertEqual(resolved["BACKUP_STRATEGY"], "restic")
        self.assertEqual(resolved["RESTORE_BACKUP_STRATEGY"], "restic")

    def test_explicit_restic_selection_keeps_restic_behavior(self):
        resolved = self.run_entrypoint_env_defaults({"BACKUP_STRATEGY": "restic"})

        self.assertEqual(resolved["BACKUP_STRATEGY"], "restic")
        self.assertEqual(resolved["RESTORE_BACKUP_STRATEGY"], "restic")

    def test_explicit_tar_selection_is_passed_through_for_runtime_rejection(self):
        resolved = self.run_entrypoint_env_defaults({"BACKUP_STRATEGY": "tar"})

        self.assertEqual(resolved["BACKUP_STRATEGY"], "tar")
        self.assertEqual(resolved["RESTORE_BACKUP_STRATEGY"], "tar")

    def test_removed_tar_configuration_defaults_are_not_written_anymore(self):
        """T7 (S1, S3): BACKUP_FILENAME and GPG_PASSPHRASE were only consumed
        by the removed TAR strategy. The entrypoint must stop carrying their
        defaults and must not inject them from env.sh; a bare environment
        must end up with neither variable defined."""
        self.assertTrue(
            all(
                not line.startswith(("BACKUP_FILENAME", "GPG_PASSPHRASE"))
                for line in env_sh_block_lines()
            ),
            "env.sh defaults must not contain obsolete BACKUP_FILENAME/GPG_PASSPHRASE",
        )
        resolved = self.run_entrypoint_env_defaults(
            extra_env_names=("BACKUP_FILENAME", "GPG_PASSPHRASE")
        )
        # The removed variables must not be defined after sourcing env.sh;
        # printf prints an empty value marker for each unset name.
        self.assertEqual(resolved["BACKUP_FILENAME"], "")
        self.assertEqual(resolved["GPG_PASSPHRASE"], "")

    def test_explicit_restore_strategy_selection_is_preserved(self):
        resolved = self.run_entrypoint_env_defaults({"RESTORE_BACKUP_STRATEGY": "restic"})

        self.assertEqual(resolved["BACKUP_STRATEGY"], "restic")
        self.assertEqual(resolved["RESTORE_BACKUP_STRATEGY"], "restic")


if __name__ == "__main__":
    unittest.main()
