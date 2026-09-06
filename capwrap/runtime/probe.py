"""Host capability probing -- the engine behind ``capwrap doctor``.

Every check is *functional* where it can be: rather than inferring that overlay
will work from a kernel version, we mount one and see.  This host in particular
has a failure mode (AppArmor's unprivileged-userns restriction not covering a
nix-installed bwrap) that no amount of version sniffing would reveal, and whose
symptom -- ``bwrap: setting up uid map: Permission denied`` -- points nowhere
useful on its own.  So each check carries a `hint` naming the actual fix.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import state_root


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    #: What to do about it.  Only shown when the check failed.
    hint: str = ""
    #: A failed check that is not fatal (a fallback exists).
    optional: bool = False
    #: For checks that pick one of several binaries: which one they settled on.
    path: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    #: The bwrap that was found to actually work here, if any.  Everything that
    #: launches a sandbox should use this rather than re-deriving it from PATH:
    #: a host may carry several bwraps and only some of them able to build a
    #: namespace, and the difference is only visible by trying.
    bwrap: str | None = None

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def get(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    @property
    def ok(self) -> bool:
        """True when every non-optional check passed."""
        return all(c.ok or c.optional for c in self.checks)

    @property
    def overlay_backend(self) -> str | None:
        """Which overlay implementation to use: 'kernel', 'fuse', or None."""
        if (c := self.get("overlay (kernel, in userns)")) and c.ok:
            return "kernel"
        if (c := self.get("fuse-overlayfs")) and c.ok:
            return "fuse"
        return None


#: Where a distribution puts bubblewrap. Tried before PATH, because these are
#: the paths an LSM's shipped exemption policy attaches to.
PACKAGED_BWRAP = ("/usr/bin/bwrap", "/bin/bwrap", "/usr/local/bin/bwrap")


def bwrap_candidates() -> list[str]:
    """Every bubblewrap on this host, best bet first.

    A host can easily carry two: one from the distribution under ``/usr/bin``
    and one from nix or a local build somewhere else.  They are not
    interchangeable.  Where an LSM restricts unprivileged user namespaces --
    Ubuntu's ``kernel.apparmor_restrict_unprivileged_userns=1`` being the
    common case -- the policy that exempts bubblewrap attaches *by path*, and
    the distribution ships it attached to ``/usr/bin/bwrap`` only.  A nix-store
    bwrap is then confined instead of exempted and fails at its first step.

    So the packaged binary is tried first and the rest of PATH after it.  That
    ordering is the whole reason capwrap no longer needs an AppArmor profile
    installed by hand: on a restricted host, installing the distribution's
    bubblewrap package is enough, and `check_bwrap_works` will settle on it
    even when another bwrap comes first on PATH.

    ``CAPWRAP_BWRAP`` still wins outright, for pinning one deliberately.
    """
    if override := os.environ.get("CAPWRAP_BWRAP"):
        return [override] if _is_executable(override) else []

    found: list[str] = []
    seen: set[str] = set()

    def add(path: str | Path) -> None:
        path = str(path)
        if not _is_executable(path):
            return
        # Two PATH entries often reach one binary through a symlink farm;
        # probing it twice would only double the cost of `doctor`.
        real = os.path.realpath(path)
        if real in seen:
            return
        seen.add(real)
        found.append(path)

    for packaged in PACKAGED_BWRAP:
        add(packaged)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory:
            add(Path(directory) / "bwrap")
    return found


def _is_executable(path: str | Path) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def find_bwrap() -> str | None:
    """The first bubblewrap on the host, whether or not it works here.

    Presence only.  Use `find_working_bwrap` before launching anything.
    """
    candidates = bwrap_candidates()
    return candidates[0] if candidates else None


def _bwrap_builds_a_namespace(path: str) -> tuple[bool, str]:
    """Try it. The failure this catches is invisible to any static inspection."""
    try:
        proc = _run([path, "--unshare-all", *_base_binds(), "/bin/true"])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, "namespace + mounts OK"
    err = (proc.stderr or proc.stdout).strip().splitlines()
    return False, err[0] if err else f"exit {proc.returncode}"


def find_working_bwrap() -> tuple[str | None, str]:
    """The first candidate that can actually build a namespace, and why not."""
    detail = "bwrap not found"
    for path in bwrap_candidates():
        ok, detail = _bwrap_builds_a_namespace(path)
        if ok:
            return path, detail
    return None, detail


#: Top-level entries the probe sandbox needs to run a dynamically linked
#: binary.  Order matters only in that /usr must come first; the rest layer on.
_MINIMAL_ROOTS = ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32"]


def _base_binds() -> list[str]:
    """Read-only binds sufficient to run a host binary, whatever the /usr layout.

    A symlinked entry is *recreated as a symlink* rather than bound through.
    That distinction is the whole function: on a merged-/usr host, /lib64 is a
    symlink to usr/lib, and it is where the ELF interpreter is looked up.  Bind
    the target at /lib instead and every dynamically linked binary in the
    sandbox dies with a bare ``execvp: No such file or directory`` -- which
    reads exactly like a broken sandbox, and would have `doctor` condemn a host
    that is in fact perfectly fine.
    """
    args: list[str] = []
    for path in _MINIMAL_ROOTS:
        p = Path(path)
        if not p.exists():
            continue
        if p.is_symlink():
            args += ["--symlink", os.readlink(path), path]
        elif p.is_dir():
            args += ["--ro-bind", path, path]
    args += ["--proc", "/proc", "--dev", "/dev"]
    return args


def _run(argv: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def check_python() -> Check:
    v = sys.version_info
    ok = v >= (3, 11)
    return Check(
        "python >= 3.11",
        ok,
        f"{v.major}.{v.minor}.{v.micro} at {sys.executable}",
        hint="capwrap uses tomllib, which landed in 3.11",
    )


def check_userns() -> Check:
    """Are unprivileged user namespaces permitted at all?"""
    path = Path("/proc/sys/kernel/unprivileged_userns_clone")
    if path.exists():
        value = path.read_text().strip()
        if value != "1":
            return Check(
                "unprivileged user namespaces",
                False,
                f"unprivileged_userns_clone={value}",
                hint="sudo sysctl -w kernel.unprivileged_userns_clone=1",
            )
    max_ns = Path("/proc/sys/user/max_user_namespaces")
    if max_ns.exists() and max_ns.read_text().strip() == "0":
        return Check(
            "unprivileged user namespaces",
            False,
            "max_user_namespaces=0",
            hint="sudo sysctl -w user.max_user_namespaces=10000",
        )
    return Check("unprivileged user namespaces", True, "permitted")


def check_bwrap() -> Check:
    candidates = bwrap_candidates()
    if not candidates:
        return Check(
            "bwrap present",
            False,
            "not found on PATH",
            hint=(
                "apt: sudo apt install bubblewrap; "
                "nix: add `bubblewrap` to home.packages and run `home-manager switch`"
            ),
        )
    proc = _run([candidates[0], "--version"])
    version = proc.stdout.strip() or proc.stderr.strip()
    detail = f"{version} ({os.path.realpath(candidates[0])})"
    if len(candidates) > 1:
        detail += f", and {len(candidates) - 1} more to fall back on"
    return Check("bwrap present", True, detail, path=candidates[0])


def check_bwrap_works() -> Check:
    """The check that matters: can *any* bwrap here actually build a namespace?

    Every candidate is tried in turn, because the answer differs between them.
    On Ubuntu with ``kernel.apparmor_restrict_unprivileged_userns=1``, a bwrap
    outside ``/usr/bin`` -- as installed by nix -- is transitioned into the
    ``unprivileged_userns`` profile, which denies it capabilities inside its own
    namespace, and it fails writing /proc/self/uid_map.  The packaged one beside
    it works fine.  Nothing short of running them tells you which is which.
    """
    candidates = bwrap_candidates()
    if not candidates:
        return Check("bwrap can create namespaces", False, "bwrap not found")

    tried: list[str] = []
    for path in candidates:
        ok, detail = _bwrap_builds_a_namespace(path)
        if ok:
            note = f"{detail} ({path})"
            if tried:
                note += f"; skipped {len(tried)} that could not"
            return Check("bwrap can create namespaces", True, note, path=path)
        tried.append(f"{path}: {detail}")

    return Check(
        "bwrap can create namespaces",
        False,
        "; ".join(tried),
        hint=_userns_hint(candidates),
    )


def _userns_hint(candidates: list[str]) -> str:
    """What to do when no bubblewrap on the host can make a namespace."""
    if not _apparmor_restricts_userns():
        return "check `dmesg | grep apparmor` for a DENIED line"
    if not any(os.path.realpath(p).startswith("/usr/") for p in candidates):
        # The zero-privilege fix: the distribution's own package ships the
        # AppArmor policy that exempts it, attached to /usr/bin/bwrap, and
        # capwrap prefers that binary automatically once it exists.
        return (
            "AppArmor confines unprivileged user namespaces and no bubblewrap "
            "here is covered by a profile. Install the distribution's package "
            "-- `sudo apt install bubblewrap` -- and capwrap will pick it up on "
            "its own. To keep using this one instead, register it with "
            "`sudo scripts/install-apparmor-profile.sh`."
        )
    return (
        "AppArmor is restricting unprivileged user namespaces and even the "
        "packaged bwrap was refused; check that its profile is loaded "
        "(`sudo aa-status | grep bwrap`)"
    )


def _apparmor_restricts_userns() -> bool:
    path = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    return path.exists() and path.read_text().strip() == "1"


def check_kernel_overlay(bwrap: str | None = None) -> Check:
    """Can bwrap mount a kernel overlayfs inside its user namespace?

    Unprivileged overlayfs has been possible since Linux 5.11, but LSM policy can
    still block it, so this is a live mount test rather than a version check.

    Takes the bwrap that `check_bwrap_works` settled on: testing overlay with a
    binary that cannot even build a namespace would report a kernel limitation
    that is nothing of the sort.
    """
    path = bwrap or find_bwrap()
    if not path:
        return Check(
            "overlay (kernel, in userns)", False, "bwrap not found", optional=True
        )

    with tempfile.TemporaryDirectory(prefix="capwrap-probe-") as tmp:
        root = Path(tmp)
        low, up, work = root / "low", root / "up", root / "work"
        for d in (low, up, work):
            d.mkdir()
        (low / "probe").write_text("lower\n")

        proc = _run(
            [
                path,
                "--unshare-all",
                *_base_binds(),
                "--overlay-src",
                str(low),
                "--overlay",
                str(up),
                str(work),
                "/mnt",
                "/bin/sh",
                "-c",
                "cat /mnt/probe && echo upper > /mnt/written",
            ]
        )
        if proc.returncode == 0 and (up / "written").exists():
            return Check(
                "overlay (kernel, in userns)", True, "mounted and wrote to upper"
            )

        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return Check(
            "overlay (kernel, in userns)",
            False,
            detail[0] if detail else f"exit {proc.returncode}",
            hint="falling back to fuse-overlayfs; set overlay_backend='fuse'",
            optional=True,
        )


def check_fuse_overlayfs() -> Check:
    path = shutil.which("fuse-overlayfs")
    if not path:
        return Check(
            "fuse-overlayfs",
            False,
            "not found",
            hint="nix: add `fuse-overlayfs` to home.packages; apt: sudo apt install fuse-overlayfs",
            optional=True,
        )
    if not Path("/dev/fuse").exists():
        return Check(
            "fuse-overlayfs",
            False,
            "/dev/fuse missing",
            hint="sudo modprobe fuse",
            optional=True,
        )
    proc = _run([path, "--version"])
    first = (proc.stdout or proc.stderr).strip().splitlines()
    return Check("fuse-overlayfs", True, first[0] if first else path, optional=True)


def check_git() -> Check:
    path = shutil.which("git")
    if not path:
        return Check(
            "git",
            False,
            "not found",
            hint="needed for mode='worktree'; nix: add `git` to home.packages",
            optional=True,
        )
    proc = _run([path, "--version"])
    return Check("git", True, proc.stdout.strip() or path, optional=True)


def check_live_remapping() -> Check:
    """Can the daemon bind-mount into a container that is already running?

    Determined by doing it: the failure is a capability check inside the kernel,
    and nothing observable from outside predicts it. Optional, because the
    shared-directory backend covers the same ground without privilege -- less
    precisely, since it cannot alias a directory.
    """
    from . import nsmount

    ok, detail = nsmount.available()
    if ok:
        return Check("live remapping (nsmount)", True, detail, optional=True)
    return Check(
        "live remapping (nsmount)",
        False,
        detail,
        hint=(
            "falling back to the 'shared' backend, which copies into the "
            "target's /shared. For real bind mounts, run the daemon with "
            "CAP_SYS_ADMIN (e.g. a systemd unit with "
            "AmbientCapabilities=CAP_SYS_ADMIN)"
        ),
        optional=True,
    )


def check_state_dir() -> Check:
    root = state_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check(
            "state directory writable",
            False,
            f"{root}: {exc}",
            hint="set CAPWRAP_STATE to a writable location",
        )
    return Check("state directory writable", True, str(root))


def run_all() -> Report:
    """Run every check, in dependency order."""
    report = Report()
    report.add(check_python())
    report.add(check_state_dir())
    report.add(check_userns())
    report.add(check_bwrap())
    working = report.add(check_bwrap_works())
    report.bwrap = working.path or None
    report.add(check_kernel_overlay(report.bwrap))
    report.add(check_fuse_overlayfs())
    report.add(check_git())
    report.add(check_live_remapping())
    return report


def format_report(report: Report, color: bool = True) -> str:
    """Render a report for the terminal."""

    def paint(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    lines = []
    for check in report.checks:
        if check.ok:
            mark = paint("ok  ", "32")
        elif check.optional:
            mark = paint("warn", "33")
        else:
            mark = paint("FAIL", "31")
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
        if not check.ok and check.hint:
            lines.append(f"         {paint('→ ' + check.hint, '2')}")

    backend = report.overlay_backend
    lines.append("")
    if report.bwrap:
        lines.append(f"  sandbox binary: {paint(report.bwrap, '36')}")
    mapping = report.get("live remapping (nsmount)")
    if mapping is not None:
        lines.append(
            f"  mapping backend: {paint('nsmount' if mapping.ok else 'shared', '36')}"
        )
    if backend:
        lines.append(f"  overlay backend: {paint(backend, '36')}")
    else:
        lines.append(f"  overlay backend: {paint('none available', '31')}")
        lines.append("         mode='overlay' mounts will fail")

    if report.ok:
        lines.append(paint("\n  host is ready", "32"))
    else:
        lines.append(paint("\n  host is NOT ready; fix the FAIL lines above", "31"))
    return "\n".join(lines)
