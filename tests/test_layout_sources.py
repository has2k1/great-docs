from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from yaml12 import read_yaml, write_yaml

from great_docs import GreatDocs


@pytest.fixture(params=[".", "docs", "website"])
def source_project(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, Path]:
    root = tmp_path
    source = root / request.param
    source.mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "sample"\nversion = "1.0"\n')
    (source / "great-docs.yml").write_text("module: sample\n")
    return root, source


def make_docs(root: Path, source: Path) -> GreatDocs:
    return GreatDocs(str(root), config_path=str(source / "great-docs.yml"))


@pytest.mark.parametrize("explicit", [False, True])
def test_documentation_sources(source_project: tuple[Path, Path], explicit: bool) -> None:
    root, source = source_project
    guide = "user_guide" if explicit else "manual"
    for directory in (guide, "recipes", "custom", "assets", "hooks", "notebooks"):
        (source / directory).mkdir()
    for name in (f"{guide}/01-start.qmd", "recipes/start.qmd", "custom/about.qmd"):
        (source / name).write_text("---\ntitle: Start\n---\nHello\n")
    for name in ("references.bib", "citation.csl", "style.css", "hooks/prepare.py", "header.html"):
        (source / name).write_text("fixture")
    (source / "assets/logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (source / "README.md").write_text("# Local documentation\n![Logo](assets/logo.svg)\n")
    write_yaml(
        {
            "module": "sample",
            "user_guide": [{"section": "Guide", "contents": ["01-start.qmd"]}]
            if explicit
            else guide,
            "sections": [{"title": "Recipes", "dir": "recipes"}],
            "custom_pages": [{"dir": "custom", "output": "custom"}],
            "bibliography": "references.bib",
            "csl": "citation.csl",
            "site": {"css": "style.css"},
            "pre_render": ["hooks/prepare.py"],
            "logo": "assets/logo.svg",
            "hero": False,
            "favicon": "assets/logo.svg",
            "include_in_header": [{"file": "header.html"}],
            "authors": [{"name": "Example", "image": "assets/logo.svg"}],
        },
        source / "great-docs.yml",
    )
    gd = make_docs(root, source)
    gd._prepare_build_directory()
    gd._copy_assets()
    gd._copy_user_guide_to_docs(gd._discover_user_guide())
    gd._process_sections()
    gd._process_custom_pages()
    build = gd.build_dir
    assert build == (root / "great-docs" if root == source else source / "_quarto/default")
    assert not hasattr(gd, "project_path")
    assert not hasattr(gd, "docs_dir")
    for name in (
        "_quarto.yml",
        "mermaid-renderer.js",
        "references.bib",
        "citation.csl",
        "style.css",
        "scripts/prepare.py",
        "scripts/post-render.py",
        "assets/logo.svg",
        "recipes/start.qmd",
        "custom/about.qmd",
        "_includes/header.html",
        "_shared/authors/logo.svg",
        "favicon.svg",
    ):
        assert (build / name).is_file(), name
    guide_name = "01-start.qmd" if explicit else "start.qmd"
    assert (build / "user-guide" / guide_name).is_file()
    config = read_yaml(build / "_quarto.yml")
    assert config["project"]["output-dir"] == "_site"
    assert "scripts/prepare.py" in config["project"]["pre-render"]
    assert config["bibliography"] == "references.bib"
    assert {"file": "_includes/header.html"} in config["format"]["html"]["include-in-header"]
    assert gd._find_package_root() == root
    assert gd.layout.cache_dir == root / ".great-docs-cache"
    if source != root:
        assert not (source / "_quarto/_quarto.yml").exists()


def test_landing_precedence_and_shared_references(source_project: tuple[Path, Path]) -> None:
    root, source = source_project
    (root / "assets").mkdir()
    (root / "assets/logo.svg").write_text("shared logo")
    (root / "README.md").write_text("# Package\n![Logo](assets/logo.svg?raw=1#logo)")
    gd = make_docs(root, source)
    gd.build_dir.mkdir(parents=True)
    assert gd._find_index_source_file()[0] == root / "README.md"
    if source != root:
        (source / "index.md").write_text(
            '![Logo](../assets/logo.svg) <img src="../assets/logo.svg#mark"> [Download](../assets/logo.svg)'
        )
        assert gd._find_index_source_file()[0] == source / "index.md"
    page = gd._find_index_source_file()[0]
    assert page is not None
    content = gd._rebase_source_references(page.read_text(), page, gd.build_dir / "index.qmd")
    assert "../assets" not in content
    assert "logo.svg" in content
    assert list(gd.build_dir.rglob("*.svg"))
    assert not (source / "_quarto/assets").exists()


def test_duplicate_hook_names_fail_before_copy(source_project: tuple[Path, Path]) -> None:
    root, source = source_project
    for directory in ("a", "b"):
        (source / directory).mkdir()
        (source / directory / "prepare.py").write_text(directory)
    write_yaml(
        {"module": "sample", "pre_render": ["a/prepare.py", "b/prepare.py"]},
        source / "great-docs.yml",
    )
    gd = make_docs(root, source)
    with pytest.raises(ValueError, match="hook|script"):
        gd._prepare_build_directory()


def test_nested_sources_and_shared_assets(source_project: tuple[Path, Path]) -> None:
    root, source = source_project
    (root / "shared").mkdir()
    (root / "shared/plot.svg").write_text("plot")
    (source / "user_guide/deep").mkdir(parents=True)
    prefix = "../" if source == root else "../../"
    page = source / "user_guide/deep/example.qmd"
    page.write_text(f"---\ntitle: Example\n---\n![Plot]({prefix}../shared/plot.svg#panel)")
    gd = make_docs(root, source)
    gd.build_dir.mkdir(parents=True)
    gd._copy_user_guide_to_docs(gd._discover_user_guide())
    generated = gd.build_dir / "user-guide/deep/example.qmd"
    content = generated.read_text()
    asset = "shared/plot.svg" if source == root else "_shared/shared/plot.svg"
    assert f"{asset}#panel" in content
    assert (gd.build_dir / asset).read_text() == "plot"


def test_asset_collision_is_rejected(source_project: tuple[Path, Path]) -> None:
    root, source = source_project
    (source / "image.png").write_text("source")
    gd = make_docs(root, source)
    gd.build_dir.mkdir(parents=True)
    (gd.build_dir / "image.png").write_text("generated")
    with pytest.raises(ValueError, match="conflict"):
        gd._rebase_source_references(
            "![Image](image.png)", source / "README.md", gd.build_dir / "index.qmd"
        )
    assert (gd.build_dir / "image.png").read_text() == "generated"


def test_shared_section_keeps_slug_and_custom_output_is_contained(
    source_project: tuple[Path, Path],
) -> None:
    root, source = source_project
    gd = make_docs(root, source)
    configured = "recipes" if root == source else "../recipes"
    assert gd._section_build_dir({"dir": configured}) == gd.build_dir / "recipes"
    nested = "guides/recipes" if root == source else "../guides/recipes"
    assert gd._section_build_dir({"dir": nested}) == gd.build_dir / "guides/recipes"
    for output in ("../escape", str(root / "escape")):
        with pytest.raises(ValueError, match="inside"):
            gd._output_directory(output)


def test_notebooks_use_documentation_root(source_project: tuple[Path, Path]) -> None:
    root, source = source_project
    (source / "notebooks").mkdir()
    (source / "notebooks/data.json").write_text("{}")
    write_yaml({"module": "sample", "marimo": True}, source / "great-docs.yml")
    gd = make_docs(root, source)
    marimo = MagicMock()
    marimo.get_islands_head_html.return_value = ""
    with (
        patch("importlib.util.find_spec", return_value=object()),
        patch.dict("sys.modules", {"great_docs._marimo": marimo}),
    ):
        gd._prepare_build_directory()
    assert (gd.build_dir / "notebooks/data.json").read_text() == "{}"


def test_api_reference_build_uses_selected_cwd(
    source_project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from great_docs._apiref.api_reference import APIReference

    class ReferenceReached(BaseException):
        pass

    root, source = source_project
    gd = make_docs(root, source)
    original_cwd = Path.cwd()
    prepare = gd._prepare_build_directory

    def prepare_reference() -> None:
        prepare()
        gd._has_api_reference = True

    def check_cwd(ref: APIReference) -> None:
        assert Path.cwd() == gd.build_dir
        raise ReferenceReached

    monkeypatch.setattr(gd, "_prepare_build_directory", prepare_reference)
    monkeypatch.setattr(APIReference, "__init__", lambda self, path: None)
    monkeypatch.setattr(APIReference, "build", check_cwd)
    with pytest.raises(ReferenceReached):
        gd.build(refresh=False)
    assert Path.cwd() == original_cwd


def test_landing_links_follow_generated_page_paths(source_project: tuple[Path, Path]) -> None:
    root, source = source_project
    (source / "user_guide").mkdir()
    (source / "user_guide/01-start.qmd").write_text("# Start")
    gd = make_docs(root, source)
    content = "[Start](user_guide/01-start.qmd#intro)"
    assert (
        gd._rebase_source_references(content, source / "index.md", gd.build_dir / "index.qmd")
        == "[Start](user-guide/start.qmd#intro)"
    )


def test_rebasing_preserves_code_examples(source_project: tuple[Path, Path]) -> None:
    root, source = source_project
    (source / "logo.svg").write_text("logo")
    gd = make_docs(root, source)
    content = '```markdown\n![Logo](logo.svg)\n```\n`![Logo](logo.svg)`\n    <img src="logo.svg">\n'
    assert (
        gd._rebase_source_references(content, source / "index.md", gd.build_dir / "index.qmd")
        == content
    )
    assert not gd.build_dir.exists()
