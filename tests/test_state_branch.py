import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/state_branch.py"


class StateBranchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "origin.git"
        self.checkout = self.root / "checkout"
        self.run_git(self.root, "init", "--bare", str(self.remote))
        self.run_git(self.root, "clone", str(self.remote), str(self.checkout))
        self.run_git(self.checkout, "config", "user.name", "Test")
        self.run_git(self.checkout, "config", "user.email", "test@example.invalid")
        (self.checkout / "README.md").write_text("original source\n")
        self.run_git(self.checkout, "add", "README.md")
        self.run_git(self.checkout, "commit", "-m", "initial")
        self.run_git(self.checkout, "push", "origin", "HEAD")
        self.original_head = self.run_git(self.checkout, "rev-parse", "HEAD").stdout

    def run_git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)

    def state_script(self, operation, cwd=None, check=True):
        return subprocess.run([sys.executable, str(SCRIPT), operation], cwd=cwd or self.checkout,
                              capture_output=True, text=True, check=check)

    def write_state(self, text):
        (self.checkout / ".local/state.json").write_text(text)

    def test_state_survives_fresh_checkout_without_changing_main(self):
        self.state_script("load")
        self.write_state('{"updated_at":"2026-11-02","notified":["event"]}\n')
        self.state_script("save")
        self.assertEqual(self.run_git(self.checkout, "rev-parse", "HEAD").stdout, self.original_head)
        self.assertEqual((self.checkout / "README.md").read_text(), "original source\n")
        fresh = self.root / "fresh"
        self.run_git(self.root, "clone", str(self.remote), str(fresh))
        self.state_script("load", cwd=fresh)
        self.assertEqual((fresh / ".local/state.json").read_bytes(), (self.checkout / ".local/state.json").read_bytes())
        self.assertEqual(self.run_git(fresh, "status", "--porcelain", "--untracked-files=no").stdout, b"")

    def test_unchanged_state_does_not_create_commit(self):
        self.state_script("load")
        self.write_state('{"updated_at":"first"}\n')
        self.state_script("save")
        self.state_script("load")
        old = self.run_git(self.checkout, "ls-remote", "origin", "refs/heads/gacha-monitor-state").stdout
        self.assertIn("unchanged", self.state_script("save").stdout)
        self.assertEqual(old, self.run_git(self.checkout, "ls-remote", "origin", "refs/heads/gacha-monitor-state").stdout)

    def test_failed_remote_does_not_erase_local_state(self):
        self.state_script("load")
        self.write_state("keep this state")
        self.run_git(self.checkout, "remote", "set-url", "origin", str(self.root / "nonexistent.git"))
        self.assertNotEqual(self.state_script("load", check=False).returncode, 0)
        self.assertEqual((self.checkout / ".local/state.json").read_text(), "keep this state")

    def test_concurrent_state_write_is_rejected_without_force_push(self):
        self.state_script("load")
        self.write_state('{"updated_at":"first"}\n')
        self.state_script("save")
        self.state_script("load")
        other = self.root / "other"
        self.run_git(self.root, "clone", str(self.remote), str(other))
        self.state_script("load", cwd=other)
        (other / ".local/state.json").write_text('{"updated_at":"second"}\n')
        self.state_script("save", cwd=other)
        self.write_state('{"updated_at":"stale writer"}\n')
        self.assertNotEqual(self.state_script("save", check=False).returncode, 0)
        self.state_script("load", cwd=other)
        self.assertIn("second", (other / ".local/state.json").read_text())


if __name__ == "__main__":
    unittest.main()
