"""Verify published layout contracts with real Git histories and Quarto renders"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from great_docs._layout import Layout
from great_docs._layout_migration import analyse, apply

pytestmark = pytest.mark.xdist_group("layout_rendered")

ROOT = Path(__file__).resolve().parents[1]
IMAGE = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="blue"/></svg>\n'
PACKAGE = '''"""A package for rendered layout acceptance"""

def shared(value: int = 1) -> int:
    """
    Return the shared value

    Parameters
    ----------
    value
        Value to return.

    Returns
    -------
    value
        The unchanged value.
    """
    return value
'''
LATEST = '''
def latest_only() -> str:
    """Identify an API added after the historical release"""
    return "latest"
'''
BUILD = """
import sys
from great_docs import GreatDocs
GreatDocs._fetch_github_releases = lambda self, *args, **kwargs: []
GreatDocs(project_path=sys.argv[1], config_path=sys.argv[2]).build()
"""


class Page(HTMLParser):
    """Collect semantic text and resource references from emitted HTML"""

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.text: list[str] = []
        self.links: list[str] = []
        self.images: list[str] = []
        self.resources: list[str] = []
        self.ids: set[str] = set()
        self.main_text: list[str] = []
        self.main_links: list[str] = []
        self._main = False
        self.feed(path.read_text())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "main":
            self._main = True
        if values.get("id"):
            self.ids.add(str(values["id"]))
        if tag == "a" and values.get("href"):
            self.links.append(str(values["href"]))
            if self._main:
                self.main_links.append(str(values["href"]))
        if tag == "img" and values.get("src"):
            self.images.append(str(values["src"]))
        if tag == "script" and values.get("src"):
            self.resources.append(str(values["src"]))
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.resources.append(str(values["href"]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self._main = False

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._main:
            self.main_text.append(data)


def put(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Rendered:
    layout: Layout
    tag: str | None
    executions: Path


@pytest.fixture(scope="module")
def required_tools() -> None:
    for name in ("git", "tar", "node", "quarto"):
        assert shutil.which(name), f"Real layout acceptance requires {name} on PATH"
    result = subprocess.run(
        [sys.executable, "-c", "import nbformat, jupyter_client, ipykernel"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Real layout acceptance requires Jupyter: {result.stderr}"
    version = subprocess.run(["quarto", "--version"], check=True, capture_output=True, text=True)
    print(f"Layout acceptance Quarto: {version.stdout.strip()}")


def make_project(root: Path, directory: str, tag: str | None, migrate: bool) -> Rendered:
    source = root if migrate else root / directory
    source.mkdir(parents=True, exist_ok=True)
    put(root, "pyproject.toml", '[project]\nname = "layout-sample"\nversion = "2.0.0"\n')
    put(root, "src/layout_sample/__init__.py", PACKAGE)
    put(root, "README.md", "# Layout sample\n\nPackage README fallback sentinel.\n")
    put(root, "shared/picture.svg", IMAGE)
    put(
        root,
        "shared/references.bib",
        "@book{layoutref, title={Layout bibliography sentinel}, author={Example, Ada}, year={2020}}\n",
    )
    shared = "shared" if source == root else "../shared"
    guide = f"""---
title: Frozen guide
jupyter: python3
---

Guide prose sentinel. See [the homepage](../index.qmd).

![Shared image](../{shared}/picture.svg)

Read the shared reference [@layoutref].

```{{python}}
import os
from pathlib import Path
with Path(os.environ["GD_LAYOUT_EXECUTION_LOG"]).open("a") as output:
    output.write("executed\\n")
print("Frozen execution sentinel")
```
"""
    put(source, "user_guide/01-frozen.qmd", guide)
    config_text = f"""module: layout_sample
display_name: Layout sample
dynamic: false
repo: https://github.com/layout-example/layout-sample
github_style: icon
site_url: https://layout.example.test
pypi: false
changelog:
  enabled: false
skill:
  enabled: false
mcp:
  enabled: false
freeze: auto
bibliography: {shared}/references.bib
"""
    if tag:
        config_text += f'''versions:
  - tag: "2.0.0"
    latest: true
  - tag: "{tag}"
    git_ref: "{tag}"
'''
    config = put(source, "great-docs.yml", config_text)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Layout acceptance")
    git(root, "config", "user.email", "layout@example.test")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Create historical fixture")
    if tag:
        git(root, "tag", tag)
    put(root, "src/layout_sample/__init__.py", PACKAGE + LATEST)
    git(root, "add", "src/layout_sample/__init__.py")
    git(root, "commit", "-qm", "Add latest API")
    git(root, "tag", "2.0.0")
    layout = Layout.make(root, config)
    if migrate:
        proposal = analyse(layout, root / "docs")
        assert not proposal.blockers, proposal.blockers
        apply(proposal)
        layout = Layout.make(root)
        assert (root / "shared/picture.svg").read_text() == IMAGE
        assert (root / "README.md").is_file()
    return Rendered(layout, tag, root / "executions.log")


CASES = [(directory, tag) for directory in (".", "docs", "website") for tag in ("1.5.0", "v1.5.0")]
CASES += [("docs", None), ("website", None)]


@pytest.fixture(scope="module", params=CASES, ids=lambda case: f"{case[0]}-{case[1] or 'single'}")
def rendered(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, required_tools: None
) -> Rendered:
    directory, tag = request.param
    root = tmp_path_factory.mktemp(f"layout-{directory.replace('.', 'root')}-{tag or 'single'}")
    fixture = make_project(root, directory, tag, migrate=directory == "docs" and tag == "1.5.0")
    env = os.environ.copy()
    env["GD_LAYOUT_EXECUTION_LOG"] = str(fixture.executions)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(root / "src"), env.get("PYTHONPATH", "")))
    for attempt in (1, 2):
        result = subprocess.run(
            [sys.executable, "-c", BUILD, str(root), str(fixture.layout.config_path)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        put(root, f"build-{attempt}.log", result.stdout + result.stderr)
        assert result.returncode == 0, (
            f"Build {attempt} failed in {root}:\n{result.stdout}\n{result.stderr}"
        )
        assert (fixture.layout.site_dir / "index.html").is_file(), (
            f"No published site in {root}; inspect build-{attempt}.log"
        )
        if tag:
            assert (fixture.layout.site_dir / "v/1.5.0/index.html").is_file(), (
                f"Missing historical output after build {attempt}: {root}"
            )
        assert fixture.executions.read_text().splitlines() == ["executed"] * (2 if tag else 1), (
            f"Freeze execution count changed on build {attempt}: {root}"
        )
    print(f"Rendered twice: {fixture.layout.site_dir}")
    return fixture


def assert_local_targets(page_path: Path, site: Path) -> Page:
    page = Page(page_path)
    for link in page.links + page.images + page.resources:
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (
            site / unquote(parsed.path).lstrip("/")
            if parsed.path.startswith("/")
            else page_path.parent / unquote(parsed.path)
        )
        if target.is_dir():
            target /= "index.html"
        assert target.is_file(), f"Broken local target {link!r} from {page_path}"
    return page


def test_real_layout_outputs(rendered: Rendered) -> None:
    layout, tag = rendered.layout, rendered.tag
    latest = assert_local_targets(layout.site_dir / "index.html", layout.site_dir)
    assert "Package README fallback sentinel." in " ".join(latest.text)
    guide = assert_local_targets(layout.site_dir / "user-guide/frozen.html", layout.site_dir)
    assert "Guide prose sentinel." in " ".join(guide.text)
    assert "Frozen execution sentinel" in " ".join(guide.text)
    assert "ref-layoutref" in guide.ids
    assert any(
        (layout.site_dir / "user-guide" / src).resolve().read_text() == IMAGE
        for src in guide.images
        if "picture.svg" in src
    )
    api = assert_local_targets(layout.site_dir / "reference/shared.html", layout.site_dir)
    assert any(
        "github.com/layout-example/layout-sample/blob/2.0.0/src/layout_sample/__init__.py" in link
        for link in api.links
    )
    assert (layout.site_dir / "reference/latest_only.html").is_file()
    assert layout.freeze_dir.is_dir()
    if layout.source_dir != layout.package_root:
        assert layout.build_dir == layout.source_dir / "_quarto/default"
        assert not (layout.source_dir / "_quarto/_quarto.yml").exists()
        assert not (layout.source_dir / "_quarto/2.0.0").exists()
        assert (layout.build_dir / "_site/index.html").is_file()
    if tag:
        historical = layout.build_dir_for(tag, "2.0.0")
        assert historical.parent == layout.build_dir.parent
        assert historical.name == (
            f"great-docs-{tag}" if layout.source_dir == layout.package_root else tag
        )
        assert (historical / "_site/index.html").is_file()
        old = layout.site_dir / "v/1.5.0"
        assert_local_targets(old / "index.html", layout.site_dir)
        assert_local_targets(old / "user-guide/frozen.html", layout.site_dir)
        assert (old / "reference/shared.html").is_file()
        assert not (old / "reference/latest_only.html").exists()
        assert not (layout.site_dir / "v/v1.5.0").exists()
        assert (layout.cache_dir / "snapshots" / f"{tag}.json").is_file()
        manifest = json.loads((layout.site_dir / "_version_map.json").read_text())
        assert [(version["tag"], version["path_prefix"]) for version in manifest["versions"]] == [
            ("2.0.0", ""),
            (tag, "v/1.5.0"),
        ]


@pytest.fixture(scope="module")
def semantic_baselines() -> dict[str, tuple[str, tuple[str, ...]]]:
    return {}


def test_semantics_match_across_layouts(
    rendered: Rendered, semantic_baselines: dict[str, tuple[str, tuple[str, ...]]]
) -> None:
    for prefix in ["", "v/1.5.0/"] if rendered.tag else [""]:
        for name in ("index.html", "user-guide/frozen.html", "reference/shared.html"):
            page = Page(rendered.layout.site_dir / prefix / name)
            semantic = (
                re.sub(r"\s+", " ", " ".join(page.main_text)).strip(),
                tuple(sorted(page.main_links)),
            )
            key = f"{rendered.tag}:{prefix}{name}"
            assert semantic == semantic_baselines.setdefault(key, semantic), key


def test_emitted_version_navigation(rendered: Rendered) -> None:
    if rendered.tag is None:
        assert not (rendered.layout.site_dir / "_version_map.json").exists()
        return
    site = rendered.layout.site_dir
    manifest = json.loads((site / "_version_map.json").read_text())
    assert manifest["pages"]["reference/latest_only.html"] == ["2.0.0"]
    script = (site / "version-selector.js").read_text()
    html = (site / "v/1.5.0/user-guide/frozen.html").read_text()
    canonical = [
        body
        for body in re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        if 'link.rel="canonical"' in body
    ]
    assert len(canonical) == 1
    harness = r"""
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const links = [];
const context = {
  window: {location: {pathname: "/v/1.5.0/user-guide/frozen.html"}},
  document: {readyState: "loading", addEventListener() {}}
};
const source = input.script.replace('if (document.readyState === "loading")',
  'globalThis.selector = {getSiteBasePath, detectCurrentVersion, getCurrentRelPath, buildVersionUrl}; if (document.readyState === "loading")');
vm.runInNewContext(source, context);
const selector = context.selector, map = input.map;
const base = selector.getSiteBasePath(map);
const result = {
  base, tag: selector.detectCurrentVersion(map),
  page: selector.getCurrentRelPath(map, base),
  latest: selector.buildVersionUrl(map, "2.0.0", "user-guide/frozen.html", base),
  historical: selector.buildVersionUrl(map, input.tag, "user-guide/frozen.html", base),
  absent: selector.buildVersionUrl(map, input.tag, "reference/latest_only.html", base)
};
context.document = {
  addEventListener(event, callback) {callback()},
  createElement() {return {}}, head: {appendChild(link) {links.push(link)}}
};
vm.runInNewContext(input.canonical, context);
result.canonical = links;
process.stdout.write(JSON.stringify(result));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        input=json.dumps(
            {"script": script, "map": manifest, "tag": rendered.tag, "canonical": canonical[0]}
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "base": "",
        "tag": rendered.tag,
        "page": "user-guide/frozen.html",
        "latest": "/user-guide/frozen.html",
        "historical": "/v/1.5.0/user-guide/frozen.html",
        "absent": "/v/1.5.0/index.html",
        "canonical": [
            {"rel": "canonical", "href": "https://layout.example.test/user-guide/frozen.html"}
        ],
    }
    for alias in ("latest", "stable"):
        redirect = (site / "v" / alias / "index.html").read_text()
        assert '<meta http-equiv="refresh" content="0; url=/">' in redirect
        assert '<link rel="canonical" href="/">' in redirect
        assert Page(site / "v" / alias / "index.html").links == ["/"]
