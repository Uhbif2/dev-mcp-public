"""Immutable content-addressed trees for server-side gate execution.

A snapshot contains a canonical manifest and its exact file bytes. The manifest
digest is the subject_version and snapshot_id. No gate reads the source worktree.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Sequence

_ID = re.compile(r"[a-f0-9]{64}\Z")
_SKIP = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"})
_MAX_FILES = 4000
_MAX_BYTES = 128 * 1024 * 1024


class SnapshotError(ValueError):
    """Invalid, corrupted, or no longer available snapshot."""


def _is_link(path: Path) -> bool:
    """Reject symlinks, Windows junctions and unrecognized NTFS reparse points."""
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        return True
    if os.name == "nt":
        try:
            # Path.is_junction is not available before Python 3.12.
            return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        except FileNotFoundError:
            return False
    return False


def _json(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _relative(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name:
        raise SnapshotError("invalid relative snapshot path")
    p = PurePosixPath(name)
    if p.is_absolute() or any(part in {"", ".", ".."} or ":" in part for part in p.parts):
        raise SnapshotError("snapshot path traversal")
    if str(p) != name:
        raise SnapshotError("noncanonical snapshot path")
    return name


def _restricted(name: str) -> bool:
    """Gate trees contain code, never local secrets, credentials or build caches."""
    for part in PurePosixPath(name).parts:
        low = part.casefold()
        if (low in _SKIP or low in {".env", "keys", "credentials", "secrets",
                                   "id_rsa", "id_ed25519", ".ssh"} or
                low.startswith(".env.") or low.endswith((".pem", ".p12", ".pfx", ".key")) or
                "password" in low or "credential" in low or "secret" in low):
            return True
    return False


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: str
    subject_version: str
    file_count: int
    byte_count: int


class SnapshotStore:
    """A server-owned store. Objects are immutable; every read verifies hashes."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _object(self, snapshot_id: str) -> Path:
        if not isinstance(snapshot_id, str) or _ID.fullmatch(snapshot_id) is None:
            raise SnapshotError("invalid snapshot_id")
        return self.root / snapshot_id

    @staticmethod
    def _safe_source(source: Path, name: str) -> Path:
        rel = _relative(name)
        if _restricted(rel):
            raise SnapshotError("secret or excluded snapshot path")
        path = source
        for part in PurePosixPath(rel).parts:
            path = path / part
            if _is_link(path):
                raise SnapshotError("symlink or junction in snapshot source")
        if not path.is_file():
            raise SnapshotError("missing or non-regular source file")
        return path

    def manifest(self, snapshot_id: str) -> dict:
        """Validate the content-addressed manifest without rereading every blob.

        A base object was verified when captured. Every reused blob is also
        checked for regular-file type and size before linking it into a new
        immutable object; verify() remains the exhaustive integrity check.
        """
        obj = self._object(snapshot_id)
        if _is_link(obj) or _is_link(obj / "manifest.json"):
            raise SnapshotError("snapshot object symlink")
        try:
            raw = (obj / "manifest.json").read_bytes()
            value = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise SnapshotError("snapshot manifest unavailable") from exc
        if (not isinstance(value, dict) or value.get("schema") != 1
                or not isinstance(value.get("files"), list)
                or _json(value) != raw
                or hashlib.sha256(raw).hexdigest() != snapshot_id):
            raise SnapshotError("invalid snapshot manifest")
        seen = set()
        for entry in value["files"]:
            if (not isinstance(entry, dict)
                    or set(entry) != {"path", "sha256", "size"}
                    or not isinstance(entry["size"], int)
                    or entry["size"] < 0
                    or not isinstance(entry["sha256"], str)
                    or _ID.fullmatch(entry["sha256"]) is None):
                raise SnapshotError("invalid snapshot entry")
            name = _relative(entry["path"])
            if _restricted(name) or name in seen:
                raise SnapshotError("invalid snapshot path")
            seen.add(name)
        return value

    def create_incremental(
        self, source: str | Path, include: Sequence[str], *,
        base_snapshot: str,
        base_versions: Mapping[str, Sequence[int]],
        current_versions: Mapping[str, Sequence[int]] | None = None,
        volatile_fallback: Callable[[str], bytes | None] | None = None,
    ) -> SnapshotRef:
        """Reuse unchanged immutable blobs; read/hash only changed source files.

        The metadata hint is a performance hint, never a source for hashes:
        hashes come from the previously content-addressed manifest. A changed
        or missing file is copied from the live tree under the same stability
        check used by create(). The stage is atomically published.
        """
        source = Path(source)
        if not source.is_dir() or _is_link(source):
            raise SnapshotError("source must be a real directory")
        names = sorted(include)
        if not names or len(names) > _MAX_FILES or len(set(names)) != len(names):
            raise SnapshotError("empty, duplicate, or oversized snapshot")
        previous = {row["path"]: row for row in self.manifest(base_snapshot)["files"]}
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.root))
        entries = []
        total = 0
        linked_bases: list[Path] = []
        created_dirs: set[Path] = set()
        try:
            for name in names:
                # Even incremental reuse must not trust a now-junction source.
                self._safe_source(source, name)
                old = previous.get(name)
                version = base_versions.get(name)
                current = (current_versions or {}).get(name)
                if current is None:
                    path = self._safe_source(source, name)
                    st = path.stat()
                    current = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                same = (
                    old is not None and version is not None
                    and tuple(version) == tuple(current)
                    and old["size"] == current[0]
                )
                dest = stage / "tree" / name
                if dest.parent not in created_dirs:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    created_dirs.add(dest.parent)
                if same:
                    base_file = self._object(base_snapshot) / "tree"
                    if _is_link(base_file):
                        raise SnapshotError("linked base snapshot tree")
                    for part in PurePosixPath(name).parts:
                        base_file = base_file / part
                        if _is_link(base_file):
                            raise SnapshotError("linked base snapshot file")
                    if not base_file.is_file():
                        raise SnapshotError("missing or linked base snapshot file")
                    if base_file.stat().st_size != old["size"]:
                        raise SnapshotError("base snapshot size mismatch")
                    try:
                        os.link(base_file, dest)
                        linked_bases.append(base_file)
                    except OSError:
                        shutil.copyfile(base_file, dest)
                    entry = dict(old)
                else:
                    data = None
                    for attempt in range(6):
                        try:
                            path = self._safe_source(source, name)
                            before = path.stat()
                            if before.st_size > _MAX_BYTES:
                                raise SnapshotError("snapshot exceeds byte limit")
                            with path.open("rb") as stream:
                                candidate = stream.read(before.st_size + 1)
                            after = path.stat()
                            if (before.st_size == after.st_size == len(candidate)
                                    and before.st_mtime_ns == after.st_mtime_ns
                                    and before.st_ctime_ns == after.st_ctime_ns):
                                data = candidate
                                break
                        except (FileNotFoundError, PermissionError):
                            pass
                        if attempt < 5:
                            import time
                            time.sleep(0.005 * (attempt + 1))
                    if data is None and volatile_fallback is not None:
                        data = volatile_fallback(name)
                    if data is None:
                        raise SnapshotError(f"source file changed during capture: {name}")
                    entry = {"path": name, "sha256": hashlib.sha256(data).hexdigest(),
                             "size": len(data)}
                    dest.write_bytes(data)
                    dest.chmod(0o444)
                total += entry["size"]
                if total > _MAX_BYTES:
                    raise SnapshotError("snapshot exceeds byte limit")
                entries.append(entry)
            manifest = {"schema": 1, "files": entries}
            subject = hashlib.sha256(_json(manifest)).hexdigest()
            target = self._object(subject)
            if target.exists():
                if self.manifest(subject) != manifest:
                    raise SnapshotError("existing snapshot collision")
            else:
                (stage / "manifest.json").write_bytes(_json(manifest))
                (stage / "manifest.json").chmod(0o444)
                try:
                    stage.rename(target)
                except FileExistsError:
                    if self.manifest(subject) != manifest:
                        raise SnapshotError("existing snapshot collision")
            return SnapshotRef(subject, subject, len(entries), total)
        finally:
            if stage.exists():
                # On Windows a read-only hardlink cannot be unlinked. Temporarily
                # clear the shared attribute, then restore the original object's
                # read-only protection after disposing of the abandoned stage.
                def retry_readonly(func, path, exc):
                    os.chmod(path, 0o666)
                    func(path)
                try:
                    shutil.rmtree(stage, onexc=retry_readonly)
                finally:
                    for base_file in linked_bases:
                        if base_file.exists():
                            base_file.chmod(0o444)

    def create(self, source: str | Path, include: Iterable[str] | None = None, *,
               volatile_fallback: Callable[[str], bytes | None] | None = None) -> SnapshotRef:
        source = Path(source)
        if not source.is_dir() or _is_link(source):
            raise SnapshotError("source must be a real directory")
        if include is None:
            names = []
            for folder, dirs, files in os.walk(source, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not _restricted(d) and
                                 not _is_link(Path(folder) / d))
                for name in sorted(files):
                    path = Path(folder) / name
                    rel = path.relative_to(source).as_posix()
                    if not _restricted(rel):
                        names.append(rel)
        else:
            names = list(include)
        if not names or len(names) > _MAX_FILES or len(set(names)) != len(names):
            raise SnapshotError("empty, duplicate, or oversized snapshot")
        blobs: dict[str, bytes] = {}
        total = 0
        walked = include is None
        for name in sorted(names):
            if walked and not (source / name).is_file():
                # Vanished between the walk and the read: the live tree is shared with
                # agents and the server moves tz-drafts (13.10.2026, TZ-330 bootstrap).
                continue
            # A busy shared project can rewrite an unrelated file while a bootstrap
            # captures it. Retry THAT file, not the entire multi-thousand-file tree.
            # The caller may supply immutable Git bytes for volatile non-owned inputs.
            data = None
            for attempt in range(6):
                try:
                    path = self._safe_source(source, name)
                    before = path.stat()
                    if before.st_size > _MAX_BYTES:
                        raise SnapshotError("snapshot exceeds byte limit")
                    # Bound the read to the observed size, not _MAX_BYTES per file.
                    with path.open("rb") as stream:
                        candidate = stream.read(before.st_size + 1)
                    after = path.stat()
                    if (before.st_size == after.st_size == len(candidate)
                            and before.st_mtime_ns == after.st_mtime_ns
                            and before.st_ctime_ns == after.st_ctime_ns):
                        data = candidate
                        break
                except (FileNotFoundError, PermissionError):
                    pass  # Concurrent atomic replace; recheck the exact path.
                if attempt < 5:
                    import time
                    time.sleep(0.005 * (attempt + 1))
            if data is None and volatile_fallback is not None:
                data = volatile_fallback(name)
            if data is None:
                raise SnapshotError(f"source file changed during capture: {name}")
            total += len(data)
            if total > _MAX_BYTES:
                raise SnapshotError("snapshot exceeds byte limit")
            blobs[name] = data
        manifest = {"schema": 1, "files": [
            {"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            for name, data in blobs.items()
        ]}
        subject = hashlib.sha256(_json(manifest)).hexdigest()
        target = self._object(subject)
        if target.exists():
            ref = self.verify(subject)
            if ref.file_count != len(blobs):
                raise SnapshotError("existing snapshot collision")
            return ref
        stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.root))
        try:
            tree = stage / "tree"
            for name, data in blobs.items():
                out = tree.joinpath(*PurePosixPath(name).parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                out.chmod(0o444)
            (stage / "manifest.json").write_bytes(_json(manifest))
            (stage / "manifest.json").chmod(0o444)
            try:
                stage.rename(target)
            except FileExistsError:
                self.verify(subject)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return self.verify(subject)

    def verify(self, snapshot_id: str) -> SnapshotRef:
        target = self._object(snapshot_id)
        if _is_link(target) or _is_link(target / "manifest.json"):
            raise SnapshotError("snapshot object symlink")
        try:
            raw = (target / "manifest.json").read_bytes()
            manifest = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise SnapshotError("snapshot manifest unavailable") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != 1:
            raise SnapshotError("invalid snapshot schema")
        if _json(manifest) != raw or hashlib.sha256(raw).hexdigest() != snapshot_id:
            raise SnapshotError("snapshot manifest digest mismatch")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise SnapshotError("empty snapshot")
        expected: set[str] = set()
        expected_dirs: set[str] = set()
        total = 0
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size"}:
                raise SnapshotError("invalid file entry")
            name = _relative(entry["path"])
            if name in expected:
                raise SnapshotError("duplicate path")
            expected.add(name)
            parts = PurePosixPath(name).parts
            expected_dirs.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
            path = target / "tree"
            if _is_link(path) or not path.is_dir():
                raise SnapshotError("invalid snapshot tree")
            for part in parts:
                path = path / part
                if _is_link(path):
                    raise SnapshotError("snapshot symlink")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise SnapshotError("snapshot file missing") from exc
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise SnapshotError("snapshot file digest mismatch")
            total += len(data)
        # Path.rglob(...)/is_file() omits symlinks to directories: copytree()
        # would subsequently follow such a link and copy unmanifested data.
        # Enumerate every entry without following links, including empty dirs.
        actual: set[str] = set()
        pending = [(target / "tree", "")]
        try:
            while pending:
                folder, prefix = pending.pop()
                with os.scandir(folder) as entries:
                    for entry in entries:
                        rel = _relative(f"{prefix}/{entry.name}" if prefix else entry.name)
                        if _is_link(Path(entry.path)):
                            raise SnapshotError("snapshot symlink")
                        if entry.is_dir(follow_symlinks=False):
                            if rel not in expected_dirs:
                                raise SnapshotError("unexpected snapshot directory")
                            pending.append((Path(entry.path), rel))
                        elif entry.is_file(follow_symlinks=False):
                            actual.add(rel)
                        else:
                            raise SnapshotError("unexpected snapshot entry")
        except OSError as exc:
            raise SnapshotError("snapshot tree unreadable") from exc
        if actual != expected:
            raise SnapshotError("unexpected snapshot files")
        return SnapshotRef(snapshot_id, snapshot_id, len(files), total)

    def materialize(self, snapshot_id: str, target: str | Path) -> Path:
        """Copy to an isolated working directory; never execute in the object tree."""
        self.verify(snapshot_id)
        target = Path(target)
        if target.exists() or _is_link(target):
            raise SnapshotError("materialization target must be absent")
        source = self._object(snapshot_id)
        manifest = json.loads((source / "manifest.json").read_bytes())
        target.mkdir()
        try:
            # Never copy the directory tree wholesale: only the manifest's
            # explicitly hashed file paths are allowed into the workspace.
            for entry in manifest["files"]:
                name = _relative(entry["path"])
                path = source / "tree"
                for part in PurePosixPath(name).parts:
                    path = path / part
                    if _is_link(path):
                        raise SnapshotError("snapshot symlink during materialization")
                data = path.read_bytes()
                if (len(data) != entry["size"] or
                        hashlib.sha256(data).hexdigest() != entry["sha256"]):
                    raise SnapshotError("snapshot changed during materialization")
                output = target.joinpath(*PurePosixPath(name).parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
                output.chmod(0o644)
            self.verify(snapshot_id)
        except Exception:
            shutil.rmtree(target)
            raise
        return target
