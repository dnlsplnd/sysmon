"""A fake ``/proc`` and ``/sys`` for the collectors to read.

Every collector reaches the kernel through ``util.read_text``, ``read_int`` and
``listdir``, which each module imports *by name*. Redirection therefore rebinds
those names inside the collector's own namespace: patching ``sysmon.util`` would
not touch the references the modules already hold.

Two collectors also call ``os`` directly -- gpu walks ``/proc`` and reads
symlinks, process counts ``/proc`` entries -- so they get an ``os`` proxy with
the same rerouting. Between the two, a patched module cannot reach the real
kernel by accident, which is the point: these tests must describe the fixture,
not the machine they happen to run on.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sysmon import util


class _OsProxy:
    """``os``, with the absolute paths a collector builds rerouted into the tree."""

    def __init__(self, tree: "KernelTree") -> None:
        self._tree = tree

    def listdir(self, path: str) -> list[str]:
        return os.listdir(self._tree.resolve(path))

    def readlink(self, path: str) -> str:
        return os.readlink(self._tree.resolve(path))

    def cpu_count(self) -> int:
        return self._tree.cpu_count

    def __getattr__(self, name: str):
        # os.path and anything else a collector might reach for is untouched;
        # only the calls that take a kernel path are rerouted.
        return getattr(os, name)


class KernelTree:
    """A directory tree standing in for the kernel's, plus the patching to use it."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.cpu_count = 4
        root.mkdir(parents=True, exist_ok=True)
        # These two always exist on a running kernel, and collectors are
        # entitled to assume so -- the /proc walk does not guard its readdir.
        (root / "proc").mkdir(exist_ok=True)
        (root / "sys").mkdir(exist_ok=True)

    # ------------------------------------------------------------ building it

    def resolve(self, path: str) -> str:
        """Map an absolute kernel path onto its place in the tree."""
        return str(self.root / str(path).lstrip("/"))

    def write(self, path: str, content: str = "") -> Path:
        """Create a file, adding the trailing newline sysfs files really have."""
        target = Path(self.resolve(path))
        target.parent.mkdir(parents=True, exist_ok=True)
        if content and not content.endswith("\n"):
            content += "\n"
        target.write_text(content)
        return target

    def mkdir(self, path: str) -> Path:
        target = Path(self.resolve(path))
        target.mkdir(parents=True, exist_ok=True)
        return target

    def symlink(self, path: str, target: str) -> Path:
        """Create a symlink holding ``target`` verbatim.

        The target need not exist: collectors read these links for their
        *text* -- ``basename(readlink(card0/device/driver))`` is how the driver
        name is discovered -- and only follow them when the fixture provides
        something real to follow.
        """
        link = Path(self.resolve(path))
        link.parent.mkdir(parents=True, exist_ok=True)
        # Idempotent, so a test can restate a process's descriptors on a later
        # tick the same way it stated them on the first.
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)
        return link

    def write_many(self, base: str, files: dict[str, str]) -> None:
        """Write a directory of small sysfs files in one call."""
        for name, content in files.items():
            self.write(f"{base}/{name}", content)

    # ------------------------------------------------------------- patching it

    def patch(self, *modules) -> None:
        """Point a collector module's filesystem helpers at this tree."""
        for module in modules:
            for name, function in (
                ("read_text", self._read_text),
                ("read_int", self._read_int),
                ("read_float", self._read_float),
                ("listdir", self._listdir),
            ):
                if hasattr(module, name):
                    self.monkeypatch.setattr(module, name, function)
            if hasattr(module, "os"):
                self.monkeypatch.setattr(module, "os", _OsProxy(self))

    def _read_text(self, path, default=None):
        return util.read_text(self.resolve(path), default)

    def _read_int(self, path, default=None):
        return util.read_int(self.resolve(path), default)

    def _read_float(self, path, default=None):
        return util.read_float(self.resolve(path), default)

    def _listdir(self, path):
        return util.listdir(self.resolve(path))


@pytest.fixture
def kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> KernelTree:
    """An empty fake kernel filesystem. Populate it, then ``patch`` the module."""
    return KernelTree(tmp_path / "root", monkeypatch)
