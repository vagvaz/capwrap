"""Host probing, and how capwrap chooses a bubblewrap.

The interesting behaviour is the choice itself. A host can carry more than one
bwrap and they are not interchangeable: where an LSM restricts unprivileged user
namespaces, the exemption attaches *by path* and the distribution ships it
attached to its own binary. Picking the first one on PATH and reporting failure
is what made an AppArmor profile look like a prerequisite; trying each of them
is what removed it.
"""

from __future__ import annotations

import os

import pytest

from capwrap.runtime import probe


@pytest.fixture(autouse=True)
def only_our_fakes(monkeypatch):
    """Ignore the host's own bubblewrap, so these test the ordering rules only."""
    monkeypatch.setattr(probe, "PACKAGED_BWRAP", ())
    monkeypatch.delenv("CAPWRAP_BWRAP", raising=False)


def fake_bwrap(directory, name: str = "bwrap"):
    """An executable file that stands in for a bwrap binary."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


# ==========================================================================
# candidate discovery
# ==========================================================================


def test_an_explicit_override_wins_outright(tmp_path, monkeypatch):
    pinned = fake_bwrap(tmp_path / "pinned")
    other = fake_bwrap(tmp_path / "other")
    monkeypatch.setenv("CAPWRAP_BWRAP", str(pinned))
    monkeypatch.setenv("PATH", str(other.parent))

    assert probe.bwrap_candidates() == [str(pinned)]


def test_an_override_that_is_not_there_yields_nothing(tmp_path, monkeypatch):
    """Better to say 'no bwrap' than to quietly use a different one than pinned."""
    monkeypatch.setenv("CAPWRAP_BWRAP", str(tmp_path / "absent"))
    assert probe.bwrap_candidates() == []


def test_every_bwrap_on_path_is_a_candidate(tmp_path, monkeypatch):
    first = fake_bwrap(tmp_path / "one")
    second = fake_bwrap(tmp_path / "two")
    monkeypatch.delenv("CAPWRAP_BWRAP", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(first.parent), str(second.parent)]))

    assert probe.bwrap_candidates() == [str(first), str(second)]


def test_the_same_binary_reached_twice_is_probed_once(tmp_path, monkeypatch):
    """Symlink farms are normal; probing through both costs a whole doctor run."""
    real = fake_bwrap(tmp_path / "real")
    link_dir = tmp_path / "link"
    link_dir.mkdir()
    (link_dir / "bwrap").symlink_to(real)

    monkeypatch.delenv("CAPWRAP_BWRAP", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(real.parent), str(link_dir)]))
    assert probe.bwrap_candidates() == [str(real)]


def test_a_non_executable_file_is_not_a_candidate(tmp_path, monkeypatch):
    directory = tmp_path / "bin"
    directory.mkdir()
    (directory / "bwrap").write_text("not a program\n")
    monkeypatch.delenv("CAPWRAP_BWRAP", raising=False)
    monkeypatch.setenv("PATH", str(directory))
    assert probe.bwrap_candidates() == []


# ==========================================================================
# choosing one that works
# ==========================================================================


def test_a_bwrap_that_cannot_build_a_namespace_is_passed_over(tmp_path, monkeypatch):
    """The whole point: the first candidate is not necessarily the usable one.

    This is the shape of the AppArmor case -- a nix-store bwrap that is confined
    rather than exempted, beside a packaged one that works.
    """
    broken = fake_bwrap(tmp_path / "broken")
    working = fake_bwrap(tmp_path / "working")
    monkeypatch.delenv("CAPWRAP_BWRAP", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(broken.parent), str(working.parent)]))

    def only_the_second_works(path):
        if path == str(working):
            return True, "namespace + mounts OK"
        return False, "bwrap: setting up uid map: Permission denied"

    monkeypatch.setattr(probe, "_bwrap_builds_a_namespace", only_the_second_works)

    path, detail = probe.find_working_bwrap()
    assert path == str(working)

    check = probe.check_bwrap_works()
    assert check.ok and check.path == str(working)
    assert "skipped 1" in check.detail


def test_a_host_where_none_of_them_work_reports_every_attempt(tmp_path, monkeypatch):
    first = fake_bwrap(tmp_path / "one")
    second = fake_bwrap(tmp_path / "two")
    monkeypatch.delenv("CAPWRAP_BWRAP", raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(first.parent), str(second.parent)]))
    monkeypatch.setattr(
        probe, "_bwrap_builds_a_namespace",
        lambda path: (False, "setting up uid map: Permission denied"),
    )

    check = probe.check_bwrap_works()
    assert not check.ok and check.path == ""
    assert str(first) in check.detail and str(second) in check.detail


def test_the_hint_names_the_package_before_the_apparmor_profile(tmp_path, monkeypatch):
    """Installing the distribution's bubblewrap needs no capwrap-specific policy.

    capwrap prefers /usr/bin/bwrap once it exists, so on a restricted host that
    is the fix that involves no hand-written AppArmor profile at all -- and it
    should be the one the hint leads with.
    """
    monkeypatch.setattr(probe, "_apparmor_restricts_userns", lambda: True)
    hint = probe._userns_hint([str(tmp_path / "nix" / "bwrap")])
    assert "apt install bubblewrap" in hint
    assert hint.index("apt install") < hint.index("install-apparmor-profile")


def test_no_apparmor_means_the_hint_does_not_blame_it(monkeypatch):
    monkeypatch.setattr(probe, "_apparmor_restricts_userns", lambda: False)
    assert "apparmor" in probe._userns_hint(["/usr/bin/bwrap"]).lower()
    assert "install" not in probe._userns_hint(["/usr/bin/bwrap"])


# ==========================================================================
# the probe's own sandbox
# ==========================================================================


def test_symlinked_lib_dirs_are_recreated_not_bound_through():
    """Merged-/usr hosts symlink /lib64, and that is where the ELF interpreter is
    looked up. Binding its target at /lib instead leaves every dynamically linked
    binary in the probe sandbox dying with a bare 'No such file or directory' --
    which reads as a broken host rather than a broken probe.
    """
    placed = {}
    args = probe._base_binds()
    i = 0
    while i < len(args):
        if args[i] in ("--ro-bind", "--symlink"):
            placed[args[i + 2]] = args[i]
            i += 3
        else:
            i += 2 if args[i] in ("--proc", "--dev") else 1

    for path in ("/usr", "/lib", "/lib64", "/bin"):
        if not os.path.exists(path):
            continue
        assert path in placed, f"{path} exists here but never reaches the sandbox"
        assert placed[path] == ("--symlink" if os.path.islink(path) else "--ro-bind")


def test_the_probe_sandbox_places_usr_before_anything_under_it():
    """bwrap applies mounts in order, so /usr has to land before /usr/local."""
    args = probe._base_binds()
    assert args.index("/usr") < len(args)


@pytest.mark.sandbox
def test_the_probe_sandbox_can_run_a_host_binary(require_sandbox):
    """The check the rest of `doctor` is built on, exercised for real."""
    assert require_sandbox.bwrap, "a working bwrap should have been found"
