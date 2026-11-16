#!/usr/bin/env python3
"""Build offline, deterministic skill ZIPs from Git objects, never the worktree.

Usage: python -B tools/build_release.py --commit HEAD --output <new-empty-dir>
Only the explicit source/resource allowlist below is shipped. ZIPs contain one
skill root (e.g. trade/SKILL.md), without the repository's skills/ wrapper.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import zipfile


SKILLS = ("pm-weather", "pm-liquid", "trade", "stock")
REPO = Path(__file__).resolve().parents[1]
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FORBIDDEN = {".git", ".mimosa", "__pycache__", ".pytest_cache", ".cache",
             "cache", "data", "ledger", "ledgers", "tests", "tmp", "temp"}
SENSITIVE = re.compile(r"(^|[_.-])(secret|secrets|credential|credentials|password|token|private|key|keys|id_rsa|id_ed25519)([_.-]|$)", re.I)
TEMPORARY = re.compile(r"^(check_|test_|audit|tmp[_.-]|temp[_.-])", re.I)


class BuildError(Exception):
    """A release could not safely be built."""


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise BuildError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout


def allowed(relative):
    """Strictly allow official direct children, not arbitrary tracked files."""
    parts = PurePosixPath(relative).parts
    if not parts or "\\" in relative or relative.startswith("/"):
        return False
    for part in parts:
        lower = part.lower()
        if (lower in FORBIDDEN or lower in {".", ".."} or lower.startswith(".env")
                or lower.startswith(".") or SENSITIVE.search(lower)
                or TEMPORARY.search(lower)):
            return False
    if len(parts) == 1:
        return relative in {"SKILL.md", "README.md"}
    if len(parts) != 2:
        return False
    folder, name = parts
    suffix = PurePosixPath(name).suffix
    return ((folder == "scripts" and suffix == ".py")
            or (folder == "references" and suffix == ".md")
            or (folder == "locales" and suffix in {".md", ".json"}))


def make_zip(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for path, content in sorted(files.items()):
            info = zipfile.ZipInfo(path, FIXED_TIME)
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED,
                             compresslevel=9)
    return stream.getvalue()


def build_release(repo, commit, output):
    """Return manifest; write only into a nonexistent or empty real directory."""
    output = Path(output).absolute()
    if output.is_symlink() or (output.exists() and
                              (not output.is_dir() or any(output.iterdir()))):
        raise BuildError("output must be a new or empty directory (not a symlink)")
    if not commit or commit.startswith("-"):
        raise BuildError("invalid commit")
    sha = git(repo, "rev-parse", "--verify", "--end-of-options",
              commit + "^{commit}").decode("ascii").strip()
    tree = git(repo, "ls-tree", "-r", "-z", sha, "--", "skills")
    selected = {skill: {} for skill in SKILLS}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        path = raw_path.decode("utf-8")
        parts = path.split("/", 2)
        if len(parts) != 3 or parts[0] != "skills" or parts[1] not in selected:
            continue
        skill, relative = parts[1:]
        if not allowed(relative):
            continue
        if mode not in {b"100644", b"100755"} or kind != b"blob":
            raise BuildError(f"non-regular release file: {path}")
        selected[skill][f"{skill}/{relative}"] = git(repo, "cat-file", "blob", oid.decode("ascii"))
    artifacts = {}
    packages = {}
    for skill, files in selected.items():
        if f"{skill}/SKILL.md" not in files:
            raise BuildError(f"missing skills/{skill}/SKILL.md in commit {sha}")
        filename = f"{skill}.zip"
        payload = make_zip(files)
        artifacts[filename] = payload
        packages[filename] = {"sha256": hashlib.sha256(payload).hexdigest(),
                              "paths": sorted(files)}
    manifest = {"schema_version": 1, "commit": sha, "packages": packages}
    artifacts["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2,
                                            sort_keys=True) + "\n").encode("utf-8")
    artifacts["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
        for name, payload in sorted(artifacts.items())).encode("ascii")
    output.mkdir(parents=True, exist_ok=True)
    if output.is_symlink() or any(output.iterdir()):
        raise BuildError("output changed or is not empty")
    created = []
    try:
        for name, payload in sorted(artifacts.items()):
            path = output / name
            with path.open("xb") as handle:
                created.append(path)
                handle.write(payload)
    except OSError as exc:
        for path in created:
            path.unlink(missing_ok=True)
        raise BuildError(f"could not write release: {exc}") from exc
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD", help="Git commit/ref (default HEAD)")
    parser.add_argument("--output", required=True, type=Path, help="new or empty output directory")
    args = parser.parse_args(argv)
    try:
        manifest = build_release(REPO, args.commit, args.output)
    except (BuildError, OSError, UnicodeError) as exc:
        parser.exit(1, f"release build failed: {exc}\n")
    print(f"Built {len(SKILLS)} packages from {manifest['commit']} in {args.output.absolute()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
