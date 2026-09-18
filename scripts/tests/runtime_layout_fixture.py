"""Test-only large-runtime fixture; never assume a lone Python binary relocates.

An exact-size executable is used in place. A smaller executable is padded only
inside a disposable layout mirror: real directories on the executable path,
absolute symlinks to original runtime siblings elsewhere. The production runner
still seals executable/script bytes. Links are TEST dependency fixtures, not an
isolation guarantee or a delivered runtime. No compiler is required by this helper.
"""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import sys

TARGET_BYTES = 31_362_304


def prepare_large_runtime(executable: str | Path, destination: str | Path,
                          *, target_bytes: int = TARGET_BYTES) -> dict[str, object]:
    """Select actual bytes and record the size branch; caller owns temp lifetime.

    Variable target_bytes is for the one-byte boundary negative control only.
    Oversized sources are rejected, never truncated. Startup is verified by the
    caller's actual pinned-execution test, not inferred from layout construction.
    """
    source = Path(executable).resolve(strict=True)
    before = source.stat()
    if not source.is_file() or not os.access(source, os.X_OK):
        raise AssertionError('fixture source must be a regular executable')
    if type(target_bytes) is not int or not 0 < target_bytes <= 256 * 1024 * 1024:
        raise AssertionError('invalid fixture target size')
    if before.st_size > target_bytes:
        raise AssertionError('do not truncate executable code')
    record = {'source_executable': str(source), 'source_bytes': before.st_size,
              'target_bytes': target_bytes}
    if before.st_size == target_bytes:
        # Do not relocate an already-large uv-managed interpreter.
        return {**record, 'mode': 'actual_runtime_already_large',
                'executable': str(source), 'executable_bytes': before.st_size,
                'copied_binary': False, 'layout_root': None,
                'source_anchor': None, 'linked_sibling_count': 0}

    # Include the actual executable ancestry AND both known Python prefixes.
    # A venv/launcher may live outside base_prefix, making this anchor '/'.
    # Mirror only the executable's directory chain; sibling links are not
    # traversed recursively, and the original installation is not changed.
    anchor = Path(os.path.commonpath([str(source.parent),
        str(Path(sys.base_prefix).resolve()), str(Path(sys.base_exec_prefix).resolve())]))
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    layout = destination / 'layout'
    layout.mkdir(mode=0o700)
    original, mirrored = anchor, layout
    links = 0
    parts = source.relative_to(anchor).parts
    for index, part in enumerate(parts):
        for sibling in original.iterdir():
            if sibling.name != part:
                (mirrored / sibling.name).symlink_to(sibling)
                links += 1
        selected = mirrored / part
        if index == len(parts) - 1:
            shutil.copyfile(source, selected)
            with selected.open('ab') as stream:
                stream.truncate(target_bytes)
            selected.chmod(0o700)
        else:
            selected.mkdir(mode=0o700)
            original, mirrored = original / part, selected
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise AssertionError('fixture source changed while mirroring')
    if selected.is_symlink() or selected.stat().st_size != target_bytes:
        raise AssertionError('invalid padded executable fixture')
    return {**record, 'mode': 'synthetic_padded_runtime_layout',
            'executable': str(selected), 'executable_bytes': selected.stat().st_size,
            'copied_binary': True, 'layout_root': str(layout),
            'source_anchor': str(anchor), 'linked_sibling_count': links}
