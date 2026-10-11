"""Exercise only a temporary launcher, with no real credentials or services."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


LAUNCHER = Path(__file__).resolve().parents[1] / "run.sh"
COMMANDS = (
    "start", "stop", "restart", "status", "logs", "initdb", "inventory",
    "ingest", "monitor", "cron-install",
)
ARGUMENT_FREE = tuple(c for c in COMMANDS if c not in ("ingest", "monitor"))
BASH = shutil.which("bash")


class RunHelpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="run-help-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.launcher = self.project / "run.sh"
        shutil.copy2(LAUNCHER, self.launcher)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env_file = self.project / ".env"
        self.env_file.write_text(
            'printf "ENV WAS SOURCED\\n" > "$HELP_SENTINEL"\nexit 91\n'
        )
        self.env_file.chmod(0o644)
        self.sentinel = self.root / "env-sourced"

        # Do not inherit the test runner's credentials, Python paths or shell hooks.
        # BASH_ENV below only blocks process signals, including Bash's kill builtin.
        guard = self.root / "guard.sh"
        guard.write_text(
            'kill() { printf "FORBIDDEN kill\\n"; return 98; }\n'
        )
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(self.root),
            "BASH_ENV": str(guard),
            "HELP_SENTINEL": str(self.sentinel),
            "LC_ALL": "C",
        }
        os.symlink(shutil.which("dirname"), self.bin / "dirname")
        # Unknown programs cannot run: PATH contains only these failing stubs,
        # dirname, and the strict help-only Python stand-in.
        for name in (
            "chmod", "mkdir", "flock", "nohup", "streamlit", "sleep", "tail",
            "rm", "seq", "hostname", "awk", "cat", "psql", "crontab", "ps",
            "pgrep", "pkill", "killall",
        ):
            self.write_executable(
                self.bin / name,
                f'printf "FORBIDDEN {name}\\n"\nexit 98\n',
            )
        self.write_executable(
            self.bin / "python3",
            'if [[ $# -ne 5 || "$1" != -B || "$2" != -m || "$3" != schwab\n'
            '      || ( "$4" != ingest && "$4" != monitor ) || "$5" != --help ]]; then\n'
            '    printf "FORBIDDEN python invocation\\n"\n'
            '    exit 98\n'
            'fi\n'
            'if [[ -v DATABASE_URL || -v APP_PASSWORD || -v GMAIL_USER\n'
            '      || -v GMAIL_APP_PASSWORD || -v LONGBRIDGE_ACCESS_TOKEN ]]; then\n'
            '    printf "FORBIDDEN credentials\\n"\n'
            '    exit 98\n'
            'fi\n'
            'printf "ARGPARSE %s --help (bytecode disabled)\\n" "$4"\n'
            'printf "options: --dry-run --no-notify\\n"\n',
        )

    @staticmethod
    def write_executable(path, body):
        path.write_text(f"#!{BASH}\n{body}")
        path.chmod(0o755)

    def seed_runtime(self):
        password = self.project / ".app_password"
        password.write_text("synthetic-password-do-not-read\n")
        password.chmod(0o644)
        runtime = self.project / ".run"
        runtime.mkdir()
        for name, content in (
            ("app.pid", "999999999\n"),
            ("app.log", "synthetic-log-do-not-read\n"),
            ("ingest.lock", "ingest lock sentinel\n"),
            ("monitor.lock", "monitor lock sentinel\n"),
        ):
            (runtime / name).write_text(content)

    def snapshot(self):
        """Catch creations, removals, content changes and metadata-only chmods."""
        result = {}
        for path in (self.root, *sorted(self.root.rglob("*"))):
            metadata = path.lstat()
            content = (
                os.readlink(path) if path.is_symlink()
                else path.read_bytes() if path.is_file() else None
            )
            result[str(path.relative_to(self.root))] = (
                metadata.st_mode, metadata.st_mtime_ns, metadata.st_ctime_ns, content,
            )
        return result

    def invoke(self, args):
        return subprocess.run(
            [BASH, str(self.launcher), *args], cwd=self.root, env=self.env,
            capture_output=True, text=True, timeout=5,
        )

    def assert_safe(self, args, expected_code=0):
        before = self.snapshot()
        completed = self.invoke(args)
        self.assertEqual(completed.returncode, expected_code, completed)
        self.assertEqual(self.snapshot(), before, args)
        self.assertFalse(self.sentinel.exists())
        self.assertNotIn("FORBIDDEN", completed.stdout + completed.stderr)
        self.assertNotIn("synthetic-password", completed.stdout + completed.stderr)
        self.assertNotIn("synthetic-log", completed.stdout + completed.stderr)
        if expected_code == 0:
            self.assertEqual(completed.stderr, "")
            self.assertIn("Usage:", completed.stdout)
        else:
            self.assertEqual(completed.stdout, "")
            self.assertIn("Error:", completed.stderr)
            self.assertIn("Usage:", completed.stderr)
        return completed

    def check_help_forms(self):
        for args in (("-h",), ("--help",), ("help",)):
            with self.subTest(args=args):
                result = self.assert_safe(args)
                for command in COMMANDS:
                    self.assertIn(command, result.stdout)
                self.assertIn("default: start", result.stdout)
                self.assertNotIn("ARGPARSE", result.stdout)
        for command in COMMANDS:
            for args in (("help", command), (command, "-h"), (command, "--help")):
                with self.subTest(args=args):
                    result = self.assert_safe(args)
                    self.assertIn(f"run.sh {command}", result.stdout)
                    if command in ("ingest", "monitor"):
                        self.assertEqual(result.stdout.count("ARGPARSE"), 1)
                        self.assertIn(f"ARGPARSE {command} --help", result.stdout)
                        self.assertIn("--dry-run --no-notify", result.stdout)
                    else:
                        self.assertNotIn("ARGPARSE", result.stdout)
                        self.assertIn("No other arguments accepted", result.stdout)

    def test_all_help_forms_without_runtime_files(self):
        self.check_help_forms()

    def test_all_help_forms_preserve_existing_runtime_files(self):
        self.seed_runtime()
        self.check_help_forms()

    def test_delegated_help_after_or_before_options_is_safe(self):
        self.seed_runtime()
        for command, options in (
            ("ingest", ("--days", "7", "--dry-run", "--no-notify")),
            ("monitor", ("--dry-run", "--no-notify")),
        ):
            for flag in ("-h", "--help"):
                for args in ((command, flag, *options), (command, *options, flag)):
                    with self.subTest(args=args):
                        self.assert_safe(args)

    def test_invalid_arguments_are_rejected_before_side_effects(self):
        self.seed_runtime()
        invalid = [
            ("unknown",), ("run",), ("",), ("help", "unknown"),
            ("help", "run"), ("help", ""), ("help", "--help"),
            ("--help", "start"), ("-h", "start"),
            ("unknown", "--help"),
        ]
        for command in COMMANDS:
            invalid.extend((("help", command, "extra"), ("help", command, "-h")))
        for command in ARGUMENT_FREE:
            invalid.extend((
                (command, "extra"), (command, "--unknown"), (command, ""),
                (command, "-h", "extra"), (command, "--help", "extra"),
                (command, "extra", "--help"), (command, "-h", "--help"),
            ))
        for args in invalid:
            with self.subTest(args=args):
                self.assert_safe(args, expected_code=2)

    def test_no_arguments_still_select_start(self):
        # The synthetic .env deliberately exits before password/process work.
        for args in ((), ("start",)):
            with self.subTest(args=args):
                self.sentinel.unlink(missing_ok=True)
                # Permit only the expected chmod without touching permissions.
                self.write_executable(
                    self.bin / "chmod",
                    '[[ $# -eq 2 && "$1" == 600 && "$2" == .env ]] || exit 98\n',
                )
                result = self.invoke(args)
                self.assertEqual(result.returncode, 91, result)
                self.assertEqual(self.sentinel.read_text(), "ENV WAS SOURCED\n")
                self.assertFalse((self.project / ".app_password").exists())
                self.assertFalse((self.project / ".run").exists())

    def test_delegated_help_failure_is_propagated_without_side_effects(self):
        self.write_executable(self.bin / "python3", 'exit 37\n')
        for command in ("ingest", "monitor"):
            for args in (("help", command), (command, "-h"), (command, "--help")):
                with self.subTest(args=args):
                    before = self.snapshot()
                    result = self.invoke(args)
                    self.assertEqual(result.returncode, 37, result)
                    self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
