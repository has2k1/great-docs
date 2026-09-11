import io
import os
from pathlib import Path

import pytest
from yaml12 import read_yaml

from great_docs._layout import Layout
from great_docs._layout_migration import analyse
from great_docs._layout_migration.model import Move, fingerprint
from great_docs._utils import QUARTO_YML_HEADER


def put(root: Path, relative: str, content: str | bytes = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    put(tmp_path, "pyproject.toml", '[project]\nname = "sample"\nversion = "1.0"\n')
    put(tmp_path, "great-docs.yml", "module: sample\n")
    put(tmp_path, "sample/__init__.py")
    return tmp_path


def snapshot(root: Path) -> dict[str, tuple[int, bytes | str]]:
    return {
        str(path.relative_to(root)): (
            path.lstat().st_mode,
            os.readlink(path)
            if path.is_symlink()
            else path.read_bytes()
            if path.is_file()
            else b"",
        )
        for path in root.rglob("*")
    }


@pytest.mark.parametrize("destination", ["docs", "website/manual"])
def test_analysis_is_read_only_with_shared_assets(project: Path, destination: str) -> None:
    put(project, "great-docs.yml", "bibliography: refs.bib # Citation\nsections: [{dir: essays}]\n")
    put(project, "refs.bib", "@book{ref}\n")
    put(project, "assets/chart.png", b"\x00\xff")
    put(project, "user_guide/page.qmd", "![Chart](../assets/chart.png)\n")
    put(project, "essays/one.md", "# One\n")
    put(project, "custom/about.html", "<h1>About</h1>\n")
    put(project, "index.qmd", "# Homepage\n")
    put(project, "README.md", "# Package\n")
    put(project, ".gitignore", "great-docs/\n_freeze/\n")
    before = snapshot(project)
    migration = analyse(Layout.make(project), Path(destination))
    assert snapshot(project) == before
    assert not migration.blockers
    for name in ("great-docs.yml", "user_guide", "essays", "custom", "index.qmd"):
        assert Move(project / name, project / destination / name) in migration.moves
    assert not any(
        move.source.name in {"assets", "README.md", "sample"} for move in migration.moves
    )
    fingerprints = dict(migration.fingerprints)
    assert project / "assets/chart.png" in fingerprints
    assert project / "refs.bib" in fingerprints
    assert project / "user_guide" in fingerprints
    edits = {edit.path: edit for edit in migration.edits}
    prefix = "../" * (len(Path(destination).parts) + 1)
    assert (
        edits[project / "user_guide/page.qmd"].after
        == f"![Chart]({prefix}assets/chart.png)\n".encode()
    )
    assert edits[project / ".gitignore"].after == (
        f"great-docs/\n_freeze/\n/{destination}/_quarto/\n/{destination}/_site/\n".encode()
    )


@pytest.mark.parametrize("target", ["great-docs.yml", "user_guide", "_freeze"])
def test_destination_collisions_block_without_writes(project: Path, target: str) -> None:
    put(project, "user_guide/page.md", "# Page")
    put(project, "_freeze/page/cache.json", b"\xff\x00")
    put(project, f"docs/{target}", "User file")
    before = snapshot(project)
    result = analyse(Layout.make(project, project / "great-docs.yml"), Path("docs"))
    assert any(target in message for message in result.blockers)
    assert snapshot(project) == before


@pytest.mark.parametrize("source", ["sample", "src", "user_guide/sub", "great-docs", "docs/inside"])
def test_overlapping_sources_are_blocked(project: Path, source: str) -> None:
    put(project, "great-docs.yml", f"sections: [{{dir: {source}}}]\n")
    put(project, "user_guide/sub/page.md", "# Page")
    put(project, f"{source}/other.md", "# Other")
    result = analyse(Layout.make(project), Path("docs"))
    assert result.blockers


@pytest.mark.parametrize("kind", ["source", "child", "destination", "shared", "freeze"])
def test_symlinks_are_blocked(project: Path, kind: str) -> None:
    target = put(project, "retained.txt", "Original")
    if kind == "source":
        (project / "user_guide").symlink_to(project / "sample", target_is_directory=True)
    elif kind == "child":
        (project / "user_guide").mkdir()
        (project / "user_guide/link").symlink_to(target)
    elif kind == "destination":
        (project / "docs").symlink_to(project / "sample", target_is_directory=True)
    elif kind == "shared":
        (project / "refs.bib").symlink_to(target)
        put(project, "great-docs.yml", "bibliography: refs.bib\n")
    else:
        (project / "_freeze").symlink_to(project / "sample", target_is_directory=True)
    before = snapshot(project)
    result = analyse(Layout.make(project), Path("docs"))
    assert any("symlink" in message.lower() for message in result.blockers)
    assert snapshot(project) == before


def test_read_failure_is_a_blocker(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = put(project, "user_guide/page.md", "# Page")
    original = Path.read_bytes

    def fail_selected(self: Path) -> bytes:
        if self == path:
            raise PermissionError("Cannot read selected page")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", fail_selected)
    result = analyse(Layout.make(project), Path("docs"))
    assert any("page.md" in message for message in result.blockers)


def test_already_migrated_is_noop(project: Path) -> None:
    config = put(project, "docs/great-docs.yml", "module: sample\n")
    before = snapshot(project)
    result = analyse(Layout.make(project, config), Path("docs"))
    assert not result.moves and not result.edits and not result.blockers
    assert any("already" in message.lower() for message in result.follow_up)
    assert snapshot(project) == before


def test_freeze_move_preserves_bytes_and_generated_trees(project: Path) -> None:
    put(project, "_freeze/page/cache.js", b"\xff\x00no newline")
    put(project, "great-docs/_quarto.yml", QUARTO_YML_HEADER)
    put(project, "great-docs/_freeze/page/cache.js", b"different")
    before = snapshot(project)
    result = analyse(Layout.make(project), Path("docs"))
    assert not result.blockers
    assert Move(project / "_freeze", project / "docs/_freeze") in result.moves
    assert not any(edit.path.name == "cache.js" for edit in result.edits)
    assert any("great-docs" in message for message in result.follow_up)
    assert snapshot(project) == before


def test_freeze_recovery_merges_historical_first_latest_last(project: Path) -> None:
    for name in ("great-docs-v1", "great-docs-v2", "great-docs"):
        put(project, f"{name}/_quarto.yml", QUARTO_YML_HEADER)
        put(project, f"{name}/_freeze/page/cache.js", name.encode())
    put(project, "great-docs-v1/_freeze/old/cache.json", b"\xffold")
    put(project, "great-docs-unmarked/_freeze/ignored.json", "Ignore")
    before = snapshot(project)
    result = analyse(Layout.make(project), Path("docs"))
    assert not result.blockers
    edits = {edit.path.relative_to(project).as_posix(): edit for edit in result.edits}
    assert edits["docs/_freeze/page/cache.js"].before is None
    assert edits["docs/_freeze/page/cache.js"].after == b"great-docs"
    assert edits["docs/_freeze/old/cache.json"].after == b"\xffold"
    assert "docs/_freeze/ignored.json" not in edits
    assert snapshot(project) == before


def test_implicit_logos_become_explicit_without_losing_settings(project: Path) -> None:
    put(project, "great-docs.yml", "logo: {alt: 'Sample'} # Keep\nhero:\n  tagline: Simple\n")
    put(project, "assets/logo.svg", "<svg/>")
    put(project, "assets/logo-dark.svg", "<svg>dark</svg>")
    put(project, "logo-hero.png", b"hero")
    result = analyse(Layout.make(project), Path("docs"))
    assert not result.blockers
    edit = next(edit for edit in result.edits if edit.path == project / "great-docs.yml")
    config = read_yaml(io.StringIO(edit.after.decode()))
    assert config["logo"] == {
        "alt": "Sample",
        "light": "../assets/logo.svg",
        "dark": "../assets/logo-dark.svg",
    }
    assert config["hero"]["logo"] == {"light": "../logo-hero.png", "dark": "../logo-hero.png"}
    assert config["hero"]["tagline"] == "Simple"
    assert "# Keep" in edit.after.decode()


def test_automation_follow_up_names_file_and_new_site(project: Path) -> None:
    put(project, ".github/workflows/docs.yml", "path: great-docs/_site\n")
    result = analyse(Layout.make(project), Path("website"))
    assert any(
        ".github/workflows/docs.yml" in message and "website/_site" in message
        for message in result.follow_up
    )


@pytest.mark.parametrize("change", ["add", "remove", "mode", "symlink"])
def test_directory_fingerprint_detects_stale_inventory(project: Path, change: str) -> None:
    path = put(project, "user_guide/page.md", "# Page")
    directory = path.parent
    before = fingerprint(directory)
    if change == "add":
        put(project, "user_guide/new.md", "# New")
    elif change == "remove":
        path.unlink()
    elif change == "mode":
        path.chmod(0o700)
    else:
        (directory / "linked").symlink_to(path)
    assert fingerprint(directory) != before


def test_package_readme_stays_at_root_with_rebased_guide_link(project: Path) -> None:
    put(project, "README.md", "[Guide](user_guide/page.md)\n")
    put(project, "user_guide/page.md", "# Page")
    result = analyse(Layout.make(project), Path("docs"))
    edit = next(edit for edit in result.edits if edit.path == project / "README.md")
    assert edit.after == b"[Guide](docs/user_guide/page.md)\n"
    assert not any(move.source == project / "README.md" for move in result.moves)
    assert not result.blockers


def test_absolute_source_directory_is_retained(project: Path) -> None:
    directory = project / "essays"
    put(project, "essays/page.md", "# Page")
    put(project, "great-docs.yml", f"sections: [{{dir: '{directory}'}}]\n")
    result = analyse(Layout.make(project), Path("docs"))
    assert not result.blockers
    assert not any(move.source == directory for move in result.moves)
    assert directory in dict(result.fingerprints)
    assert not any(edit.path == project / "great-docs.yml" for edit in result.edits)


def test_nested_package_blocks_document_directory_move(project: Path) -> None:
    put(project, "user_guide/nested_package/__init__.py")
    result = analyse(Layout.make(project), Path("docs"))
    assert any(
        "package" in message.lower() and "user_guide" in message for message in result.blockers
    )


def test_recovered_cache_file_directory_collision_blocks(project: Path) -> None:
    for name in ("great-docs-old", "great-docs"):
        put(project, f"{name}/_quarto.yml", QUARTO_YML_HEADER)
    put(project, "great-docs-old/_freeze/page", "File")
    put(project, "great-docs/_freeze/page/data.json", "Cache")
    result = analyse(Layout.make(project), Path("docs"))
    assert any(
        "cache" in message.lower() and "overlap" in message.lower() for message in result.blockers
    )


def test_missing_configuration_blocks_instead_of_proposing_empty_file(project: Path) -> None:
    (project / "great-docs.yml").unlink()
    result = analyse(Layout.make(project), Path("docs"))
    assert result.blockers
    assert not result.moves


def test_root_freeze_must_be_a_directory(project: Path) -> None:
    put(project, "_freeze", "Not a cache directory")
    result = analyse(Layout.make(project), Path("docs"))
    assert any("cache" in message.lower() for message in result.blockers)


def test_destination_rejects_symlink_before_parent_normalisation(project: Path) -> None:
    (project / "link").symlink_to(project / "sample", target_is_directory=True)
    result = analyse(Layout.make(project), Path("link/../docs"))
    assert any("symlink" in message.lower() for message in result.blockers)


def test_absolute_file_reference_cannot_point_into_a_moved_directory(project: Path) -> None:
    asset = put(project, "user_guide/refs.bib", "@book{ref}")
    put(project, "great-docs.yml", f"bibliography: '{asset}'\n")
    result = analyse(Layout.make(project), Path("docs"))
    assert any(
        "absolute" in message.lower() and "refs.bib" in message for message in result.blockers
    )


def test_unreferenced_implicit_asset_is_reported(project: Path) -> None:
    put(project, "assets/download.zip", b"download")
    result = analyse(Layout.make(project), Path("docs"))
    assert any(
        "download.zip" in message and "implicit" in message.lower() for message in result.blockers
    )


def test_migrated_shared_asset_is_staged_under_the_build(project: Path) -> None:
    from great_docs import GreatDocs

    put(project, "assets/chart.png", b"chart")
    put(
        project,
        "user_guide/01-start.qmd",
        "---\ntitle: Start\n---\n![Chart](../assets/chart.png)\n",
    )
    migration = analyse(Layout.make(project), Path("docs"))
    assert not migration.blockers
    for edit in migration.edits:
        edit.path.parent.mkdir(parents=True, exist_ok=True)
        edit.path.write_bytes(edit.after)
    for move in migration.moves:
        move.destination.parent.mkdir(parents=True, exist_ok=True)
        move.source.rename(move.destination)
    gd = GreatDocs(str(project))
    gd._prepare_build_directory()
    gd._copy_user_guide_to_docs(gd._discover_user_guide())
    page = gd.build_dir / "user-guide/start.qmd"
    assert "![Chart](../_shared/assets/chart.png)" in page.read_text()
    assert (gd.build_dir / "_shared/assets/chart.png").read_bytes() == b"chart"


def test_retained_narrative_links_follow_moved_guides(project: Path) -> None:
    directory = project / "essays"
    page = put(project, "essays/page.md", "[Guide](../user_guide/start.md)\n")
    put(project, "user_guide/start.md", "# Start")
    put(project, "great-docs.yml", f"sections: [{{dir: '{directory}'}}]\n")
    result = analyse(Layout.make(project), Path("docs"))
    assert not result.blockers
    edit = next(edit for edit in result.edits if edit.path == page)
    assert edit.after == b"[Guide](../docs/user_guide/start.md)\n"


def test_recursive_terminal_recordings_have_file_specific_follow_up(project: Path) -> None:
    recording = put(project, "examples/nested/demo.termshow", "Recording")
    companion = put(project, "examples/nested/demo.yml", "command: echo demo")
    result = analyse(Layout.make(project), Path("docs"))
    assert any(str(recording) in message for message in result.follow_up)
    assert recording in dict(result.fingerprints)
    assert companion in dict(result.fingerprints)


@pytest.mark.parametrize("directory", ["user_guide", "_freeze", "great-docs/_freeze"])
def test_preview_fingerprint_detects_changed_tree_hierarchy(project: Path, directory: str) -> None:
    if directory.startswith("great-docs/"):
        put(project, "great-docs/_quarto.yml", QUARTO_YML_HEADER)
    tree = project / directory
    (tree / "a").mkdir(parents=True)
    sibling = put(tree, "b", b"same bytes")
    migration = analyse(Layout.make(project), Path("docs"))
    assert not migration.blockers
    before = dict(migration.fingerprints)[tree]
    sibling.rename(tree / "a/b")
    assert fingerprint(tree) != before


def test_intermediate_destination_file_blocks_without_writes(project: Path) -> None:
    put(project, "great-docs.yml", "sections: [{dir: tutorials/guides}]\n")
    put(project, "tutorials/guides/start.md", "# Start")
    obstruction = put(project, "docs/tutorials", "User file")
    before = snapshot(project)
    migration = analyse(Layout.make(project), Path("docs"))
    assert any(str(obstruction) in message for message in migration.blockers)
    assert snapshot(project) == before


@pytest.mark.parametrize("extension", ["html", "qmd"])
def test_generated_reference_links_do_not_block_analysis(project: Path, extension: str) -> None:
    reference = f"reference/sample.api.{extension}?view=full#usage"
    content = f"[API]({reference})\n"
    put(project, "index.qmd", content)
    put(project, "user_guide/start.qmd", f"[API](../{reference})\n")
    before = snapshot(project)
    migration = analyse(Layout.make(project), Path("docs"))
    assert not migration.blockers
    assert not any(edit.path.suffix == ".qmd" for edit in migration.edits)
    assert snapshot(project) == before
