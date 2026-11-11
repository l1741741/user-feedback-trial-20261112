"""Handles all file writing and post-installation processing."""

import io
import os
import stat
import warnings
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    BinaryIO,
    Union,
)

from installer.exceptions import UnsafePathError
from installer.records import Hash, RecordEntry
from installer.scripts import Script
from installer.utils import (
    _WINDOWS,
    Scheme,
    _current_umask,
    construct_record_file,
    copyfileobj_with_hashing,
    fix_shebang,
    make_file_executable,
    validate_installation_path,
)

if TYPE_CHECKING:
    from installer.scripts import LauncherKind, ScriptSection


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

# ``dir_fd`` support is a POSIX feature. On such platforms, all strict writes
# walk and open via descriptors, never by resolving a path first.
_DIR_FD_SUPPORTED = (
    os.name == "posix"
    and bool(_O_NOFOLLOW)
    and bool(_O_DIRECTORY)
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.mkdir in os.supports_dir_fd
)


class WheelDestination:
    """Handles writing the unpacked files, script generation and ``RECORD`` generation.

    Subclasses provide the concrete script generation logic, as well as the RECORD file
    (re)writing.
    """

    def write_script(
        self, name: str, module: str, attr: str, section: "ScriptSection"
    ) -> RecordEntry:
        """Write a script in the correct location to invoke given entry point.

        :param name: name of the script
        :param module: module path, to load the entry point from
        :param attr: final attribute access, for the entry point
        :param section: Denotes the "entry point section" where this was specified.
            Valid values are ``"gui"`` and ``"console"``.
        :type section: str

        Example usage/behaviour::

            >>> dest.write_script("pip", "pip._internal.cli", "main", "console")

        """
        raise NotImplementedError

    def write_file(
        self,
        scheme: Scheme,
        path: Union[str, "os.PathLike[str]"],
        stream: BinaryIO,
        is_executable: bool,
    ) -> RecordEntry:
        """Write a file to correct ``path`` within the ``scheme``.

        :param scheme: scheme to write the file in (like "purelib", "platlib" etc).
        :param path: path within that scheme
        :param stream: contents of the file
        :param is_executable: whether the file should be made executable

        The stream would be closed by the caller, after this call.

        Example usage/behaviour::

            >>> with open("__init__.py") as stream:
            ...     dest.write_file("purelib", "pkg/__init__.py", stream)

        """
        raise NotImplementedError

    def finalize_installation(
        self,
        scheme: Scheme,
        record_file_path: str,
        records: Iterable[tuple[Scheme, RecordEntry]],
    ) -> None:
        """Finalize installation, after all the files are written.

        Handles (re)writing of the ``RECORD`` file.

        :param scheme: scheme to write the ``RECORD`` file in
        :param record_file_path: path of the ``RECORD`` file with that scheme
        :param records: entries to write to the ``RECORD`` file

        Example usage/behaviour::

            >>> dest.finalize_installation("purelib")

        """
        raise NotImplementedError


@dataclass
class SchemeDictionaryDestination(WheelDestination):
    """Destination, based on a mapping of {scheme: file-system-path}."""

    scheme_dict: dict[str, str]
    """A mapping of {scheme: file-system-path}"""

    interpreter: str
    """The interpreter to use for generating scripts."""

    script_kind: "LauncherKind"
    """The "kind" of launcher script to use."""

    hash_algorithm: str = "sha256"
    """
    The hashing algorithm to use, which is a member of
    :any:`hashlib.algorithms_available` (ideally from
    :any:`hashlib.algorithms_guaranteed`).
    """

    bytecode_optimization_levels: Collection[int] = ()
    """
    Compile cached bytecode for installed .py files with these optimization
    levels. The bytecode is specific to the minor version of Python (e.g. 3.10)
    used to generate it.
    """

    destdir: str | None = None
    """
    A staging directory in which to write all files. This is expected to be the
    filesystem root at runtime, so embedded paths will be written as though
    this was the root.
    """

    overwrite_existing: bool = False
    """Silently overwrite existing files."""

    strict_paths: bool = False
    """Enable strict, symlink-resistant path handling.

    When enabled, installation performs a full preflight before writing
    anything: the wheel ``RECORD`` is checked, member paths are validated,
    conflicting target paths are rejected, and pre-existing targets are
    inspected. File writes then use directory file descriptors (on POSIX) so
    that no symlink component is followed, even if it is introduced after the
    preflight check.

    This is opt-in; the default preserves the historical behaviour.
    """

    def _path_with_destdir(self, scheme: Scheme, path: str) -> Path:
        # See https://docs.python.org/3/library/zipfile.html#zipfile.Path:
        #  When handling untrusted archives,
        #  consider resolving filenames using os.path.abspath()
        #  and checking against the target directory with os.path.commonpath().
        #
        # Attention: Path.absolute() is not sufficient because it does not
        #  normalize, i.e. does not remove "..".
        #
        # We want to avoid Path.resolve() because it is significantly slower
        # than os.path.abspath()!
        target_dir = Path(os.path.abspath(self.scheme_dict[scheme]))  # noqa: PTH100
        file = Path(os.path.abspath(target_dir / path))  # noqa: PTH100

        if not file.is_relative_to(target_dir):
            raise ValueError(
                f"Attempting to write {path} outside of the target directory"
            )

        if self.destdir is not None:
            rel_path = file.relative_to(file.anchor)
            return Path(self.destdir) / rel_path
        return file

    # ------------------------------------------------------------------
    # Strict path handling
    # ------------------------------------------------------------------

    def _strict_target(
        self, scheme: Scheme, path: str
    ) -> tuple[str, list[str], str]:
        """Compute the trusted root, path components and display target.

        ``root`` is the caller-provided trusted starting point (the scheme
        directory, or ``destdir`` when one is set). ``components`` are relative
        to that root and must be traversed without following symlinks.
        """
        if scheme not in self.scheme_dict:
            raise UnsafePathError(f"Unknown installation scheme: {scheme!r}")

        scheme_root = os.path.abspath(os.fspath(self.scheme_dict[scheme]))
        logical = os.path.normpath(os.path.join(scheme_root, path))

        if os.path.commonpath([scheme_root, logical]) != scheme_root:
            raise UnsafePathError(
                f"Attempting to write {path!r} outside of the target directory"
            )

        if self.destdir is not None:
            anchor = Path(logical).anchor
            rel_to_anchor = Path(logical).relative_to(anchor)
            root = os.path.abspath(os.fspath(self.destdir))
            target = os.path.normpath(
                os.path.join(root, os.fspath(rel_to_anchor))
            )
        else:
            root = scheme_root
            target = logical

        relative = os.path.relpath(target, root)
        components = relative.split(os.sep)
        if (
            relative in (os.curdir, os.pardir)
            or relative.startswith(os.pardir + os.sep)
            or any(part in ("", os.curdir, os.pardir) for part in components)
        ):
            raise UnsafePathError(
                f"Attempting to write {path!r} to an invalid target path"
            )

        return root, components, target

    @staticmethod
    def _strict_conflict_key(target: str) -> str:
        # ``realpath`` is used only to compare preflight identities (for
        # example, two scheme roots which are links to the same directory).
        # Actual writes never resolve a path and then open it by name; they walk
        # with ``O_NOFOLLOW`` and open via directory file descriptors.
        return os.path.normcase(os.path.realpath(target))

    def _preflight_writes(
        self, planned: Iterable[tuple[Scheme, str, str]]
    ) -> None:
        """Check strict-mode install targets before anything is written.

        :param planned: ``(scheme, relative path, description)`` for every file
            that will be written, including entry point scripts and the final
            ``RECORD``.
        """
        seen: dict[str, str] = {}
        targets: list[tuple[str, list[str], str, str]] = []

        for scheme, path, origin in planned:
            normalized = validate_installation_path(path)
            root, components, target = self._strict_target(scheme, normalized)
            key = self._strict_conflict_key(target)
            if key in seen:
                raise UnsafePathError(
                    f"Multiple wheel members map to the same installation target "
                    f"{target!r}: {seen[key]} and {origin}"
                )
            seen[key] = origin
            targets.append((root, components, target, origin))

        # Only inspect the existing filesystem once all paths have been
        # validated and conflicts have been rejected.
        for root, components, target, origin in targets:
            self._check_existing_target(root, components, target, origin)

    def _open_trusted_root(self, root: str, *, create: bool) -> int | None:
        if create:
            try:
                os.makedirs(root, exist_ok=True)
            except OSError as exc:
                raise UnsafePathError(
                    f"Unable to create the trusted installation root {root!r}: {exc}"
                ) from exc

        try:
            return os.open(root, os.O_RDONLY | _O_DIRECTORY)
        except FileNotFoundError:
            if create:
                raise
            return None
        except OSError as exc:
            raise UnsafePathError(
                f"Unable to open the trusted installation root {root!r}: {exc}"
            ) from exc

    @staticmethod
    def _component_error(component: str, dir_fd: int, exc: OSError) -> UnsafePathError:
        try:
            st = os.stat(component, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            st = None
        if st is not None and stat.S_ISLNK(st.st_mode):
            return UnsafePathError(
                f"Refusing to traverse symbolic link component: {component!r}"
            )
        if st is not None and not stat.S_ISDIR(st.st_mode):
            return UnsafePathError(
                f"Refusing to traverse non-directory component: {component!r}"
            )
        return UnsafePathError(
            f"Refusing to traverse directory component {component!r}: {exc}"
        )

    def _descend(
        self, dir_fd: int, component: str, *, create: bool
    ) -> int | None:
        """Open a child directory without following symlinks.

        Returns ``None`` when the component does not exist and ``create`` is
        false.
        """
        flags = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
        try:
            return os.open(component, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            if not create:
                return None
            try:
                os.mkdir(component, dir_fd=dir_fd)
            except FileExistsError:
                pass
            try:
                return os.open(component, flags, dir_fd=dir_fd)
            except OSError as exc:
                raise self._component_error(component, dir_fd, exc) from exc
        except OSError as exc:
            raise self._component_error(component, dir_fd, exc) from exc

    def _check_existing_target(
        self, root: str, components: list[str], target: str, origin: str
    ) -> None:
        """Inspect pre-existing target state without creating anything."""
        root_fd = self._open_trusted_root(root, create=False)
        if root_fd is None:
            # The whole trusted root is missing, so every target is new.
            return

        opened = [root_fd]
        try:
            parent_fd = root_fd
            for component in components[:-1]:
                parent_fd = self._descend(parent_fd, component, create=False)
                if parent_fd is None:
                    # This component and everything below it is new.
                    return
                opened.append(parent_fd)

            name = components[-1]
            try:
                st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise UnsafePathError(
                    f"Unable to inspect existing target {target!r} for "
                    f"{origin}: {exc}"
                ) from exc

            if stat.S_ISLNK(st.st_mode):
                raise UnsafePathError(
                    f"Refusing to write through a symbolic link at {target!r} "
                    f"(for {origin})"
                )
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePathError(
                    f"Refusing to overwrite a non-regular file at {target!r} "
                    f"(for {origin})"
                )
            if not self.overwrite_existing:
                raise FileExistsError(f"File already exists: {target!s}")
            if st.st_nlink > 1:
                raise UnsafePathError(
                    f"Refusing to overwrite a multiply-linked file at "
                    f"{target!r} (for {origin})"
                )
        finally:
            for fd in opened:
                try:
                    os.close(fd)
                except OSError:  # pragma: no cover
                    pass

    @staticmethod
    def _strict_open_error(exc: OSError, name: str) -> UnsafePathError:
        return UnsafePathError(
            f"Refusing to open installation target {name!r}: {exc}"
        )

    def _open_strict_target(
        self, parent_fd: int, name: str, target: str
    ) -> int:
        """Safely open the final file, never following a symlink."""
        nofollow = _O_NOFOLLOW
        nonblock = _O_NONBLOCK

        try:
            st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            st = None

        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                raise UnsafePathError(
                    f"Refusing to write through a symbolic link at {target!r}"
                )
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePathError(
                    f"Refusing to overwrite a non-regular file at {target!r}"
                )
            if not self.overwrite_existing:
                raise FileExistsError(f"File already exists: {target!s}")
            if st.st_nlink > 1:
                raise UnsafePathError(
                    f"Refusing to overwrite a multiply-linked file at {target!r}"
                )

        if st is None:
            try:
                return os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | nonblock,
                    0o666,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                raise FileExistsError(f"File already exists: {target!s}") from None
            except OSError as exc:
                raise self._strict_open_error(exc, target) from exc

        # Overwrite an existing regular file. Open without O_TRUNC, verify the
        # object actually reached through this descriptor, and only then
        # truncate it. This avoids following a link or clobbering a file that
        # gained another hard link after the preflight check.
        try:
            fd = os.open(
                name,
                os.O_WRONLY | nofollow | nonblock,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            try:
                return os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | nonblock,
                    0o666,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise self._strict_open_error(exc, target) from exc
        except OSError as exc:
            raise self._strict_open_error(exc, target) from exc

        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode):
                raise UnsafePathError(
                    f"Refusing to overwrite a non-regular file at {target!r}"
                )
            if fst.st_nlink > 1:
                raise UnsafePathError(
                    f"Refusing to overwrite a multiply-linked file at {target!r}"
                )
            os.ftruncate(fd, 0)
        except BaseException:
            os.close(fd)
            raise

        return fd

    def _write_with_fd(
        self,
        parent_fd: int,
        name: str,
        target: str,
        stream: BinaryIO,
        is_executable: bool,
    ) -> tuple[str, int]:
        fd = self._open_strict_target(parent_fd, name, target)
        try:
            os.set_blocking(fd, True)
            file_obj = os.fdopen(fd, "wb")
        except BaseException:  # pragma: no cover - very unlikely
            os.close(fd)
            raise

        with file_obj as f:
            hash_, size = copyfileobj_with_hashing(stream, f, self.hash_algorithm)
            if is_executable:
                os.fchmod(f.fileno(), 0o777 & ~_current_umask() | 0o111)

        return hash_, size

    def _write_to_fs_strict(
        self,
        scheme: Scheme,
        path: str,
        stream: BinaryIO,
        is_executable: bool,
    ) -> RecordEntry:
        normalized = validate_installation_path(path)
        root, components, target = self._strict_target(scheme, normalized)

        root_fd = self._open_trusted_root(root, create=True)
        assert root_fd is not None
        opened = [root_fd]
        try:
            parent_fd = root_fd
            for component in components[:-1]:
                parent_fd = self._descend(parent_fd, component, create=True)
                assert parent_fd is not None
                opened.append(parent_fd)

            hash_, size = self._write_with_fd(
                parent_fd,
                components[-1],
                target,
                stream,
                is_executable,
            )
        finally:
            for fd in opened:
                try:
                    os.close(fd)
                except OSError:  # pragma: no cover
                    pass

        return RecordEntry(normalized, Hash(self.hash_algorithm, hash_), size)

    def _write_to_fs_strict_fallback(
        self,
        scheme: Scheme,
        path: str,
        stream: BinaryIO,
        is_executable: bool,
    ) -> RecordEntry:
        """Best-effort strict handling on platforms without directory fds.

        This still rejects symlinks and multiply-linked files, but cannot
        provide the same atomic no-follow guarantees as the POSIX implementation.
        """
        normalized = validate_installation_path(path)
        root, components, target = self._strict_target(scheme, normalized)
        os.makedirs(root, exist_ok=True)  # noqa: PTH103

        current = root
        for component in components[:-1]:
            current = os.path.join(current, component)  # noqa: PTH118
            if os.path.islink(current):
                raise UnsafePathError(
                    f"Refusing to traverse symbolic link component: {component!r}"
                )
            if os.path.exists(current) and not os.path.isdir(current):
                raise UnsafePathError(
                    f"Refusing to traverse non-directory component: {component!r}"
                )
            os.makedirs(current, exist_ok=True)  # noqa: PTH103

        target_path = os.path.join(current, components[-1])  # noqa: PTH118
        if os.path.islink(target_path):
            raise UnsafePathError(
                f"Refusing to write through a symbolic link at {target!r}"
            )
        if os.path.lexists(target_path):
            st = os.lstat(target_path)
            if not stat.S_ISREG(st.st_mode):
                raise UnsafePathError(
                    f"Refusing to overwrite a non-regular file at {target!r}"
                )
            if not self.overwrite_existing:
                raise FileExistsError(f"File already exists: {target!s}")
            if st.st_nlink > 1:
                raise UnsafePathError(
                    f"Refusing to overwrite a multiply-linked file at {target!r}"
                )

        with open(target_path, "wb") as f:  # noqa: PTH123
            hash_, size = copyfileobj_with_hashing(stream, f, self.hash_algorithm)
            if is_executable:
                os.chmod(target_path, 0o777 & ~_current_umask() | 0o111)  # noqa: PTH101

        return RecordEntry(normalized, Hash(self.hash_algorithm, hash_), size)

    def write_to_fs(
        self,
        scheme: Scheme,
        path: str,
        stream: BinaryIO,
        is_executable: bool,
    ) -> RecordEntry:
        """Write contents of ``stream`` to the correct location on the filesystem.

        :param scheme: scheme to write the file in (like "purelib", "platlib" etc).
        :param path: path within that scheme
        :param stream: contents of the file
        :param is_executable: whether the file should be made executable

        - Ensures that an existing file is not being overwritten.
        - Hashes the written content, to determine the entry in the ``RECORD`` file.
        """
        if self.strict_paths:
            if _DIR_FD_SUPPORTED:
                return self._write_to_fs_strict(
                    scheme, path, stream, is_executable
                )
            return self._write_to_fs_strict_fallback(  # pragma: no cover
                scheme, path, stream, is_executable
            )

        target_path = self._path_with_destdir(scheme, path)
        if not self.overwrite_existing and target_path.exists():
            message = f"File already exists: {target_path!s}"
            raise FileExistsError(message)

        parent_folder = target_path.parent
        if not parent_folder.exists():
            parent_folder.mkdir(parents=True)

        with target_path.open("wb") as f:
            hash_, size = copyfileobj_with_hashing(stream, f, self.hash_algorithm)

        if is_executable:
            make_file_executable(target_path)

        return RecordEntry(path, Hash(self.hash_algorithm, hash_), size)


    def write_file(
        self,
        scheme: Scheme,
        path: Union[str, "os.PathLike[str]"],
        stream: BinaryIO,
        is_executable: bool,
    ) -> RecordEntry:
        """Write a file to correct ``path`` within the ``scheme``.

        :param scheme: scheme to write the file in (like "purelib", "platlib" etc).
        :param path: path within that scheme
        :param stream: contents of the file
        :param is_executable: whether the file should be made executable

        - Changes the shebang for files in the "scripts" scheme.
        - Uses :py:meth:`SchemeDictionaryDestination.write_to_fs` for the
          filesystem interaction.
        """
        path_ = os.fspath(path)
        if self.strict_paths:
            path_ = validate_installation_path(path_)

        if scheme == "scripts":
            with fix_shebang(stream, self.interpreter) as stream_with_different_shebang:
                return self.write_to_fs(
                    scheme, path_, stream_with_different_shebang, is_executable
                )

        return self.write_to_fs(scheme, path_, stream, is_executable)

    def _script_file_name(self, name: str) -> str:
        """Return the file name a generated entry point script will use."""
        if self.script_kind == "posix":
            return name
        return f"{name}.exe"  # pragma: no cover - Windows launcher naming

    def write_script(
        self, name: str, module: str, attr: str, section: "ScriptSection"
    ) -> RecordEntry:
        """Write a script to invoke an entrypoint.

        :param name: name of the script
        :param module: module path, to load the entry point from
        :param attr: final attribute access, for the entry point
        :param section: Denotes the "entry point section" where this was specified.
            Valid values are ``"gui"`` and ``"console"``.
        :type section: str

        - Generates a launcher using :any:`Script.generate`.
        - Writes to the "scripts" scheme.
        - Uses :py:meth:`SchemeDictionaryDestination.write_to_fs` for the
          filesystem interaction.
        """
        if self.strict_paths:
            name = validate_installation_path(name)

        script = Script(name, module, attr, section)
        script_name, data = script.generate(self.interpreter, self.script_kind)

        with io.BytesIO(data) as stream:
            entry = self.write_to_fs(
                Scheme("scripts"), script_name, stream, is_executable=True
            )

            if not self.strict_paths:
                path = self._path_with_destdir(Scheme("scripts"), script_name)
                mode = path.stat().st_mode
                mode |= (mode & 0o444) >> 2
                path.chmod(mode)

            return entry

    def _compile_bytecode(self, scheme: Scheme, record: RecordEntry) -> None:
        """Compile bytecode for a single .py file."""
        if not self.bytecode_optimization_levels or scheme not in (
            "purelib",
            "platlib",
        ):
            return

        import compileall

        target_path = self._path_with_destdir(scheme, record.path)
        dir_path_to_embed = (Path(self.scheme_dict[scheme]) / record.path).parent

        for level in self.bytecode_optimization_levels:
            compileall.compile_file(
                target_path, optimize=level, quiet=1, ddir=dir_path_to_embed
            )

    def finalize_installation(
        self,
        scheme: Scheme,
        record_file_path: str,
        records: Iterable[tuple[Scheme, RecordEntry]],
    ) -> None:
        """Finalize installation, by writing the ``RECORD`` file & compiling bytecode.

        :param scheme: scheme to write the ``RECORD`` file in
        :param record_file_path: path of the ``RECORD`` file with that scheme
        :param records: entries to write to the ``RECORD`` file
        """

        def prefix_for_scheme(file_scheme: str) -> str | None:
            if file_scheme == scheme:
                return None
            if _WINDOWS:  # pragma: no cover
                path = os.path.abspath(self.scheme_dict[file_scheme])  # noqa: PTH100
            else:  # pragma: no cover
                path = os.path.relpath(
                    self.scheme_dict[file_scheme],
                    start=self.scheme_dict[scheme],
                )
            return path + "/"

        record_list = list(records)
        with construct_record_file(record_list, prefix_for_scheme) as record_stream:
            self.write_to_fs(
                scheme, record_file_path, record_stream, is_executable=False
            )

        if self.strict_paths and self.bytecode_optimization_levels:
            warnings.warn(
                "Bytecode compilation is disabled in strict_paths mode, because "
                "compileall cannot write through directory file descriptors.",
                RuntimeWarning,
                stacklevel=2,
            )
            return

        for scheme_, record in record_list:
            self._compile_bytecode(scheme_, record)
