"""Offline release regressions using disposable local Git repositories only."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "build_release.py"
SPEC = importlib.util.spec_from_file_location("release_builder", SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="Release Test",
               GIT_AUTHOR_EMAIL="release@example.invalid",
               GIT_COMMITTER_NAME="Release Test",
               GIT_COMMITTER_EMAIL="release@example.invalid")
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        check=True, capture_output=True, env=env).stdout.decode().strip()


@pytest.fixture
def sandbox():
    # Unlike pytest's retained tmp_path, these repositories/artifacts are removed.
    with tempfile.TemporaryDirectory(prefix="skill-release-tests-") as directory:
        yield Path(directory)


@pytest.fixture
def repo(sandbox):
    root = sandbox / "repo"
    root.mkdir()
    git(root, "init")
    for skill in release.SKILLS:
        files = {
            "SKILL.md": f"# {skill}\n",
            "README.md": "Instructions\n",
            "scripts/main.py": "print('committed')\n",
            "references/guide.md": "Guide\n",
            "locales/zh-CN.json": '{"label": "中文"}\n',
            "locales/en-US.md": "English\n",
        }
        for name, content in files.items():
            path = root / "skills" / skill / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "fixture")
    return root


def test_structure_manifest_and_hashes(repo, sandbox):
    output = sandbox / "release"
    manifest = release.build_release(repo, "HEAD", output)
    assert manifest["commit"] == git(repo, "rev-parse", "HEAD")
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert {path.name for path in output.iterdir()} == {
        *(f"{skill}.zip" for skill in release.SKILLS), "manifest.json", "SHA256SUMS"}
    for skill in release.SKILLS:
        name = f"{skill}.zip"
        with zipfile.ZipFile(output / name) as archive:
            paths = archive.namelist()
            assert paths == sorted(paths) == manifest["packages"][name]["paths"]
            assert f"{skill}/SKILL.md" in paths
            assert f"{skill}/locales/zh-CN.json" in paths
            assert all(path.startswith(f"{skill}/") for path in paths)
            assert not any(path.startswith("skills/") or f"{skill}/{skill}/" in path
                           for path in paths)
            assert archive.testzip() is None
            assert all(info.date_time == release.FIXED_TIME for info in archive.infolist())
        assert manifest["packages"][name]["sha256"] == hashlib.sha256(
            (output / name).read_bytes()).hexdigest()
    sums = {}
    for line in (output / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        sums[name] = digest
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()
    assert set(sums) == {"manifest.json", *(f"{s}.zip" for s in release.SKILLS)}


def test_tracked_exclusions(repo, sandbox):
    forbidden = [
        ".mimosa/report.json", ".env", ".env.production", "tests/test_main.py",
        "scripts/__pycache__/main.pyc", "scripts/main.pyc", "scripts/.env.py",
        "scripts/.env.production", "scripts/secrets.py", "scripts/api_key.py",
        "scripts/credentials.py", "scripts/private_key.py", "scripts/id_rsa.py",
        "scripts/check_probe.py", "scripts/test_probe.py", "scripts/audit_probe.py",
        "scripts/tmp_probe.py", "scripts/temp_probe.py", "scripts/main.py.bak",
        "scripts/main.py~", "scripts/.main.py", "scripts/cache/cached.py",
        "scripts/data/record.py", "scripts/ledger/record.py", "cache/value.json",
        ".cache/value.json", ".pytest_cache/README.md", "data/snapshot.json",
        "ledger/run.jsonl", "references/secrets.md", "locales/token.json",
        "locales/snapshot.csv", "snapshot.json", "README.md.tmp",
        "scripts/nested/unknown.py", "references/nested/unknown.md",
    ]
    for name in forbidden:
        path = repo / "skills" / "trade" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not ship", encoding="utf-8")
    git(repo, "add", "-f", ".")
    git(repo, "commit", "-m", "tracked runtime and private files")
    output = sandbox / "release"
    release.build_release(repo, "HEAD", output)
    with zipfile.ZipFile(output / "trade.zip") as archive:
        assert not set(f"trade/{name}" for name in forbidden) & set(archive.namelist())
        assert len(archive.namelist()) == 6
    # Git cannot track its own .git directory; test the filter directly too.
    assert not release.allowed(".git/config")


def test_commit_only_and_reproducible(repo, sandbox):
    sha = git(repo, "rev-parse", "HEAD")
    first, second = sandbox / "first", sandbox / "second"
    release.build_release(repo, sha, first)
    script = repo / "skills" / "trade" / "scripts" / "main.py"
    script.write_text("dirty worktree", encoding="utf-8")
    (script.parent / "untracked.py").write_text("untracked", encoding="utf-8")
    (repo / "skills" / "trade" / "SKILL.md").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "different HEAD")
    script.write_text("different again", encoding="utf-8")
    second.mkdir()  # Existing empty directories are supported.
    release.build_release(repo, sha, second)
    assert {p.name: p.read_bytes() for p in first.iterdir()} == {
        p.name: p.read_bytes() for p in second.iterdir()}
    with zipfile.ZipFile(second / "trade.zip") as archive:
        assert archive.read("trade/scripts/main.py") == b"print('committed')\n"
        assert "trade/scripts/untracked.py" not in archive.namelist()


@pytest.mark.parametrize("existing", ["manifest.json", "trade.zip", ".hidden", "unrelated"])
def test_reject_nonempty_output_without_changes(repo, sandbox, existing):
    output = sandbox / "output"
    output.mkdir()
    sentinel = output / existing
    sentinel.write_bytes(b"user-owned")
    with pytest.raises(release.BuildError, match="empty"):
        release.build_release(repo, "HEAD", output)
    assert list(output.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"user-owned"


def test_reject_file_output(repo, sandbox):
    output = sandbox / "file"
    output.write_text("keep", encoding="utf-8")
    with pytest.raises(release.BuildError):
        release.build_release(repo, "HEAD", output)
    assert output.read_text() == "keep"


@pytest.mark.parametrize("commit", ["--help", "missing-ref"])
def test_invalid_commit_has_no_output(repo, sandbox, commit):
    output = sandbox / "output"
    with pytest.raises(release.BuildError):
        release.build_release(repo, commit, output)
    assert not output.exists()


def test_missing_skill_has_no_output(repo, sandbox):
    git(repo, "rm", "skills/stock/SKILL.md")
    git(repo, "commit", "-m", "missing entrypoint")
    output = sandbox / "output"
    with pytest.raises(release.BuildError, match="missing skills/stock/SKILL.md"):
        release.build_release(repo, "HEAD", output)
    assert not output.exists()


def test_reject_tracked_symlink(repo, sandbox):
    blob = git(repo, "hash-object", "-w", "skills/trade/SKILL.md")
    git(repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},skills/trade/scripts/link.py")
    git(repo, "commit", "-m", "link mode")
    output = sandbox / "output"
    with pytest.raises(release.BuildError, match="non-regular"):
        release.build_release(repo, "HEAD", output)
    assert not output.exists()


def test_cli_defaults_and_errors(repo, sandbox, monkeypatch, capsys):
    monkeypatch.setattr(release, "REPO", repo)
    output = sandbox / "release"
    assert release.main(["--output", str(output)]) == 0
    assert git(repo, "rev-parse", "HEAD") in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        release.main(["--commit", "HEAD", "--output", str(output)])
    assert error.value.code == 1
    assert "release build failed" in capsys.readouterr().err
