"""Descriptor-relative local file I/O and crash reconciliation."""
import errno
import os
import re
import stat
import sys
from contextlib import contextmanager

from pydantic import TypeAdapter, ValidationError

from .adapters import Rejected, Uncertain
from .config import ensure_private_directory
from .contracts import MAX_FILE_BYTES, FilePath, ReadFile, WriteFile, digest

PATH = TypeAdapter(FilePath)
EXECUTION_ID = re.compile(r"^[0-9a-f]{32}$")


class Workspace:
    def __init__(self, root):
        if sys.platform not in {"darwin", "linux"} or not {os.open, os.link, os.stat, os.unlink} <= os.supports_dir_fd:
            raise RuntimeError("Workspace requires local macOS or Linux descriptor-relative file operations")
        self.root = root / "files"
        self.root_fd = None

    def initialize(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        ensure_private_directory(self.root)
        for name in ("workspace", "reports"):
            directory = self.root / name
            directory.mkdir(mode=0o700, exist_ok=True)
            ensure_private_directory(directory)
        for directory in (self.root, self.root.parent):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def __enter__(self):
        self.root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._private_directory(self.root_fd)
            for name in ("workspace", "reports"):
                with self.parent(f"{name}/probe") as (fd, _):
                    # Fail startup on filesystems lacking durable directory synchronization.
                    os.fsync(fd)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        os.close(self.root_fd)
        self.root_fd = None

    @staticmethod
    def _private_directory(fd):
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise Rejected("unsafe_workspace_directory")

    @contextmanager
    def parent(self, path):
        try:
            PATH.validate_python(path)
        except ValidationError as exc:
            raise Rejected("unsafe_workspace_path") from exc
        parts = path.split("/")
        fd = os.dup(self.root_fd)
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError as exc:
                    raise Rejected("parent_not_found") from exc
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EACCES}:
                        raise Rejected("unsafe_workspace_directory") from exc
                    raise
                os.close(fd)
                fd = child
                self._private_directory(fd)
            yield fd, parts[-1]
        finally:
            os.close(fd)

    @staticmethod
    def _identity(info):
        return [info.st_dev, info.st_ino]

    @staticmethod
    def _staging_name(execution_id):
        if not EXECUTION_ID.fullmatch(execution_id):
            raise Rejected("invalid_execution_id")
        return ".broker-stage-" + execution_id

    def _read_entry(self, parent, name, *, staging=False):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError as exc:
            raise Rejected("file_not_found") from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EACCES}:
                raise Rejected("unsafe_workspace_file") from exc
            raise
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(fd)
            allowed_links = {1, 2} if staging else {1}
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink not in allowed_links
                    or stat.S_IMODE(info.st_mode) & 0o077):
                raise Rejected("unsafe_workspace_file")
            if info.st_size > MAX_FILE_BYTES:
                raise Rejected("file_too_large")
            data = source.read(MAX_FILE_BYTES + 1)
            final = os.fstat(fd)
            if len(data) > MAX_FILE_BYTES or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                final.st_size, final.st_mtime_ns, final.st_ctime_ns
            ):
                raise Rejected("file_changed_while_reading")
            return data, self._identity(info)

    def read(self, intent: ReadFile):
        with self.parent(intent.path) as (parent, name):
            data, _ = self._read_entry(parent, name)
        try:
            content = data.decode("utf-8")
        except UnicodeError as exc:
            raise Rejected("file_not_utf8") from exc
        return {"path": intent.path, "content": content, "bytes": len(data), "sha256": digest(data)}

    def stage(self, execution_id: str, intent: WriteFile):
        data = intent.content.encode("utf-8")
        with self.parent(intent.path) as (parent, name):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise Rejected("path_exists")
            temporary = self._staging_name(execution_id)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(fd)
                identity = self._identity(os.fstat(fd))
            os.fsync(parent)
            return {"directory": self._identity(os.fstat(parent)), "inode": identity, "sha256": digest(data), "bytes": len(data)}

    def _matches(self, parent, name, checkpoint):
        try:
            content, inode = self._read_entry(parent, name, staging=True)
        except Rejected:
            return False
        return inode == checkpoint["inode"] and len(content) == checkpoint["bytes"] and digest(content) == checkpoint["sha256"]

    def _receipt(self, path, checkpoint):
        return {"path": path, "bytes": checkpoint["bytes"], "sha256": checkpoint["sha256"]}

    def publish(self, execution_id, path, checkpoint):
        temporary = self._staging_name(execution_id)
        with self.parent(path) as (parent, target):
            if self._identity(os.fstat(parent)) != checkpoint["directory"] or not self._matches(parent, temporary, checkpoint):
                raise Rejected("staging_changed")
            try:
                os.link(temporary, target, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            except FileExistsError as exc:
                raise Rejected("path_exists") from exc
            except OSError as exc:
                raise Uncertain("file_publication_unconfirmed") from exc
            try:
                os.fsync(parent)
                if not self._matches(parent, target, checkpoint):
                    raise Uncertain("file_publication_unconfirmed")
            except OSError as exc:
                raise Uncertain("file_publication_unconfirmed") from exc
        return self._receipt(path, checkpoint)

    def reconcile(self, execution_id, path, checkpoint):
        with self.parent(path) as (parent, target):
            if self._identity(os.fstat(parent)) != checkpoint["directory"]:
                return None
            if not self._matches(parent, self._staging_name(execution_id), checkpoint):
                return None
            if not self._matches(parent, target, checkpoint):
                return None
            os.fsync(parent)
        return self._receipt(path, checkpoint)

    def discard(self, execution_id, path):
        with self.parent(path) as (parent, _):
            try:
                os.unlink(self._staging_name(execution_id), dir_fd=parent)
            except FileNotFoundError:
                return
            os.fsync(parent)
