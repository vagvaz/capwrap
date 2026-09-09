"""What a container produced, read from the host side.

Two views of an agent's output, neither of which needs the sandbox to be
running (the worktree, home and shared directories live under the container's
state dir on the host):

* the diff that will be merged -- ``git diff <base>...<branch>`` over the
  container's worktree mount; and
* everything outside git -- uncommitted worktree files, the container's home,
  and its shared dir -- listed for browsing and copied out on request.

Every path a caller supplies goes through `resolve_container_path`, which
refuses traversal (`..`) and refuses to leave the container's own areas, so
neither the CLI nor the web endpoints can be talked into reading anything the
container's mounts do not already expose.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .errors import CapwrapError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ContainerConfig, MountSpec
    from .paths import ContainerPaths

#: Unified diff and --stat payloads are capped so one huge diff cannot take
#: down a browser tab or a console.
DIFF_CAP = 200 * 1024
#: Inline file previews are capped smaller: they are rendered into the page.
CONTENT_CAP = 100 * 1024
#: How far into a file we sniff for a null byte before decoding it as text.
SNIFF_BYTES = 8 * 1024
#: Entries per listing before we cut it off and say so.
LISTING_CAP = 200

#: Extensions that are never worth previewing as text.
BINARY_EXTENSIONS = frozenset(
    {
        ".7z",
        ".a",
        ".bin",
        ".bz2",
        ".class",
        ".db",
        ".dll",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".mp3",
        ".mp4",
        ".o",
        ".pdf",
        ".png",
        ".pyc",
        ".so",
        ".sqlite",
        ".tar",
        ".ttf",
        ".wasm",
        ".webp",
        ".woff",
        ".woff2",
        ".xz",
        ".zip",
    }
)

#: Sandbox plumbing that is capwrap's, not the agent's output. Never listed,
#: never served.
PLUMBING_FILES = frozenset(
    {"agent.sock", "proxy.sock", "signing.key", "daemon.sock", "grants.json"}
)
#: State directories that hold capwrap's own bookkeeping.
PLUMBING_DIRS = frozenset({"worktrees", "copies", "upper", "work", "merged", "files"})
#: Anything ending in one of these is a database or a socket, not content.
PLUMBING_SUFFIXES = (".db", ".sock", ".key")


class ContainerFileError(CapwrapError):
    """A path request refused, or a host-side read that could not happen."""


# --------------------------------------------------------------------------
# path resolution -- the one gate every access goes through
# --------------------------------------------------------------------------


def worktree_mount(config: "ContainerConfig") -> "MountSpec | None":
    """The container's worktree mount, if it has one."""
    return next((m for m in config.mounts if m.mode == "worktree"), None)


def areas(config: "ContainerConfig", paths: "ContainerPaths") -> list[tuple[str, Path]]:
    """The host-side roots a container's files may live in, in lookup order."""
    out: list[tuple[str, Path]] = []
    mount = worktree_mount(config)
    if mount is not None:
        out.append(("worktree", paths.worktree(mount.dest)))
    out.append(("home", paths.home))
    out.append(("shared", paths.shared))
    return out


def resolve_container_path(
    config: "ContainerConfig", paths: "ContainerPaths", raw: str
) -> tuple[str, Path]:
    """Resolve a container-relative path to (area, host path).

    The path is interpreted relative to each of the container's areas in turn
    (worktree, home, shared) and the first existing match wins.  Refused:

    * anything containing a ``..`` component -- there is no reason for one,
      and it is the classic escape;
    * a path that resolves outside its area through a symlink.
    """
    text = (raw or "").strip()
    if not text:
        raise ContainerFileError("empty path")
    parts = [p for p in PurePosixPath(text).parts if p not in ("/", ".")]
    if not parts:
        raise ContainerFileError(f"path {raw!r} does not name a file")
    if any(p == ".." for p in parts):
        raise ContainerFileError(
            f"path {raw!r} must not contain '..': it would leave the "
            "container's worktree, home and shared directories"
        )
    relative = PurePosixPath(*parts)
    for area, root in areas(config, paths):
        if not root.is_dir():
            continue
        candidate = (root / relative).resolve()
        if not candidate.is_relative_to(root.resolve()):
            continue  # a symlink pointing out; not this area's file
        if candidate.exists():
            return area, candidate
    raise ContainerFileError(
        f"{raw!r} not found in the container's worktree, home or shared dir"
    )


def _is_plumbing(relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return any(
        p in PLUMBING_FILES or p in PLUMBING_DIRS or p.endswith(PLUMBING_SUFFIXES)
        for p in parts
    )


# --------------------------------------------------------------------------
# the diff that will be merged
# --------------------------------------------------------------------------


def _cap(text: str, cap: int) -> tuple[str, bool]:
    if len(text.encode()) <= cap:
        return text, False
    return text[:cap], True


def _git(worktree: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ContainerFileError(f"git {args[0]} failed: {exc}") from None
    if proc.returncode != 0:
        raise ContainerFileError(
            f"git {args[0]} failed: {proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout


def container_diff(config: "ContainerConfig", paths: "ContainerPaths") -> dict:
    """``git diff <base>...<branch>`` over the container's worktree mount."""
    mount = worktree_mount(config)
    if mount is None:
        raise ContainerFileError(f"{config.name} has no worktree mount")
    worktree = paths.worktree(mount.dest)
    if not worktree.is_dir():
        raise ContainerFileError(
            f"worktree {worktree} does not exist; has {config.name} ever started?"
        )
    base = mount.base
    branch = mount.branch or f"capwrap/{config.name}"
    rng = f"{base}...{branch}"
    stat_text, stat_truncated = _cap(_git(worktree, "diff", "--stat", rng), DIFF_CAP)
    diff_text, diff_truncated = _cap(_git(worktree, "diff", rng), DIFF_CAP)
    return {
        "base": base,
        "branch": branch,
        "stat": stat_text,
        "diff": diff_text,
        "empty": not stat_text.strip() and not diff_text.strip(),
        "truncated": stat_truncated or diff_truncated,
    }


# --------------------------------------------------------------------------
# everything outside git
# --------------------------------------------------------------------------


def _worktree_entries(worktree: Path) -> list[dict]:
    """Untracked and uncommitted files, from ``git status --porcelain``."""
    out = _git(worktree, "status", "--porcelain")
    entries = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        status, rel = line[:2], line[3:]
        # A rename reports "old -> new"; the new name is the one on disk.
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        rel = rel.strip().strip('"')
        path = worktree / rel
        try:
            size = path.stat().st_size if path.is_file() else None
        except OSError:
            size = None
        entries.append({"path": rel, "status": status.strip(), "size": size})
    return entries


def _walk_entries(root: Path) -> list[dict]:
    """Every file under a host directory, relative paths plus sizes."""
    import os

    entries: list[dict] = []
    if not root.is_dir():
        return entries
    for current, dirnames, filenames in os.walk(root):
        here = Path(current)
        for name in filenames:
            rel = str((here / name).relative_to(root))
            if _is_plumbing(rel):
                continue
            try:
                size = (here / name).stat().st_size
            except OSError:
                size = None
            entries.append({"path": rel, "status": "", "size": size})
        # Prune plumbing directories in place so os.walk does not descend.
        dirnames[:] = [
            d for d in dirnames if not _is_plumbing(str((here / d).relative_to(root)))
        ]
        dirnames.sort()
    entries.sort(key=lambda e: e["path"])
    return entries


def list_container_files(config: "ContainerConfig", paths: "ContainerPaths") -> dict:
    """What the container produced outside git, grouped by area.

    Capped at `LISTING_CAP` entries across all groups, with a truncated flag,
    so one prolific agent cannot produce an unbounded payload.
    """
    groups: list[dict] = []
    remaining = LISTING_CAP

    def add(area: str, label: str, entries: list[dict]) -> None:
        nonlocal remaining
        taken, entries = entries[: max(0, remaining)], entries[max(0, remaining) :]
        truncated = bool(entries)
        remaining -= len(taken)
        if taken or truncated:
            groups.append(
                {
                    "area": area,
                    "label": label,
                    "entries": taken,
                    "truncated": truncated,
                }
            )

    mount = worktree_mount(config)
    if mount is not None:
        worktree = paths.worktree(mount.dest)
        if worktree.is_dir():
            add(
                "worktree",
                f"uncommitted in worktree ({mount.dest})",
                _worktree_entries(worktree),
            )
    add("home", "container home", _walk_entries(paths.home))
    add("shared", "shared dir", _walk_entries(paths.shared))
    return {"groups": groups, "truncated": remaining <= 0}


# --------------------------------------------------------------------------
# reading one file out
# --------------------------------------------------------------------------


def _single_file(config, paths, raw: str) -> tuple[str, Path]:
    area, path = resolve_container_path(config, paths, raw)
    if path.is_dir():
        raise ContainerFileError(
            f"{raw!r} is a directory; use `capwrap files` to list what is in it"
        )
    return area, path


def read_container_file(config, paths, raw: str) -> dict:
    """A text file's content, capped, with binaries refused."""
    area, path = _single_file(config, paths, raw)
    suffix = path.suffix.lower()
    if suffix in BINARY_EXTENSIONS:
        raise ContainerFileError(
            f"{raw!r} looks binary ({suffix}); copy it out with "
            "`capwrap get` instead of previewing it"
        )
    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(min(SNIFF_BYTES, CONTENT_CAP))
        data = handle.read(max(0, CONTENT_CAP - len(head)))
    if b"\0" in head:
        raise ContainerFileError(
            f"{raw!r} contains null bytes, so it is not text; copy it out "
            "with `capwrap get` instead"
        )
    read = len(head) + len(data)
    return {
        "path": raw,
        "area": area,
        "size": size,
        "truncated": read < size,
        "content": (head + data).decode("utf-8", errors="replace"),
    }


def copy_container_file(
    config, paths, raw: str, dest: Path | None = None
) -> tuple[str, Path]:
    """Copy one file out to the host. Returns (area, destination path)."""
    import shutil

    area, src = _single_file(config, paths, raw)
    target = dest if dest is not None else Path.cwd() / src.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    return area, target
