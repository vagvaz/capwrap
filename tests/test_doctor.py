"""Tests for capwrap.doctor: version compare, drift, config checks, exit codes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from capwrap import doctor
from capwrap.config import load_config_data


# ---------------------------------------------------------------------------
# version comparison
# ---------------------------------------------------------------------------


class TestVersions:
    def test_older_vs_newer(self):
        assert doctor.compare_versions("0.75.5", "0.85.1") == -1

    def test_newer_vs_older(self):
        assert doctor.compare_versions("0.85.1", "0.75.5") == 1

    def test_equal(self):
        assert doctor.compare_versions("1.2.3", "1.2.3") == 0

    def test_missing_component_is_zero(self):
        assert doctor.compare_versions("1.2", "1.2.0") == 0

    def test_lenient_prefixes(self):
        assert doctor.compare_versions("v0.75.5 (pi)", "0.85.1-beta") == -1

    def test_malformed_is_no_verdict(self):
        assert doctor.compare_versions("banana", "1.2.3") is None
        assert doctor.compare_versions("1.2.3", "") is None

    def test_parse_version(self):
        assert doctor.parse_version("1.0.58 (Claude Code)") == (1, 0, 58)
        assert doctor.parse_version("v2.3") == (2, 3)
        assert doctor.parse_version("no digits here") is None


# ---------------------------------------------------------------------------
# agent drift
# ---------------------------------------------------------------------------


class FakeCompose:
    """A stand-in for examples/roles-and-personas/compose.py."""

    AGENT_SETUP = {
        "claude": {
            "command": ["/opt/claude/claude"],
            "mounts": [("/host/bin/claude", "/opt/claude/claude", "ro")],
        },
        "opencode": {
            "command": ["/opt/opencode/opencode"],
            "mounts": [("/host/oc/bin", "/opt/opencode", "ro")],
        },
        "pi": {"command": [], "mounts": []},
    }

    def __init__(self, pi_package: Path | None = None):
        self._pi = pi_package

    def find_pi_package(self):
        return self._pi


def _fake_which(mapping: dict[str, str]):
    return lambda name: mapping.get(name)


def _version_stub(versions: dict[str, str]):
    def run(argv):
        return versions.get(argv[0])

    return run


class TestAgentDrift:
    def test_drift_warns_with_incident_wording(self, tmp_path):
        fake = FakeCompose()
        which = _fake_which({"claude": "/usr/bin/claude"})
        versions = _version_stub(
            {"/host/bin/claude": "0.75.5", "/usr/bin/claude": "0.85.1"}
        )
        result = doctor.check_agent_drift(
            "claude", fake, which=which, version_of=versions, exists=lambda p: True
        )
        assert result.status == "warn"
        assert result.detail == (
            "claude configured at /host/bin/claude is 0.75.5 but `claude` on PATH "
            "is 0.85.1 \u2014 containers will run the old one"
        )

    def test_matching_versions_ok(self, tmp_path):
        fake = FakeCompose()
        which = _fake_which({"claude": "/usr/bin/claude"})
        versions = _version_stub(
            {"/host/bin/claude": "1.2.3", "/usr/bin/claude": "1.2.3"}
        )
        result = doctor.check_agent_drift(
            "claude", fake, which=which, version_of=versions, exists=lambda p: True
        )
        assert result.status == "ok"

    def test_path_binary_missing_fails(self):
        fake = FakeCompose()
        result = doctor.check_agent_drift(
            "claude",
            fake,
            which=_fake_which({}),
            version_of=_version_stub({}),
            exists=lambda p: True,
        )
        assert result.status == "fail"
        assert "not found on PATH" in result.detail

    def test_configured_install_missing_fails(self):
        fake = FakeCompose()
        which = _fake_which({"claude": "/usr/bin/claude"})
        result = doctor.check_agent_drift(
            "claude", fake, which=which, version_of=_version_stub({})
        )
        assert result.status == "fail"
        assert "does not exist" in result.detail

    def test_pi_drift_via_package_json(self, tmp_path):
        package = tmp_path / "pi-package"
        package.mkdir()
        (package / "package.json").write_text(json.dumps({"version": "0.75.5"}))
        fake = FakeCompose(pi_package=package)
        which = _fake_which({"pi": "/usr/bin/pi"})
        versions = _version_stub({"/usr/bin/pi": "0.85.1"})
        result = doctor.check_agent_drift("pi", fake, which=which, version_of=versions)
        assert result.status == "warn"
        assert "pi configured at" in result.detail
        assert "0.75.5" in result.detail and "0.85.1" in result.detail

    def test_pi_package_missing_fails(self):
        fake = FakeCompose(pi_package=None)
        result = doctor.check_agent_drift(
            "pi", fake, which=_fake_which({}), version_of=_version_stub({})
        )
        assert result.status == "fail"

    def test_opencode_resolves_through_mount_dir(self):
        # command /opt/opencode/opencode, mount src is the *directory*
        # /host/oc/bin -> configured binary is /host/oc/bin/opencode.
        fake = FakeCompose()
        which = _fake_which({"opencode": "/usr/bin/opencode"})
        versions = _version_stub(
            {"/host/oc/bin/opencode": "1.0.0", "/usr/bin/opencode": "1.0.0"}
        )
        result = doctor.check_agent_drift(
            "opencode", fake, which=which, version_of=versions, exists=lambda p: True
        )
        assert result.status == "ok"


# ---------------------------------------------------------------------------
# per-config checks
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, raw: dict):
    return load_config_data(raw, base_dir=tmp_path)


class TestConfigChecks:
    def test_command_path_via_mount(self, tmp_path):
        pkg = tmp_path / "pkg"
        (pkg / "dist").mkdir(parents=True)
        (pkg / "dist" / "cli.js").write_text("x")
        config = _config(
            tmp_path,
            {
                "name": "t",
                "runtime": {"command": ["node", "/opt/pi/dist/cli.js"]},
                "mounts": [{"src": str(pkg), "dest": "/opt/pi", "mode": "ro"}],
            },
        )
        results = doctor.check_command_paths(config)
        assert all(r.status == "ok" for r in results), [r.detail for r in results]

    def test_command_path_missing_fails(self, tmp_path):
        config = _config(
            tmp_path,
            {"name": "t", "runtime": {"command": ["/no/such/binary"]}},
        )
        results = doctor.check_command_paths(config)
        assert [r.status for r in results] == ["fail"]

    def test_missing_mount_fails(self, tmp_path):
        config = _config(
            tmp_path,
            {
                "name": "t",
                "mounts": [{"src": str(tmp_path / "gone"), "dest": "/x", "mode": "ro"}],
            },
        )
        results = doctor.check_mounts(config)
        assert [r.status for r in results] == ["fail"]

    def test_copy_mount_must_be_readable(self, tmp_path):
        src = tmp_path / "secret"
        src.mkdir()
        src.chmod(0o000)
        config = _config(
            tmp_path,
            {
                "name": "t",
                "mounts": [{"src": str(src), "dest": "/x", "mode": "copy"}],
            },
        )
        try:
            results = doctor.check_mounts(config)
            assert [r.status for r in results] == ["fail"]
        finally:
            src.chmod(0o755)

    def test_missing_file_src_fails(self, tmp_path):
        config = _config(
            tmp_path,
            {
                "name": "t",
                "files": [{"src": "house.md", "dest": "/work/CLAUDE.md"}],
            },
        )
        results = doctor.check_files(config)
        assert [r.status for r in results] == ["fail"]

    def test_missing_env_warns(self, tmp_path):
        config = _config(
            tmp_path,
            {"name": "t", "runtime": {"env_from_host": ["DEFINITELY_NOT_SET_XYZ"]}},
        )
        results = doctor.check_env(config, environ={})
        assert [r.status for r in results] == ["warn"]
        assert (
            "containers will start without DEFINITELY_NOT_SET_XYZ" in results[0].detail
        )

    def test_present_env_ok(self, tmp_path):
        config = _config(
            tmp_path,
            {"name": "t", "runtime": {"env_from_host": ["HOME"]}},
        )
        results = doctor.check_env(config, environ={"HOME": "/home/x"})
        assert [r.status for r in results] == ["ok"]


# ---------------------------------------------------------------------------
# worktree checks
# ---------------------------------------------------------------------------


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args, cwd=repo):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "f.txt").write_text("hi")
    git("add", ".")
    git("commit", "-m", "init")
    return repo


class TestWorktreeChecks:
    def test_base_resolves_ok(self, git_repo):
        config = load_config_data(
            {
                "name": "t",
                "mounts": [
                    {
                        "src": str(git_repo),
                        "dest": "/work",
                        "mode": "worktree",
                        "branch": "capwrap/t",
                        "base": "main",
                    }
                ],
            },
            base_dir=git_repo.parent,
        )
        results = doctor.check_worktrees(config)
        assert all(r.status == "ok" for r in results), [r.detail for r in results]

    def test_bad_base_fails(self, git_repo):
        config = load_config_data(
            {
                "name": "t",
                "mounts": [
                    {
                        "src": str(git_repo),
                        "dest": "/work",
                        "mode": "worktree",
                        "branch": "capwrap/t",
                        "base": "no-such-branch",
                    }
                ],
            },
            base_dir=git_repo.parent,
        )
        results = doctor.check_worktrees(config)
        assert any(
            r.status == "fail" and "does not resolve" in r.detail for r in results
        )

    def test_branch_taken_elsewhere_warns(self, git_repo):
        subprocess.run(
            ["git", "worktree", "add", str(git_repo.parent / "wt"), "-b", "capwrap/t"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )
        config = load_config_data(
            {
                "name": "t",
                "mounts": [
                    {
                        "src": str(git_repo),
                        "dest": "/work",
                        "mode": "worktree",
                        "branch": "capwrap/t",
                        "base": "main",
                    }
                ],
            },
            base_dir=git_repo.parent,
        )
        results = doctor.check_worktrees(config)
        warns = [r for r in results if r.status == "warn"]
        assert len(warns) == 1
        assert (
            f"branch capwrap/t is checked out at {git_repo.parent / 'wt'}; "
            "spawn will fail until it is released"
        ) in warns[0].detail


# ---------------------------------------------------------------------------
# daemon + exit codes
# ---------------------------------------------------------------------------


class TestDaemonAndExit:
    def test_no_daemon_is_info(self):
        # port 1 on loopback: nothing listens there, and no network leaves the host
        result = doctor.check_daemon(port=1, timeout=0.5)
        assert result.status == "info"

    def test_exit_code_warn_is_zero(self):
        assert doctor.exit_code([doctor.Result("a", "warn", "x")]) == 0
        assert doctor.exit_code([doctor.Result("a", "info", "x")]) == 0
        assert doctor.exit_code([doctor.Result("a", "ok", "x")]) == 0

    def test_exit_code_fail_is_one(self):
        assert (
            doctor.exit_code([doctor.Result("a", "ok"), doctor.Result("b", "fail")])
            == 1
        )

    def test_render_lines(self):
        text = doctor.render(
            [("environment", [doctor.Result("agent claude", "warn", "drifted")])],
            color=False,
        )
        assert "WARN" in text and "agent claude" in text and "drifted" in text
