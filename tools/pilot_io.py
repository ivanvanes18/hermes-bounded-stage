"""Private-by-construction I/O for the out-of-tree pilot tools. Not installed.

`tools/` ships nothing and is unreachable from the installed skill. This module is the
one place where the pilot tools touch the filesystem or measure an executable, so the
trust boundary is stated once and shared by `pilot_registry.py` and `pilot_preflight.py`.

**Paths are resolved, never interpreted.** Every absolute parent component is opened with
`O_NOFOLLOW|O_DIRECTORY` from a descriptor on `/`, so no ancestor may be a symlink and no
ancestor may be swapped for one between the check and the use: after the walk the caller
holds a descriptor on the *inode* it validated, and every subsequent operation is
descriptor-relative. `.`, `..` and empty components are refused outright rather than
normalised, because normalising a path is an interpretation of someone else's namespace.

**Privacy is a mode, not a convention.** The final parent of anything published or read
back must be owned by the current uid and carry **no group or other bits at all** --
`st_mode & 0o077 == 0`. The intended mode is exactly `0o700` (`PRIVATE_PARENT_MODE`); the
enforced rule is the mask, so `0o500` and `0o300` also pass and `0o701`, `0o750` and
`0o755` do not. Parents are never created here: a parent that does not already exist
privately is a refusal, because creating one would mean writing into a namespace nobody
validated.

**A file's mode is an argument, and the set of legal values is closed.** `publish` takes
an explicit `mode` and `read_private_file` an optional `expect_mode`, each restricted to
`RESTRICTED_MODES` -- `0o600` (`PUBLISHED_MODE`, the default, for registry and preflight
output) or `0o400` (`READONLY_MODE`, for the pinned probe anchor). Anything else is
`mode_not_restricted`, raised before a descriptor exists. The *exact* final mode is
verified on the readback, so a 0400 publication is never satisfied by a 0600 file.

**Publication never overwrites a pathname.** There is no `os.replace`, no `w` mode and no
truncation anywhere in this file. A target that already exists -- as a symlink, a regular
file, a FIFO or anything else -- is a refusal, not a thing to replace. The write itself is
descriptor-relative `O_CREAT|O_EXCL|O_NOFOLLOW`, runs a complete write loop, `fsync`s the
file and then the parent, and finally reopens the name through a fresh nofollow descriptor
to confirm the exact bytes, owner, mode and *inode*. A name re-pointed at another file
between the write and the readback is caught by the inode comparison, not merely by the
content comparison.

**Executables are held, not named.** `hold_executable` returns an open descriptor on the
bytes it measured. Callers execute `/proc/self/fd/<n>` with `pass_fds=`, so the version
that is observed and the process that is started both come from the inode that was
hashed. Replacing the pathname afterwards cannot change either.

What this does NOT do: it makes no claim about the contents of the executable it measures,
about what that executable then opens or executes, or about any directory above the final
parent beyond "it is a real directory and not a symlink".
"""
from __future__ import annotations
import errno
import hashlib
import os
import pwd
from pathlib import Path
import re
import stat
import subprocess

PRIVATE_PARENT_MASK=0o077          # no group bits, no other bits
PRIVATE_PARENT_MODE=0o700          # the mode the pilot actually uses
PUBLISHED_MODE=0o600               # registry and preflight output
READONLY_MODE=0o400                # the pinned probe anchor
RESTRICTED_MODES=(READONLY_MODE,PUBLISHED_MODE)
# Mirrors `pinned_runtime.MAX_EXECUTABLE`; restated rather than imported because nothing
# under `tools/` may depend on the installed skill's runtime.
MAX_EXECUTABLE=256*1024*1024
MAX_PRIVATE_READ=16*1024*1024
ELF_MAGIC=b'\x7fELF'
VERSION_TIMEOUT=10
READ_CHUNK=1024*1024
_VERSION_LINE=re.compile(r'^codex-cli\s+(\d+)\.(\d+)\.(\d+)$')
_OPEN_FLAGS=os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK


class PilotIoError(Exception):
    """Public reason code only. Never echoes a path's contents."""


def alias(fd):
    """The `/proc/self/fd/<n>` name of a held descriptor. Pass it with `pass_fds=`."""
    return '/proc/self/fd/%d'%fd


def child_env():
    """A literal dict derived from the passwd database. Nothing is inherited."""
    try:home=pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:raise PilotIoError('home_unavailable') from None
    if not (home and os.path.isabs(home) and os.path.isdir(home)):
        raise PilotIoError('home_unavailable')
    return {'PATH':os.defpath,'HOME':home,'CODEX_HOME':os.path.join(home,'.codex'),
            'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','TERM':'dumb'}


def _restricted(mode):
    """The only two modes this module will create or require. Checked before any I/O."""
    if mode not in RESTRICTED_MODES:raise PilotIoError('mode_not_restricted')
    return mode


def _components(path):
    value=Path(path)
    if not value.is_absolute():raise PilotIoError('path_not_absolute')
    parts=value.parts[1:]
    if not parts:raise PilotIoError('path_component_invalid')
    for part in parts:
        if part in ('','.','..') or '/' in part:raise PilotIoError('path_component_invalid')
    return parts


def _open_component(name,parent_fd):
    try:
        return os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,
                       dir_fd=parent_fd)
    except FileNotFoundError:raise PilotIoError('path_component_missing') from None
    except OSError as failure:
        # A symlink under O_NOFOLLOW reports ELOOP; a non-directory reports ENOTDIR.
        # Both mean the same thing here: the component is not the directory it claimed.
        if failure.errno in (errno.ELOOP,errno.ENOTDIR):
            raise PilotIoError('path_component_not_directory') from None
        raise PilotIoError('path_component_unreadable') from None


def open_parent(path,*,private=True):
    """Walk every parent component from `/` with O_NOFOLLOW|O_DIRECTORY.

    Returns `(parent_fd, final_name)`. The caller owns `parent_fd` and must close it.
    """
    parts=_components(path)
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    try:
        for part in parts[:-1]:
            nxt=_open_component(part,fd);os.close(fd);fd=nxt
        info=os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):raise PilotIoError('path_component_not_directory')
        if private:
            if info.st_uid!=os.getuid():raise PilotIoError('parent_not_owned')
            if stat.S_IMODE(info.st_mode)&PRIVATE_PARENT_MASK:
                raise PilotIoError('parent_not_private')
    except BaseException:
        os.close(fd);raise
    return fd,parts[-1]


def _reject_existing(parent_fd,name):
    try:info=os.stat(name,dir_fd=parent_fd,follow_symlinks=False)
    except FileNotFoundError:return
    except OSError:raise PilotIoError('target_unreadable') from None
    if stat.S_ISLNK(info.st_mode):raise PilotIoError('target_symlink')
    if not stat.S_ISREG(info.st_mode):raise PilotIoError('target_not_regular')
    raise PilotIoError('target_exists')


def preflight_target(path):
    """Validate a publication target before doing any work that could be wasted.

    Exactly the checks `publish` performs up to the point of creation, and nothing else:
    no descriptor is created and no byte is written.
    """
    parent_fd,name=open_parent(path)
    try:_reject_existing(parent_fd,name)
    finally:os.close(parent_fd)


def _read_exactly(fd,limit):
    chunks=[];total=0
    while total<limit:
        chunk=os.pread(fd,min(READ_CHUNK,limit-total),total)
        if not chunk:break
        chunks.append(chunk);total+=len(chunk)
    return b''.join(chunks)


def _digest_fd(fd,size):
    hasher=hashlib.sha256();offset=0
    while offset<size:
        chunk=os.pread(fd,min(READ_CHUNK,size-offset),offset)
        if not chunk:raise PilotIoError('short_read')
        hasher.update(chunk);offset+=len(chunk)
    if os.pread(fd,1,size):raise PilotIoError('size_changed')
    return hasher.hexdigest()


def verify_published(parent_fd,name,body,*,inode=None,mode=PUBLISHED_MODE):
    """Reopen the published name through a fresh nofollow descriptor and prove it.

    `inode` is the `(st_dev, st_ino)` of the descriptor the bytes were written through.
    Supplying it is what turns this from a content check into a rebind check: a name
    re-pointed at a byte-identical file elsewhere still fails. `mode` is compared
    exactly, not masked: the mode that was asked for is the mode that must be there.
    """
    body=bytes(body);_restricted(mode)
    try:fd=os.open(name,_OPEN_FLAGS,dir_fd=parent_fd)
    except OSError as failure:
        if failure.errno==errno.ELOOP:raise PilotIoError('published_rebound') from None
        raise PilotIoError('published_unreadable') from None
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):raise PilotIoError('published_not_regular')
        if inode is not None and (info.st_dev,info.st_ino)!=inode:
            raise PilotIoError('published_rebound')
        if info.st_uid!=os.getuid():raise PilotIoError('published_owner')
        if stat.S_IMODE(info.st_mode)!=mode:raise PilotIoError('published_mode')
        if info.st_size!=len(body):raise PilotIoError('published_bytes')
        seen=_read_exactly(fd,len(body)+1)
    finally:os.close(fd)
    if seen!=body:raise PilotIoError('published_bytes')
    return hashlib.sha256(seen).hexdigest()


def publish(path,body,*,mode=PUBLISHED_MODE):
    """Create `path` with exactly `body`, privately, without ever overwriting a name.

    `mode` must be one of `RESTRICTED_MODES` and is the mode the readback then requires.
    """
    body=bytes(body);_restricted(mode)
    parent_fd,name=open_parent(path)
    try:
        _reject_existing(parent_fd,name)
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,
                   mode,dir_fd=parent_fd)
        try:
            # The creating open is subject to the umask; the fchmod is not.
            os.fchmod(fd,mode)
            created=os.fstat(fd)
            if not stat.S_ISREG(created.st_mode):raise PilotIoError('target_not_regular')
            view=memoryview(body);offset=0
            while offset<len(body):
                written=os.write(fd,view[offset:])
                if written<=0:raise PilotIoError('short_write')
                offset+=written
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            # The partial target is ours, was created by O_EXCL this instant, and has
            # never been a valid artifact. Removing it is what makes a refusal a no-op.
            try:os.unlink(name,dir_fd=parent_fd)
            except OSError:pass
            raise
        os.close(fd)
        os.fsync(parent_fd)
        return verify_published(parent_fd,name,body,inode=(created.st_dev,created.st_ino),
                                mode=mode)
    finally:os.close(parent_fd)


def read_private_file(path,*,max_bytes=MAX_PRIVATE_READ,expect_mode=None):
    """Read a private regular file through a held descriptor. Returns `(bytes, sha256)`.

    `expect_mode`, when given, is required exactly -- the read-side half of the
    restricted-mode contract, checked on the held descriptor rather than the pathname.
    """
    if expect_mode is not None:_restricted(expect_mode)
    parent_fd,name=open_parent(path)
    try:
        try:fd=os.open(name,_OPEN_FLAGS,dir_fd=parent_fd)
        except FileNotFoundError:raise PilotIoError('source_missing') from None
        except OSError as failure:
            if failure.errno==errno.ELOOP:raise PilotIoError('source_symlink') from None
            raise PilotIoError('source_unreadable') from None
    finally:os.close(parent_fd)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):raise PilotIoError('source_not_regular')
        if info.st_uid!=os.getuid():raise PilotIoError('source_not_owned')
        if stat.S_IMODE(info.st_mode)&PRIVATE_PARENT_MASK:
            raise PilotIoError('source_not_private')
        if expect_mode is not None and stat.S_IMODE(info.st_mode)!=expect_mode:
            raise PilotIoError('source_mode')
        if info.st_size>max_bytes:raise PilotIoError('source_too_large')
        body=_read_exactly(fd,max_bytes+1)
        if len(body)>max_bytes:raise PilotIoError('source_too_large')
    finally:os.close(fd)
    return body,hashlib.sha256(body).hexdigest()


def hold_executable(path):
    """Open, measure and KEEP the native executable. Returns `(fd, sha256, stat_result)`.

    The caller closes `fd`. Until then `alias(fd)` names the exact inode that was hashed,
    and nothing that happens to the pathname can substitute different bytes for it.
    """
    parent_fd,name=open_parent(path,private=False)
    try:
        try:fd=os.open(name,_OPEN_FLAGS,dir_fd=parent_fd)
        except FileNotFoundError:raise PilotIoError('codex_missing') from None
        except OSError as failure:
            if failure.errno==errno.ELOOP:raise PilotIoError('codex_symlink') from None
            raise PilotIoError('codex_unreadable') from None
    finally:os.close(parent_fd)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):raise PilotIoError('codex_not_regular')
        if info.st_mode&(stat.S_ISUID|stat.S_ISGID):raise PilotIoError('codex_setid')
        if not info.st_mode&stat.S_IXUSR:raise PilotIoError('codex_not_executable')
        if info.st_size>MAX_EXECUTABLE:raise PilotIoError('codex_too_large')
        if os.pread(fd,len(ELF_MAGIC),0)!=ELF_MAGIC:raise PilotIoError('codex_not_elf')
        measured=_digest_fd(fd,info.st_size)
        os.set_blocking(fd,True)
    except BaseException:
        os.close(fd);raise
    return fd,measured,info


def probe_cli_version(executable,*,env=None,pass_fds=()):
    """`codex --version` and nothing else. No model call, no app-server, no network.

    `executable` is expected to be an `alias(fd)` of a held descriptor, so the version
    observed here is the version of the bytes that were measured.
    """
    try:
        proc=subprocess.run(['codex','--version'],executable=executable,
                            pass_fds=tuple(pass_fds),stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                            env=dict(env or child_env()),timeout=VERSION_TIMEOUT)
    except (OSError,subprocess.SubprocessError):
        raise PilotIoError('cli_version_unreadable') from None
    if proc.returncode!=0:raise PilotIoError('cli_version_unreadable')
    try:text=proc.stdout.decode('utf-8','replace').strip()
    except UnicodeError:raise PilotIoError('cli_version_unreadable') from None
    if not _VERSION_LINE.match(text):raise PilotIoError('cli_version_unreadable')
    return text
