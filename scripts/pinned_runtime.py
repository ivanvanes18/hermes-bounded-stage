"""Bounded no-follow reads and Linux sealed executable/script snapshots.

Only /proc/self/fd aliases for retained, sealed memfds reach exec. Original
pathnames are not executable authority. OS loader/libraries/procfs and controller
isolation remain trusted prerequisites, NOT an OS sandbox supplied by this code.
"""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import PurePosixPath
import stat
import sys
from schema_validation import ContractError

MAX_DOCUMENT = 16 * 1024 * 1024
MAX_EXECUTABLE = 256 * 1024 * 1024
MAX_COMMAND_BYTES = 512 * 1024 * 1024
CHUNK = 64 * 1024

# Linux UAPI (v6.12 include/uapi/linux/fcntl.h, asm-generic/fcntl.h).
# These are fcntl command/flag values, NOT architecture-specific syscall numbers.
# CPython exports depend on build-time headers; symbol presence is not a kernel
# capability probe. Use only after bind_command's Linux guard, and query every
# actual snapshot. FUTURE_WRITE/EXEC are not part of the inherited required mask.
_LINUX_F_ADD_SEALS = 1033  # F_LINUX_SPECIFIC_BASE (1024) + 9
_LINUX_F_GET_SEALS = 1034  # F_LINUX_SPECIFIC_BASE (1024) + 10
_REQUIRED_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008  # SEAL/SHRINK/GROW/WRITE


def _seal_snapshot(fd):
    """Establish capability on the actual object; no cached or symbol-only result."""
    try:
        added = fcntl.fcntl(fd, _LINUX_F_ADD_SEALS, _REQUIRED_SEALS)
        actual = fcntl.fcntl(fd, _LINUX_F_GET_SEALS)
    except (OSError, AttributeError):
        # Translate here, before regular_fd could misclassify a syscall failure
        # as a source-file failure. No child is launched before ALL objects pass.
        raise ContractError('sealed_execution_unavailable') from None
    if (type(added) is not int or added != 0 or type(actual) is not int or
            actual < 0 or actual & _REQUIRED_SEALS != _REQUIRED_SEALS):
        raise ContractError('sealed_execution_unavailable')


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _parts(path):
    text = os.fspath(path)
    if (not isinstance(text, str) or '\\' in text or '\x00' in text or
            not text.startswith('/') or text.startswith('//') or
            any(part in ('', '.', '..') for part in text.split('/')[1:])):
        raise ContractError('noncanonical_path')
    return PurePosixPath(text).parts[1:]


@contextmanager
def regular_fd(path, *, limit=MAX_DOCUMENT, executable=False):
    """Anchor every component with openat(O_NOFOLLOW), including directory prefixes."""
    fds = []
    try:
        parts = _parts(path)
        if type(limit) is not int or not 0 < limit <= MAX_EXECUTABLE:
            raise ContractError('source_limit')
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        directory = os.open('/', directory_flags); fds.append(directory)
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            fds.append(child); directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=directory)
        fds.append(fd); before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ContractError('source_not_regular')
        if before.st_size > limit:
            raise ContractError('executable_size' if executable else 'source_size')
        if executable and (not before.st_mode & 0o111 or before.st_mode & 0o6000):
            raise ContractError('executable_permissions')
        yield fd, before
        if _identity(os.fstat(fd)) != _identity(before):
            raise ContractError('source_changed_during_read')
    except OSError:
        raise ContractError('source_unavailable_or_unsafe') from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _chunks(fd, limit):
    total = 0
    while True:
        raw = os.read(fd, min(CHUNK, limit - total + 1))
        if not raw:
            return
        total += len(raw)
        if total > limit:
            raise ContractError('source_size')
        yield raw


def read_document(path, limit=MAX_DOCUMENT):
    if type(limit) is not int or not 0 < limit <= MAX_DOCUMENT:
        raise ContractError('source_limit')
    with regular_fd(path, limit=limit) as (fd, before):
        raw = b''.join(_chunks(fd, limit))
        if len(raw) != before.st_size:
            raise ContractError('source_changed_during_read')
    return raw


def pinned_hash(path, *, executable=False):
    limit = MAX_EXECUTABLE if executable else MAX_DOCUMENT
    with regular_fd(path, limit=limit, executable=executable) as (fd, before):
        hasher = hashlib.sha256(); count = 0
        for chunk in _chunks(fd, limit):
            hasher.update(chunk); count += len(chunk)
        if count != before.st_size:
            raise ContractError('source_changed_during_read')
    return hasher.hexdigest()


@contextmanager
def bind_command(spec, cwd):
    """Revalidate copied bytes, seal them, then keep descriptor bindings through exec.

    argv[0] is a display/runtime-discovery name only. Popen's executable override
    is a sealed descriptor alias. All other pinned arguments (notably scripts)
    become sealed descriptor aliases as well. No Python preexec_fn is used.
    """
    if sys.platform != 'linux' or not hasattr(os, 'memfd_create'):
        raise ContractError('sealed_execution_unavailable')
    retained = {}; total = 0
    try:
        for path, expected in spec['pins'].items():
            is_executable = path == spec['argv'][0]
            limit = MAX_EXECUTABLE if is_executable else MAX_DOCUMENT
            with regular_fd(path, limit=limit, executable=is_executable) as (source, before):
                total += before.st_size
                if total > MAX_COMMAND_BYTES:
                    raise ContractError('command_bytes_budget')
                # Shebang launch would resolve an unpinned interpreter. Require
                # an explicit pinned native runtime + pinned script argument.
                if is_executable and os.pread(source, 4, 0) != b'\x7fELF':
                    raise ContractError('explicit_native_runtime_required')
                try:
                    fd = os.memfd_create('hermes-pinned', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
                except (OSError, AttributeError):
                    raise ContractError('sealed_execution_unavailable') from None
                retained[path] = fd; hasher = hashlib.sha256(); count = 0
                for chunk in _chunks(source, limit):
                    hasher.update(chunk); count += len(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise ContractError('snapshot_write_failed')
                        view = view[written:]
                if count != before.st_size or hasher.hexdigest() != expected:
                    raise ContractError('command_pin_changed')
                os.fchmod(fd, 0o500 if is_executable else 0o400)
                _seal_snapshot(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                # Verify the immutable object, not just the copy input.
                sealed_hash = hashlib.sha256()
                for chunk in _chunks(fd, limit):
                    sealed_hash.update(chunk)
                if sealed_hash.hexdigest() != expected:
                    raise ContractError('command_pin_changed')
                os.lseek(fd, 0, os.SEEK_SET)
        args = [str(cwd) if a == '{workspace}' else
                ('/proc/self/fd/' + str(retained[a]) if i and a in retained else a)
                for i, a in enumerate(spec['argv'])]
        yield args, '/proc/self/fd/' + str(retained[spec['argv'][0]]), tuple(retained.values())
    except (OSError, AttributeError):
        raise ContractError('sealed_execution_unavailable') from None
    finally:
        for fd in retained.values():
            os.close(fd)
