"""Create-only local installer. It never changes config, credentials, or memory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from runtime_support import read_regular, parse_json, StageError

NAME = "hermes-bounded-stage"
VERSION = "1.3.0"


class InstallError(Exception):
    pass


def frontmatter(text):
    """Narrow reader for the top-level SKILL.md header block; no YAML dependency.

    Only unindented `key: value` lines of the leading `---` block are read. Nested
    entries, list items, comments and blank lines are skipped rather than guessed at,
    a repeated top-level key is refused instead of being shadowed, and anything that
    is not a closed header block is refused. This is a strict identity check, not a
    general YAML parser.
    """
    if not text.startswith("---\n"):
        raise InstallError("skill_frontmatter_missing")
    fields = {}
    for line in text.split("\n")[1:]:
        if line.strip() in ("---", "..."):
            return fields
        if not line or line[:1] in (" ", "\t", "#", "-") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        if key != key.strip() or key in fields:
            raise InstallError("invalid_skill_frontmatter")
        fields[key] = value.strip()
    raise InstallError("skill_frontmatter_missing")


def verify_bundle(bundle_root):
    root = Path(bundle_root).expanduser().resolve()
    try:
        manifest = parse_json(read_regular(root / "MANIFEST.json", 1024 * 1024))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("name") != NAME or manifest.get("version") != VERSION:
            raise InstallError("invalid_manifest")
        files = manifest.get("files")
        if not isinstance(files, dict) or not 1 <= len(files) <= 512:
            raise InstallError("invalid_file_inventory")
        for name, digest in files.items():
            if (not isinstance(name, str) or not name or "\\" in name or PurePosixPath(name).is_absolute() or
                    ".." in PurePosixPath(name).parts or "." in name.split("/") or
                    not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise InstallError("invalid_manifest_path_or_hash")
        actual = set()
        for path in root.rglob("*"):
            if path.is_symlink():
                raise InstallError("symlink_in_bundle")
            if path.is_file() and path != root / "MANIFEST.json":
                actual.add(path.relative_to(root).as_posix())
            elif not path.is_file() and not path.is_dir():
                raise InstallError("special_file_in_bundle")
        if actual != set(files):
            raise InstallError("file_inventory_mismatch")
        total = 0
        for name, expected in files.items():
            raw = read_regular(root / name, 2 * 1024 * 1024)
            total += len(raw)
            if total > 16 * 1024 * 1024:
                raise InstallError("bundle_too_large")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise InstallError("file_hash_mismatch")
        skill_md = read_regular(root / NAME / "SKILL.md", 64 * 1024).decode("utf-8")
        if not re.search(r"(?m)^name:\s*" + re.escape(NAME) + r"\s*$", skill_md):
            raise InstallError("skill_name_mismatch")
        # The manifest version alone cannot keep a stale SKILL.md from announcing itself.
        if frontmatter(skill_md).get("version") != VERSION:
            raise InstallError("skill_version_mismatch")
        return manifest
    except (StageError, UnicodeError, OSError) as exc:
        raise InstallError("bundle_unreadable") from exc


def install_bundle(bundle_root, hermes_home):
    root = Path(bundle_root).expanduser().resolve()
    manifest = verify_bundle(root)
    home = Path(hermes_home).expanduser().absolute()
    if home.is_symlink() or not home.is_dir():
        raise InstallError("profile_directory_missing_or_symlink")
    home = home.resolve(); skills = home / "skills"; target = skills / NAME
    if skills.is_symlink() or (skills.exists() and not skills.is_dir()):
        raise InstallError("invalid_skills_directory")
    if target.exists() or target.is_symlink():
        raise InstallError("skill_already_exists_no_overwrite")
    skills.mkdir(mode=0o700, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".bounded-stage-install-", dir=str(skills)))
    reserved = False
    count = 0
    try:
        for relative, expected in manifest["files"].items():
            if not relative.startswith(NAME + "/"):
                continue
            suffix = relative[len(NAME) + 1:]
            destination = staging / suffix
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            raw = read_regular(root / relative, 2 * 1024 * 1024)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise InstallError("source_changed_during_install")
            fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush(); os.fsync(stream.fileno())
            count += 1
        # Atomic reservation refuses both a pre-existing and racing destination.
        target.mkdir(mode=0o700)
        reserved = True
        os.replace(staging, target)
        reserved = False
        return {"status": "installed", "name": NAME, "version": VERSION,
                "path": str(target), "files": count,
                "configuration_changed": False, "memory_changed": False,
                "model_calls": 0}
    except (StageError, OSError) as exc:
        raise InstallError("installation_failed_without_overwrite") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if reserved:
            # Remove only our still-empty reservation, never someone else's data.
            try:
                target.rmdir()
            except OSError:
                pass


def main(argv=None, bundle_root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", default=str(bundle_root or Path(__file__).resolve().parents[2]))
    parser.add_argument("--hermes-home")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            manifest = verify_bundle(args.bundle)
            result = {"status": "bundle_verified", "files": len(manifest["files"]), "installed": False}
        elif args.hermes_home:
            result = install_bundle(args.bundle, args.hermes_home)
        else:
            raise InstallError("provide_hermes_home_or_verify_only")
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except InstallError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
