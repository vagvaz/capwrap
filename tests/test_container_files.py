"""UX-6: the two views of what a container produced.

The diff that will be merged (``git diff base...branch`` over the worktree)
and everything outside git (uncommitted worktree files, home, shared), plus
the one path resolver both the CLI's `get` and the web endpoints go through.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from capwrap import container_files as cf
from capwrap.config import load_config_data
from capwrap.container_files import ContainerFileError
from capwrap.daemon import Container, Daemon


def make_container(name: str, repo: Path, tmp_path: Path) -> tuple[Daemon, Container]:
    """A registered container with a real git worktree behind its /work mount."""
    daemon = Daemon(audit_path=tmp_path / "audit.db")
    config = load_config_data(
        {
            "name": name,
            "mounts": [
                {
                    "src": str(repo),
                    "dest": "/work",
                    "mode": "worktree",
                    # A fixed base: the default "HEAD" would move with the
                    # branch and diff empty forever.
                    "base": "main",
                }
            ],
        },
        base_dir=tmp_path,
    )
    daemon.register(config)
    paths = daemon.containers[name].paths
    paths.ensure()
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "--quiet",
            "-b",
            f"capwrap/{name}",
            str(paths.worktree("/work")),
        ],
        check=True,
        capture_output=True,
    )
    return daemon, daemon.containers[name]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def worktree_repo(git_repo: Path) -> tuple[Path, None]:
    """The origin repo. The container's worktree is created by
    `make_container`, so every test gets its own branch and checkout."""
    return git_repo, None


# ---------------------------------------------------------------- the diff


def test_diff_shape_on_a_temp_repo(git_repo: Path, tmp_path: Path, state_dir: Path):
    _, container = make_container("alpha", git_repo, tmp_path)
    (Path(container.paths.worktree("/work")) / "src" / "app.py").write_text("x = 2\n")
    git("add", "-A", cwd=container.paths.worktree("/work"))
    git("commit", "--quiet", "-m", "change", cwd=container.paths.worktree("/work"))

    result = cf.container_diff(container.config, container.paths)

    assert result["base"] == "main"
    assert result["branch"] == "capwrap/alpha"
    assert "src/app.py" in result["stat"]
    assert "-x = 1" in result["diff"] and "+x = 2" in result["diff"]
    assert result["empty"] is False
    assert result["truncated"] is False


def test_diff_reports_no_changes(worktree_repo, tmp_path: Path, state_dir: Path):
    repo, _ = worktree_repo
    _, container = make_container("alpha", repo, tmp_path)

    result = cf.container_diff(container.config, container.paths)

    assert result["empty"] is True
    assert result["diff"] == ""


def test_diff_refuses_a_container_without_a_worktree(tmp_path: Path, state_dir: Path):
    daemon = Daemon(audit_path=tmp_path / "audit.db")
    config = load_config_data({"name": "plain"}, base_dir=tmp_path)
    daemon.register(config)
    container = daemon.containers["plain"]

    with pytest.raises(ContainerFileError, match="no worktree mount"):
        cf.container_diff(container.config, container.paths)


# ------------------------------------------------------------ the listing


def test_files_grouping_and_plumbing_skip(
    worktree_repo, tmp_path: Path, state_dir: Path
):
    repo, _ = worktree_repo
    _, container = make_container("alpha", repo, tmp_path)
    paths = container.paths

    (paths.worktree("/work") / "notes.txt").write_text("uncommitted\n")
    (paths.home / "settings.json").write_text("{}")
    (paths.home / "signing.key").write_text("plumbing")
    (paths.home / "cache.db").write_text("plumbing")
    (paths.shared / "inbox").mkdir(parents=True, exist_ok=True)
    (paths.shared / "inbox" / "hello.txt").write_text("hi")

    result = cf.list_container_files(container.config, paths)

    by_area = {g["area"]: g for g in result["groups"]}
    worktree_paths = [e["path"] for e in by_area["worktree"]["entries"]]
    home_paths = [e["path"] for e in by_area["home"]["entries"]]
    shared_paths = [e["path"] for e in by_area["shared"]["entries"]]

    assert "notes.txt" in worktree_paths
    assert any(e["status"] == "??" for e in by_area["worktree"]["entries"])
    assert "settings.json" in home_paths
    assert "signing.key" not in home_paths, "sandbox plumbing must not be listed"
    assert "cache.db" not in home_paths, "databases are plumbing, not content"
    assert "inbox/hello.txt" in shared_paths
    assert result["truncated"] is False


def test_files_listing_is_capped(worktree_repo, tmp_path: Path, state_dir: Path):
    repo, _ = worktree_repo
    _, container = make_container("alpha", repo, tmp_path)
    paths = container.paths
    for i in range(cf.LISTING_CAP + 20):
        (paths.home / f"f{i:04d}.txt").write_text("x")

    result = cf.list_container_files(container.config, paths)

    total = sum(len(g["entries"]) for g in result["groups"])
    assert total == cf.LISTING_CAP
    assert result["truncated"] is True


# ------------------------------------------------------------- the resolver


def test_resolver_refuses_traversal_and_absolute_escape(
    worktree_repo, tmp_path: Path, state_dir: Path
):
    repo, _ = worktree_repo
    _, container = make_container("alpha", repo, tmp_path)

    for bad in ("../signing.key", "src/../../signing.key", "/etc/passwd"):
        with pytest.raises(ContainerFileError):
            cf.resolve_container_path(container.config, container.paths, bad)


def test_resolver_resolves_against_each_area_in_order(
    worktree_repo, tmp_path: Path, state_dir: Path
):
    repo, _ = worktree_repo
    _, container = make_container("alpha", repo, tmp_path)
    (container.paths.worktree("/work") / "both.txt").write_text("worktree wins")
    (container.paths.home / "both.txt").write_text("home copy")

    area, path = cf.resolve_container_path(
        container.config, container.paths, "both.txt"
    )

    assert area == "worktree"
    assert path.read_text() == "worktree wins"

    (container.paths.home / "settings.json").write_text("{}")
    area, path = cf.resolve_container_path(
        container.config, container.paths, "settings.json"
    )
    assert area == "home"


def test_get_copies_a_file_out_and_refuses_directories(
    worktree_repo, tmp_path: Path, state_dir: Path
):
    repo, _ = worktree_repo
    _, container = make_container("alpha", repo, tmp_path)
    (container.paths.worktree("/work") / "notes.txt").write_text("take me home\n")

    area, dest = cf.copy_container_file(
        container.config,
        container.paths,
        "notes.txt",
        tmp_path / "out" / "notes.txt",
    )
    assert area == "worktree"
    assert (tmp_path / "out" / "notes.txt").read_text() == "take me home\n"

    with pytest.raises(ContainerFileError, match="directory"):
        cf.copy_container_file(
            container.config, container.paths, "src", tmp_path / "nope"
        )


# ----------------------------------------------------------------- content


def test_content_is_capped_and_binaries_refused(
    worktree_repo, tmp_path: Path, state_dir: Path
):
    repo, _ = worktree_repo
    _, container = make_container("alpha", repo, tmp_path)
    paths = container.paths
    (paths.home / "big.txt").write_text("x" * (cf.CONTENT_CAP + 5000))
    (paths.home / "logo.png").write_bytes(b"PNG" + b"\0" * 10)
    (paths.home / "nulls.txt").write_bytes(b"text\0with nulls")

    result = cf.read_container_file(container.config, paths, "big.txt")
    assert result["truncated"] is True
    assert len(result["content"]) <= cf.CONTENT_CAP

    with pytest.raises(ContainerFileError, match="binary"):
        cf.read_container_file(container.config, paths, "logo.png")
    with pytest.raises(ContainerFileError, match="null bytes"):
        cf.read_container_file(container.config, paths, "nulls.txt")


# ------------------------------------------------------- shared with the web


def test_web_endpoints_share_the_resolver(
    worktree_repo, tmp_path: Path, state_dir: Path
):
    from fastapi.testclient import TestClient

    from capwrap.web.app import create_app

    repo, _ = worktree_repo
    daemon, container = make_container("alpha", repo, tmp_path)
    (container.paths.home / "hello.txt").write_text("hi\n")
    (container.paths.worktree("/work") / "notes.txt").write_text("n\n")
    (container.paths.shared / "out.txt").write_text("s\n")
    client = TestClient(create_app(daemon))

    missing = client.get("/api/containers/ghost/files")
    assert missing.status_code == 404

    listing = client.get("/api/containers/alpha/files")
    assert listing.status_code == 200
    areas = {g["area"] for g in listing.json()["groups"]}
    assert {"worktree", "home", "shared"} <= areas

    diff = client.get("/api/containers/alpha/diff")
    assert diff.status_code == 200
    body = diff.json()
    assert set(body) >= {"base", "branch", "stat", "diff"}

    content = client.get("/api/containers/alpha/files/content?path=hello.txt")
    assert content.status_code == 200
    assert content.json()["content"] == "hi\n"

    # The same gate the CLI's `get` goes through, over HTTP.
    escape = client.get("/api/containers/alpha/files/content?path=../signing.key")
    assert escape.status_code == 400
    raw_escape = client.get("/api/containers/alpha/files/raw?path=/etc/passwd")
    assert raw_escape.status_code == 400

    raw = client.get("/api/containers/alpha/files/raw?path=hello.txt")
    assert raw.status_code == 200
    assert raw.content == b"hi\n"

    directory = client.get("/api/containers/alpha/files/raw?path=src")
    assert directory.status_code == 400


# ------------------------------------------------------------------ the CLI


def test_cli_diff_and_files_render(monkeypatch, capsys, tmp_path: Path):
    """`capwrap diff` and `capwrap files` are thin HTTP clients over the
    same container_files implementation the daemon endpoints use."""
    from capwrap import cli

    monkeypatch.setattr(
        cli,
        "_fetch_json",
        lambda args, path: {
            "base": "main",
            "branch": "capwrap/alpha",
            "stat": " src/app.py | 2 +-\n",
            "diff": "--- a/src/app.py\n+++ b/src/app.py\n",
            "empty": False,
            "truncated": False,
        },
    )
    assert cli.main(["diff", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "src/app.py" in out and "--- a/src/app.py" in out

    monkeypatch.setattr(
        cli,
        "_fetch_json",
        lambda args, path: {
            "groups": [
                {
                    "area": "home",
                    "label": "container home",
                    "entries": [{"path": "notes.txt", "status": "", "size": 12}],
                    "truncated": False,
                }
            ],
            "truncated": False,
        },
    )
    assert cli.main(["files", "alpha"]) == 0
    assert "notes.txt" in capsys.readouterr().out

    monkeypatch.setattr(
        cli,
        "_fetch_json",
        lambda args, path: {
            "base": "main",
            "branch": "capwrap/alpha",
            "stat": "",
            "diff": "",
            "empty": True,
            "truncated": False,
        },
    )
    assert cli.main(["diff", "alpha"]) == 0
    assert "no changes vs main" in capsys.readouterr().out


def test_cli_get_writes_the_file_out(monkeypatch, tmp_path: Path):
    from capwrap import cli

    monkeypatch.setattr(cli, "_fetch_bytes", lambda args, path: b"copied\n")
    dest = tmp_path / "out.txt"
    rc = cli.main(["get", "alpha", "notes.txt", "--dest", str(dest)])
    assert rc == 0
    assert dest.read_bytes() == b"copied\n"


def test_cli_get_surfaces_the_server_refusal(monkeypatch, capsys):
    from capwrap import cli
    from capwrap.errors import CapwrapError

    # What _fetch_bytes produces after parsing the daemon's 400 body.
    def refuse(args, path):
        raise CapwrapError("path must not contain '..'")

    monkeypatch.setattr(cli, "_fetch_bytes", refuse)
    assert cli.main(["get", "alpha", "../signing.key"]) == 1
    assert ".." in capsys.readouterr().err
