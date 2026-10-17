from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from generation_io import supervisor


def _make_fake_package(tmp_path: Path, *, large_venv_files: int = 0):
    package = tmp_path / "fake-package"
    runtime = package / "generation_io"
    (runtime / "nested").mkdir(parents=True)
    files = {
        runtime / "__init__.py": b"# runtime package\n",
        runtime / "nested" / "schema.json": b'{"runtime": true}\n',
        package / "pyproject.toml": b"[project]\nname='fake'\n",
        package / "uv.lock": b"version = 1\n",
        package / ".venv" / "lib" / "dependency.py": b"# dependency\n",
        package / "tests" / "test_noise.py": b"# test\n",
        package / "tools" / "tool.py": b"# tool\n",
        package / "cache" / "cached.json": b"{}\n",
        package / "output" / "generated.py": b"# generated\n",
    }
    for path, contents in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    for index in range(large_venv_files):
        dependency = package / ".venv" / "lib" / f"dependency-{index:04}.py"
        dependency.write_text("# ignored dependency\n", encoding="utf-8")
    return package, runtime, files


def _refs_by_path(refs):
    return {ref["path"]: ref for ref in refs}


def test_owned_code_refs_only_include_runtime_package_and_required_manifests(tmp_path, monkeypatch):
    package, runtime, files = _make_fake_package(tmp_path)
    monkeypatch.setattr(supervisor, "PACKAGE", package)

    refs = supervisor._owned_code_refs()

    expected_paths = {
        str(runtime / "__init__.py"),
        str(runtime / "nested" / "schema.json"),
        str(package / "pyproject.toml"),
        str(package / "uv.lock"),
    }
    assert {ref["path"] for ref in refs} == expected_paths
    expected_contents = {str(path): contents for path, contents in files.items() if str(path) in expected_paths}
    assert refs == [
        {
            "path": path,
            "bytes": len(expected_contents[path]),
            "sha256": hashlib.sha256(expected_contents[path]).hexdigest(),
        }
        for path in sorted(expected_paths)
    ]


def test_included_runtime_mutation_changes_refs_but_excluded_mutations_do_not(tmp_path, monkeypatch):
    package, runtime, _ = _make_fake_package(tmp_path)
    monkeypatch.setattr(supervisor, "PACKAGE", package)
    before = supervisor._owned_code_refs()

    source = runtime / "__init__.py"
    source.write_bytes(b"# changed runtime package\n")
    after_runtime_mutation = supervisor._owned_code_refs()
    assert after_runtime_mutation != before
    before_source = _refs_by_path(before)[str(source)]
    after_source = _refs_by_path(after_runtime_mutation)[str(source)]
    assert after_source["path"] == before_source["path"]
    assert after_source["bytes"] != before_source["bytes"]
    assert after_source["sha256"] != before_source["sha256"]

    (package / ".venv" / "lib" / "dependency.py").write_bytes(b"# mutated dependency\n")
    (package / "tests" / "test_noise.py").write_bytes(b"# mutated test\n")
    (package / "tools" / "tool.py").write_bytes(b"# mutated tool\n")
    (package / "cache" / "cached.json").write_bytes(b'{"changed": true}\n')
    (package / "output" / "generated.py").write_bytes(b"# mutated output\n")
    assert supervisor._owned_code_refs() == after_runtime_mutation


def test_large_venv_is_not_traversed_and_package_root_is_not_recursively_scanned(
    tmp_path, monkeypatch
):
    package, runtime, _ = _make_fake_package(tmp_path, large_venv_files=256)
    monkeypatch.setattr(supervisor, "PACKAGE", package)
    scanned_directories = []
    original_scandir = supervisor.os.scandir
    original_rglob = Path.rglob

    def recording_scandir(path):
        candidate = Path(path)
        if candidate.is_relative_to(package):
            scanned_directories.append(candidate)
        return original_scandir(path)

    def reject_package_rglob(path, *args, **kwargs):
        if Path(path).is_relative_to(package):
            raise AssertionError("package-root recursive scan is forbidden")
        return original_rglob(path, *args, **kwargs)

    monkeypatch.setattr(supervisor.os, "scandir", recording_scandir)
    monkeypatch.setattr(Path, "rglob", reject_package_rglob)

    refs = supervisor._owned_code_refs()

    assert len(refs) == 4
    assert set(scanned_directories) == {runtime, runtime / "nested"}
    assert all(not path.is_relative_to(package / ".venv") for path in scanned_directories)
    assert package not in scanned_directories


@pytest.mark.parametrize("link_kind", ["file", "directory"])
def test_runtime_symlink_files_and_directories_are_rejected(tmp_path, monkeypatch, link_kind):
    package, runtime, _ = _make_fake_package(tmp_path)
    if link_kind == "file":
        target = tmp_path / "external.py"
        target.write_text("# external\n", encoding="utf-8")
        link = runtime / "linked.py"
        link.symlink_to(target)
    else:
        target = tmp_path / "external-directory"
        target.mkdir()
        link = runtime / "linked-directory"
        link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(supervisor, "PACKAGE", package)

    with pytest.raises(supervisor.SupervisorError, match="supervisor_path_symlink"):
        supervisor._owned_code_refs()


@pytest.mark.parametrize("manifest", ["pyproject.toml", "uv.lock"])
def test_missing_required_manifest_is_rejected(tmp_path, monkeypatch, manifest):
    package, _, _ = _make_fake_package(tmp_path)
    (package / manifest).unlink()
    monkeypatch.setattr(supervisor, "PACKAGE", package)

    with pytest.raises(supervisor.SupervisorError, match="owned_code_manifest_missing"):
        supervisor._owned_code_refs()


def test_required_manifest_must_be_regular_and_not_a_symlink(tmp_path, monkeypatch):
    package, _, _ = _make_fake_package(tmp_path)
    (package / "pyproject.toml").unlink()
    outside_manifest = tmp_path / "outside.toml"
    outside_manifest.write_text("[project]\n", encoding="utf-8")
    (package / "pyproject.toml").symlink_to(outside_manifest)
    monkeypatch.setattr(supervisor, "PACKAGE", package)

    with pytest.raises(supervisor.SupervisorError, match="supervisor_path_symlink"):
        supervisor._owned_code_refs()

    (package / "pyproject.toml").unlink()
    (package / "pyproject.toml").mkdir()
    with pytest.raises(supervisor.SupervisorError, match="owned_code_ref_not_regular"):
        supervisor._owned_code_refs()
