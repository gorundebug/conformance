from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import subprocess
from unittest import mock

from published_components import run


class PublishedComponentsTest(unittest.TestCase):
    def test_declared_internal_tags_follow_generated_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "goexample"
            service = example / "orderservice"
            service.mkdir(parents=True)
            (service / "make.generated.mk").write_text(
                "MODEL_SOURCE := "
                "https://github.com/gorundebug/model_go.git\\#v4.5.6\n"
            )
            subprocess.run(["git", "init", "-q"], cwd=example, check=True)
            subprocess.run(["git", "add", "orderservice/make.generated.mk"],
                           cwd=example, check=True)
            (example / "dist").mkdir()
            (example / "dist" / "stale.generated.mk").write_text(
                "MODEL_SOURCE := "
                "https://github.com/gorundebug/model_go.git\\#v1.0.0\n"
            )

            self.assertEqual(
                run.declared_internal_tags(root),
                {"model_go": {"v4.5.6"}},
            )

    def test_repository_specs_keep_go_components_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = root / "goexample"
            module = example / "model_go"
            module.mkdir(parents=True)
            (example / "clone.generated.sh").write_text(
                'clone_if_missing "model_go" '
                '"https://github.com/gorundebug/model_go.git" "v1.2.3"\n'
            )
            (module / "go.mod").write_text(
                "module github.com/gorundebug/model_go\n"
            )
            subprocess.run(["git", "init", "-q"], cwd=example, check=True)
            subprocess.run(["git", "add", "clone.generated.sh", "model_go/go.mod"],
                           cwd=example, check=True)

            specs = run.repository_specs(root)

            model = next(spec for spec in specs if spec.name == "model_go")
            self.assertEqual(model.source, module)
            self.assertEqual(model.tags, ("v1.2.3",))
            self.assertEqual(
                model.relative_path,
                Path("github.com/gorundebug/model_go.git"),
            )

    def test_mirror_environment_preserves_canonical_urls(self) -> None:
        host = run.mirror_environment(19084)
        docker = run.mirror_environment(19084, docker=True)

        self.assertEqual(host["GIT_CONFIG_VALUE_0"], "https://github.com/")
        self.assertIn("localhost:19084", host["GIT_CONFIG_KEY_0"])
        self.assertIn("host.docker.internal:19084", docker["GIT_CONFIG_KEY_0"])

    def test_direct_mode_rewrites_only_internal_repositories(self) -> None:
        environment = run.mirror_environment(19084, include_external=False)

        self.assertEqual(environment["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(
            environment["GIT_CONFIG_VALUE_0"],
            "https://github.com/gorundebug/",
        )
        self.assertIn(
            "/github.com/gorundebug/.insteadOf",
            environment["GIT_CONFIG_KEY_0"],
        )

    def test_build_environment_does_not_require_proxy(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            environment = run.build_environment(19084)

        self.assertNotIn("DEPENDENCY_PROXY_DIR", environment)
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "1")

    def test_snapshot_copies_only_tracked_head_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=source, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@localhost"],
                cwd=source,
                check=True,
            )
            (source / "tracked.txt").write_text("tracked")
            subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
            (source / "ignored-build.txt").write_text("not published")

            destination = root / "destination"
            run.copy_tracked_source(source, destination)

            self.assertEqual((destination / "tracked.txt").read_text(), "tracked")
            self.assertFalse((destination / "ignored-build.txt").exists())

    def test_snapshot_preserves_force_tracked_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            source = repository / "service"
            source.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=repository, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@localhost"],
                cwd=repository,
                check=True,
            )
            (source / ".gitignore").write_text("module/\n")
            module = source / "module"
            module.mkdir()
            (module / "tracked.txt").write_text("published")
            subprocess.run(
                ["git", "add", "service/.gitignore"], cwd=repository, check=True
            )
            subprocess.run(
                ["git", "add", "--force", "service/module/tracked.txt"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-qm", "fixture"], cwd=repository, check=True
            )

            mirror = root / "mirror"
            scratch = root / "scratch"
            mirror.mkdir()
            scratch.mkdir()
            spec = run.RepositorySpec("gorundebug", "fixture", source, ("v1.0.0",))
            run.snapshot_repository(spec, mirror, scratch)
            files = subprocess.run(
                [
                    "git", "--git-dir",
                    str(mirror / spec.relative_path),
                    "ls-tree", "-r", "--name-only", "v1.0.0",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertIn("module/tracked.txt", files)

    def test_snapshot_preserves_standalone_repository_commit_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=source, check=True)
            subprocess.run(
                ["git", "config", "user.name", "Test"], cwd=source, check=True
            )
            subprocess.run(
                ["git", "config", "user.email", "test@localhost"],
                cwd=source,
                check=True,
            )
            (source / "locked.txt").write_text("locked")
            subprocess.run(["git", "add", "locked.txt"], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=source, check=True)
            subprocess.run(["git", "tag", "v1.0.0"], cwd=source, check=True)
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=source, check=True,
                capture_output=True, text=True,
            ).stdout.strip()

            mirror = root / "mirror"
            scratch = root / "scratch"
            mirror.mkdir()
            scratch.mkdir()
            spec = run.RepositorySpec("gorundebug", "fixture", source, ("v1.0.0",))
            run.snapshot_repository(spec, mirror, scratch)
            mirrored = subprocess.run(
                ["git", "--git-dir", str(mirror / spec.relative_path),
                 "rev-parse", "v1.0.0^{commit}"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(mirrored, commit)

    def test_stale_workspace_cleanup_removes_only_owned_prefix(self) -> None:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        stale = Path(tempfile.mkdtemp(prefix=run.WORKSPACE_PREFIX)).resolve()
        unrelated = Path(tempfile.mkdtemp(prefix="unrelated-fixture-")).resolve()
        try:
            run.cleanup_stale_workspaces()
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(unrelated.parent, temporary_root)
        finally:
            if unrelated.exists():
                unrelated.rmdir()

    def test_service_repository_names_keep_hybrid_layout(self) -> None:
        self.assertEqual(
            run.service_repository_name("go", "orderservice"),
            "orderservice",
        )
        self.assertEqual(
            run.service_repository_name("cpp", "orderservice"),
            "cppexample-orderservice",
        )
        self.assertEqual(
            run.service_repository_name("typescript", "orderservice"),
            "tsexample-orderservice",
        )

    def test_service_packagers_follow_release_artifact_shape(self) -> None:
        self.assertIsNone(run.service_package_script("go", "orderservice"))
        self.assertIsNone(run.service_package_script("cpp", "automationservice"))
        self.assertEqual(
            run.service_package_script("cpp", "orderservice"),
            "scripts/package-cpp-service.generated.sh",
        )
        self.assertEqual(
            run.service_package_script("python", "automationservice"),
            "scripts/package-python-service.generated.sh",
        )


if __name__ == "__main__":
    unittest.main()
