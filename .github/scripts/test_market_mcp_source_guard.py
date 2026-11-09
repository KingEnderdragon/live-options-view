"""Fail-closed checks for the reviewed MCP consumer source."""

import os
import subprocess
import tempfile
import unittest

from market_mcp_source_guard import GuardError, validate_source


class Repo:
    def __init__(self, root):
        self.root = root
        self.git("init", "-b", "master")
        self.git("config", "user.name", "Guard Test")
        self.git("config", "user.email", "guard@example.invalid")

    def git(self, *args):
        completed = subprocess.run(
            ["git", "-C", self.root, *args],
            check=True, capture_output=True, text=True,
        )
        return completed.stdout.strip()

    def write(self, path, text):
        target = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(target), exist_ok=True) if os.path.dirname(target) else None
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")


def accepted_repo():
    temp = tempfile.TemporaryDirectory()
    repo = Repo(temp.name)
    repo.write("README.md", "base\n")
    repo.commit("base")
    repo.write("market_mcp/overview.js", "consumer\n")
    repo.write("market_mcp/overview-v2.test.js", "test\n")
    sha = repo.commit("consumer")
    return temp, repo, sha


class SourceGuardTests(unittest.TestCase):
    def test_exact_reviewed_commit_passes(self):
        temp, repo, sha = accepted_repo()
        with temp:
            changed = validate_source(repo.root, sha, "HEAD")
            self.assertEqual(sorted(changed),
                             ["market_mcp/overview-v2.test.js", "market_mcp/overview.js"])

    def test_merge_commit_uses_first_parent_diff(self):
        temp, repo, sha = accepted_repo()
        with temp:
            repo.git("checkout", "-b", "consumer", sha)
            repo.git("checkout", "master")
            repo.git("merge", "--no-ff", "-m", "merge consumer", "consumer")
            merge_sha = repo.git("rev-parse", "HEAD")
            changed = validate_source(repo.root, merge_sha, "HEAD")
            self.assertEqual(len(changed), 2)

    def test_unreviewed_path_blocks(self):
        temp = tempfile.TemporaryDirectory()
        with temp:
            repo = Repo(temp.name)
            repo.write("README.md", "base\n")
            repo.commit("base")
            repo.write("market_mcp/overview.js", "consumer\n")
            repo.write("market_data/live_market/price_lane.py", "producer\n")
            sha = repo.commit("mixed")
            with self.assertRaisesRegex(GuardError, "unreviewed paths"):
                validate_source(repo.root, sha, "HEAD")

    def test_dirty_tree_blocks(self):
        temp, repo, sha = accepted_repo()
        with temp:
            repo.write("market_mcp/overview.js", "dirty\n")
            with self.assertRaisesRegex(GuardError, "not clean"):
                validate_source(repo.root, sha, "HEAD")

    def test_head_mismatch_blocks(self):
        temp, repo, sha = accepted_repo()
        with temp:
            with self.assertRaisesRegex(GuardError, "does not match"):
                validate_source(repo.root, "0" * 40, "HEAD")

    def test_non_ancestor_blocks(self):
        temp, repo, sha = accepted_repo()
        with temp:
            repo.git("checkout", "-b", "detached", "HEAD~1")
            repo.write("market_mcp/overview.js", "other\n")
            other = repo.commit("other")
            with self.assertRaisesRegex(GuardError, "not an ancestor"):
                validate_source(repo.root, other, "master")

    def test_invalid_sha_and_root_commit_block(self):
        temp, repo, _ = accepted_repo()
        with temp:
            with self.assertRaisesRegex(GuardError, "40-character"):
                validate_source(repo.root, "not-a-sha", "HEAD")
            root = repo.git("rev-list", "--max-parents=0", "HEAD")
            repo.git("checkout", root)
            with self.assertRaisesRegex(GuardError, "no parent"):
                validate_source(repo.root, root, "master")


if __name__ == "__main__":
    unittest.main()
