"""Run with: python -m unittest discover -s .github/tests -v (requires PyYAML).

Exercise the workflow's actual Bash block against local Git repositories.
No GitHub credentials or network access are used.
"""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

import yaml


WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/sync-upstream.yml"
UPSTREAM_URL = "https://github.com/cherry-embedded/CherryUSB.git"


class SyncUpstreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        cls.script = next(
            step["run"] for step in workflow["jobs"]["sync-upstream"]["steps"]
            if "run" in step
        )
        cls.environment = os.environ.copy()
        cls.environment.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME="Sync test",
            GIT_AUTHOR_EMAIL="sync@example.invalid", GIT_COMMITTER_NAME="Sync test",
            GIT_COMMITTER_EMAIL="sync@example.invalid",
        )
        if os.name == "nt":
            git_root = Path(shutil.which("git")).resolve().parent.parent
            cls.bash = str(git_root / "bin/bash.exe")
            cls.environment["PATH"] = os.pathsep.join([
                str(git_root / "usr/bin"), str(git_root / "bin"),
                cls.environment["PATH"],
            ])
        else:
            cls.bash = shutil.which("bash")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cherryusb-sync-test-")
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.origin = self.root / "origin.git"
        self.upstream = self.root / "upstream.git"
        self.seed = self.root / "seed"
        self.git(self.root, "init", "--bare", "-b", "master", str(self.origin))
        self.git(self.root, "init", "--bare", "-b", "master", str(self.upstream))
        self.git(self.root, "init", "-b", "master", str(self.seed))
        self.base = self.commit("base")
        self.publish(self.origin, self.base)
        self.publish(self.upstream, self.base)
        self.runner_count = 0

    def execute(self, command, cwd, check=True):
        result = subprocess.run(
            command, cwd=cwd, env=self.environment, text=True,
            encoding="utf-8", errors="replace", capture_output=True,
        )
        if check and result.returncode:
            self.fail(f"{command}\n{result.stdout}\n{result.stderr}")
        return result

    def git(self, repo, *arguments):
        return self.execute(["git", "-C", str(repo), *arguments], self.root).stdout.strip()

    def commit(self, content, parent=None):
        return self.commit_files({"state.txt": content}, parent=parent, message=content)

    def commit_files(self, files, parent=None, message="commit"):
        if parent is not None:
            self.git(self.seed, "checkout", "--detach", parent)
        for name, content in files.items():
            path = self.seed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.git(self.seed, "add", "--all")
        self.git(self.seed, "commit", "-m", message)
        return self.git(self.seed, "rev-parse", "HEAD")

    def assert_source_tree(self, actual, expected):
        result = self.execute(
            [
                "git", "-C", str(self.origin), "diff", "--quiet",
                f"{actual}^{{tree}}", f"{expected}^{{tree}}", "--", ".",
                ":(exclude).github/workflows",
            ],
            self.root,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def publish(self, remote, commit, ref="refs/heads/master", force=False):
        # Force is used only to model a rewrite by the upstream in this fixture.
        self.git(self.seed, "push", str(remote), ("+" if force else "") + f"{commit}:{ref}")

    def checkout_runner(self):
        self.runner_count += 1
        runner = self.root / f"runner-{self.runner_count}"
        self.git(self.root, "clone", "--no-local", "--branch", "master", str(self.origin), str(runner))
        # Redirect the workflow's literal upstream URL without editing its Bash.
        self.git(runner, "config", f"url.{self.upstream.as_uri()}.insteadOf", UPSTREAM_URL)
        self.git(runner, "config", "protocol.file.allow", "always")
        return runner

    def sync(self, runner=None, success=True):
        if runner is None:
            runner = self.checkout_runner()
        script_file = self.root / f"sync-{self.runner_count}.sh"
        script_file.write_text(self.script, encoding="utf-8", newline="\n")
        result = self.execute(
            [self.bash, "--noprofile", "--norc", "-e", "-o", "pipefail", str(script_file)],
            runner, check=False,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return self.git(self.origin, "rev-parse", "refs/heads/master")

    def assert_preserved(self, old, new):
        self.git(self.origin, "merge-base", "--is-ancestor", old, new)

    def assert_upstream_tree(self, new, upstream):
        self.assertEqual(
            self.git(self.origin, "rev-parse", f"{new}^{{tree}}"),
            self.git(self.upstream, "rev-parse", f"{upstream}^{{tree}}"),
        )

    def test_fast_forward_and_no_change(self):
        new = self.commit("upstream next")
        self.publish(self.upstream, new)
        self.publish(self.upstream, new, "refs/tags/v-next")
        self.assertEqual(self.sync(), new)
        self.assertEqual(self.sync(), new)
        tag_commit = self.git(self.origin, "rev-parse", "refs/tags/v-next^{commit}")
        self.assert_source_tree(tag_commit, new)
        self.assertNotEqual(tag_commit, new)
        self.assert_preserved(self.base, new)

    def test_conflicting_rewrite_preserves_history_on_master(self):
        old = self.commit("old published version")
        self.publish(self.origin, old)
        new = self.commit("rewritten upstream version", self.base)
        self.publish(self.upstream, new)
        merged = self.sync()
        self.assert_preserved(old, merged)
        self.assert_preserved(new, merged)
        self.assert_upstream_tree(merged, new)
        self.assertEqual(self.git(self.origin, "rev-parse", f"{merged}^1"), old)
        self.assertEqual(self.sync(), merged, "unchanged upstream must not create daily merge commits")
        self.assertEqual(self.git(self.origin, "for-each-ref", "--format=%(refname)", "refs/heads"), "refs/heads/master")
        next_upstream = self.commit("subsequent upstream version", new)
        self.publish(self.upstream, next_upstream)
        next_master = self.sync()
        self.assert_preserved(merged, next_master)
        self.assert_preserved(old, next_master)
        self.assert_upstream_tree(next_master, next_upstream)

    def test_upstream_rewind_preserves_newer_pins(self):
        old = self.commit("published version before rewind")
        self.publish(self.origin, old)
        merged = self.sync()
        self.assert_preserved(old, merged)
        self.assert_upstream_tree(merged, self.base)
        self.assertEqual(self.sync(), merged)

    def test_unrelated_upstream_history(self):
        self.git(self.seed, "checkout", "--orphan", "replacement")
        new = self.commit("replacement repository contents")
        self.publish(self.upstream, new, force=True)
        merged = self.sync()
        self.assert_preserved(self.base, merged)
        self.assert_preserved(new, merged)
        self.assert_upstream_tree(merged, new)

    def test_new_commit_with_identical_tree_is_kept(self):
        self.git(self.seed, "commit", "--allow-empty", "-m", "upstream metadata only")
        new = self.git(self.seed, "rev-parse", "HEAD")
        self.publish(self.upstream, new)
        synced = self.sync()
        self.assertEqual(synced, new)
        self.assert_preserved(new, synced)
        self.assert_upstream_tree(synced, new)

    def test_upstream_workflows_are_not_imported(self):
        fork = self.commit_files(
            {".github/workflows/build.yml": "fork workflow\n"},
            parent=self.base,
            message="fork workflow",
        )
        self.publish(self.origin, fork)
        upstream = self.commit_files(
            {
                "state.txt": "upstream source\n",
                ".github/workflows/build.yml": "upstream workflow\n",
            },
            parent=self.base,
            message="upstream workflow",
        )
        self.publish(self.upstream, upstream)

        synced = self.sync()
        self.assert_source_tree(synced, upstream)
        self.assert_preserved(fork, synced)
        self.assert_preserved(upstream, synced)
        self.assertEqual(
            self.git(self.origin, "show", f"{synced}:.github/workflows/build.yml"),
            "fork workflow",
        )

    def test_tag_outside_upstream_master_is_not_published(self):
        side = self.commit("side branch", parent=self.base)
        self.publish(self.upstream, side, "refs/heads/release-only")
        self.publish(self.upstream, side, "refs/tags/v-side")
        self.sync()
        self.assertEqual(self.git(self.origin, "tag", "--list", "v-side"), "")

    def test_missing_upstream_master_leaves_fork_untouched(self):
        self.git(self.upstream, "update-ref", "-d", "refs/heads/master")
        self.assertEqual(self.sync(success=False), self.base)

    def test_rewritten_tag_aborts_before_updating_master(self):
        self.publish(self.origin, self.base, "refs/tags/v-stable")
        new = self.commit("upstream replaces existing tag")
        self.publish(self.upstream, new)
        self.publish(self.upstream, new, "refs/tags/v-stable")
        self.assertEqual(self.sync(success=False), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "refs/tags/v-stable"), self.base)

    def test_tag_created_after_checkout_rejects_whole_push(self):
        runner = self.checkout_runner()
        self.publish(self.origin, self.base, "refs/tags/v-race")
        new = self.commit("upstream tag conflict after checkout")
        self.publish(self.upstream, new)
        self.publish(self.upstream, new, "refs/tags/v-race")
        self.assertEqual(self.sync(runner, success=False), self.base)
        self.assertEqual(self.git(self.origin, "rev-parse", "refs/tags/v-race"), self.base)

    def test_tag_absent_upstream_is_retained(self):
        self.publish(self.origin, self.base, "refs/tags/v-retained")
        new = self.commit("upstream without the old tag")
        self.publish(self.upstream, new)
        self.assertEqual(self.sync(), new)
        self.assertEqual(self.git(self.origin, "rev-parse", "refs/tags/v-retained"), self.base)

    def test_concurrent_master_update_rejects_whole_push(self):
        concurrent = self.commit("concurrent fork update")
        self.publish(self.origin, concurrent, "refs/heads/other-writer")
        runner = self.checkout_runner()
        new = self.commit("upstream concurrent update", self.base)
        self.publish(self.upstream, new)
        self.publish(self.upstream, new, "refs/tags/v-unpushed")
        hook = runner / ".git/hooks/pre-push"
        hook.write_text(
            "#!/bin/sh\n"
            f"git -C {shlex.quote(self.origin.as_posix())} update-ref refs/heads/master {concurrent} {self.base}\n",
            encoding="utf-8", newline="\n",
        )
        hook.chmod(0o755)
        self.assertEqual(self.sync(runner, success=False), concurrent)
        self.assertEqual(self.git(self.origin, "tag", "--list", "v-unpushed"), "")
        self.assert_preserved(self.base, concurrent)

    def test_rejected_push_preserves_all_refs(self):
        new = self.commit("upstream while writes are rejected")
        self.publish(self.upstream, new)
        self.publish(self.upstream, new, "refs/tags/v-unpushed")
        hook = self.origin / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        self.assertEqual(self.sync(success=False), self.base)
        self.assertEqual(self.git(self.origin, "tag", "--list"), "")

    def test_pinned_submodule_can_be_recloned_after_rewrite_and_gc(self):
        pinned = self.commit("version pinned by the consumer")
        self.publish(self.origin, pinned)
        consumer = self.root / "consumer"
        self.git(self.root, "init", "-b", "main", str(consumer))
        (consumer / ".gitmodules").write_text(
            '[submodule "library/CherryUSB"]\n\tpath = library/CherryUSB\n'
            f'\turl = {self.origin.as_uri()}\n\tbranch = master\n', encoding="utf-8",
        )
        self.git(consumer, "add", ".gitmodules")
        self.git(consumer, "update-index", "--add", "--cacheinfo", f"160000,{pinned},library/CherryUSB")
        self.git(consumer, "commit", "-m", "pin CherryUSB dependency")
        rewritten = self.commit("replacement upstream code", self.base)
        self.publish(self.upstream, rewritten)
        master = self.sync()
        self.assert_preserved(pinned, master)
        self.git(self.origin, "reflog", "expire", "--expire=now", "--all")
        self.git(self.origin, "gc", "--prune=now")
        master_clone = self.root / "master-only"
        self.git(self.root, "clone", "--no-local", "--single-branch", "--no-tags", "--branch", "master", str(self.origin), str(master_clone))
        self.git(master_clone, "checkout", "--detach", pinned)
        self.assertEqual((master_clone / "state.txt").read_text(), "version pinned by the consumer")
        fresh = self.root / "fresh-consumer"
        self.git(self.root, "clone", "--no-local", str(consumer), str(fresh))
        self.git(fresh, "-c", "protocol.file.allow=always", "submodule", "update", "--init", "--recursive")
        dependency = fresh / "library/CherryUSB"
        self.assertEqual(self.git(dependency, "rev-parse", "HEAD"), pinned)
        self.assertEqual((dependency / "state.txt").read_text(), "version pinned by the consumer")


if __name__ == "__main__":
    unittest.main()
