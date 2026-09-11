"""Inspect migration inputs and produce operations without writing files"""

from __future__ import annotations

import configparser
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from great_docs._layout import Layout
from great_docs._utils import is_great_docs_build_dir, recognised_build_dirs

from .content import (
    ConfigPath,
    config_paths,
    local_path,
    read_config,
    rewrite_config,
    rewrite_document,
    set_config_values,
)
from .model import (
    Edit,
    Migration,
    MigrationError,
    Move,
    absolute_path,
    check_symlinks,
    fingerprint,
    moved_path,
    tree_files,
)

_DOCUMENT_SUFFIXES = {".md", ".qmd", ".html", ".htm"}
_MANIFESTS = ("pyproject.toml", "setup.py", "setup.cfg", "go.mod", "Cargo.toml")
_RESERVED = {
    ".git",
    ".venv",
    ".great-docs-cache",
    "great-docs",
    "_quarto",
    "_site",
    "_freeze",
    "assets",
}


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _package_metadata(root: Path) -> tuple[str, list[Path]]:
    name = ""
    sources: list[Path] = [root / "src"]
    manifest = root / "pyproject.toml"
    if manifest.is_file():
        data = tomllib.loads(manifest.read_bytes().decode("utf-8"))
        name = data.get("project", {}).get("name", "")
        setuptools = data.get("tool", {}).get("setuptools", {})
        for directory in setuptools.get("package-dir", {}).values():
            if isinstance(directory, str) and directory not in {"", "."}:
                sources.append(absolute_path(root / directory))
        packages = setuptools.get("packages", {})
        if isinstance(packages, dict):
            for directory in packages.get("find", {}).get("where", []):
                if isinstance(directory, str) and directory not in {"", "."}:
                    sources.append(absolute_path(root / directory))
        elif isinstance(packages, list):
            sources.extend(
                root / package.split(".")[0] for package in packages if isinstance(package, str)
            )
        for package in data.get("tool", {}).get("poetry", {}).get("packages", []):
            if isinstance(package, dict) and isinstance(package.get("include"), str):
                sources.append(root / package.get("from", "") / package["include"])
    if not name and (root / "setup.cfg").is_file():
        config = configparser.ConfigParser()
        config.read_string((root / "setup.cfg").read_bytes().decode("utf-8"))
        name = config.get("metadata", "name", fallback="")
    if not name and (root / "setup.py").is_file():
        match = re.search(
            r"name\s*=\s*[\"']([^\"']+)[\"']", (root / "setup.py").read_bytes().decode("utf-8")
        )
        if match:
            name = match[1]
    packages = [
        child
        for child in sorted(root.iterdir())
        if not child.name.startswith(".") and (child / "__init__.py").is_file()
    ]
    sources.extend(packages)
    if not name and len(packages) == 1:
        name = packages[0].name
    if name:
        sources.append(root / name.replace("-", "_"))
    return name, sources


def _logo_candidates(package: str, *, hero: bool) -> list[str]:
    if hero:
        return [
            "logo-hero.svg",
            "logo-hero.png",
            "assets/logo-hero.svg",
            "assets/logo-hero.png",
            "logo-hero-light.svg",
            "logo-hero-light.png",
            "assets/logo-hero-light.svg",
            "assets/logo-hero-light.png",
        ]
    candidates = [
        "logo.svg",
        "logo.png",
        "assets/logo.svg",
        "assets/logo.png",
        "docs/assets/logo.svg",
        "docs/assets/logo.png",
    ]
    for name in dict.fromkeys((package, package.replace("-", "_"))):
        if name:
            candidates.extend(
                [
                    f"{name}_logo.svg",
                    f"{name}_logo.png",
                    f"assets/{name}_logo.svg",
                    f"assets/{name}_logo.png",
                ]
            )
    candidates.extend(["assets/logo-light.svg", "assets/logo-light.png"])
    return candidates


def _dedicated_directories(config: dict[str, Any], root: Path) -> list[Path]:
    selected: list[Path] = []
    guide = config.get("user_guide")
    if isinstance(guide, str) and not Path(guide).is_absolute():
        selected.append(root / guide)
    elif not isinstance(guide, str):
        for name in ("user_guide", "user-guide"):
            if (root / name).exists() or (root / name).is_symlink():
                selected.append(root / name)
                break
    sections = config.get("sections") or []
    if not isinstance(sections, list):
        raise MigrationError("Narrative sections must be a list of directory mappings")
    for section in sections:
        if (
            isinstance(section, dict)
            and isinstance(section.get("dir"), str)
            and not Path(section["dir"]).is_absolute()
        ):
            selected.append(root / section["dir"])
    custom = config.get("custom_pages")
    if custom is None:
        if (root / "custom").exists() or (root / "custom").is_symlink():
            selected.append(root / "custom")
    else:
        for entry in custom if isinstance(custom, list) else [custom]:
            directory = entry.get("dir") if isinstance(entry, dict) else entry
            if isinstance(directory, str) and not Path(directory).is_absolute():
                selected.append(root / directory)
    marimo = config.get("marimo", False)
    if marimo is True or isinstance(marimo, dict) and marimo.get("enabled"):
        if (root / "notebooks").exists() or (root / "notebooks").is_symlink():
            selected.append(root / "notebooks")
    for path in selected:
        check_symlinks(path)
    return [absolute_path(path) for path in selected]


def analyse(layout: Layout, destination: Path) -> Migration:
    """
    Preview a root-layout migration without modifying the filesystem

    Resolve a relative destination against the package root. Record moves,
    edits at original paths, retained input fingerprints, blocking conflicts,
    and file-specific manual follow-up. Preserve generated trees and cache
    bytes. Report an already migrated matching layout without operations.
    """
    root = layout.package_root
    supplied_destination = root / destination
    destination = absolute_path(root / destination)
    config_path = layout.config_path
    moves: list[Move] = []
    edits: list[Edit] = []
    fingerprints: dict[Path, str] = {}
    blockers: list[str] = []
    follow_up: list[str] = []

    def result() -> Migration:
        return Migration(
            root,
            config_path,
            tuple(moves),
            tuple(edits),
            tuple(sorted(fingerprints.items())),
            tuple(dict.fromkeys(blockers)),
            tuple(dict.fromkeys(follow_up)),
        )

    def retain(path: Path) -> bool:
        try:
            check_symlinks(path)
            if path.is_dir():
                tree_files(path)
            fingerprints[path] = fingerprint(path)
            return True
        except (OSError, MigrationError) as error:
            blockers.append(f"Cannot inspect {path}: {error}")
            return False

    if layout.source_dir != root:
        if destination == layout.source_dir:
            follow_up.append(
                f"Documentation already uses {layout.source_dir}; no migration is needed"
            )
        else:
            blockers.append(
                f"Relocating an already migrated project is unsupported: {layout.source_dir}"
            )
        return result()
    if destination == root or not destination.is_relative_to(root):
        blockers.append(f"The destination must be a descendant of the package root: {destination}")
        return result()
    try:
        check_symlinks(supplied_destination)
        check_symlinks(destination)
        for component in (destination, *destination.parents):
            if component.exists() and not component.is_dir():
                blockers.append(f"Destination component is not a directory: {component}")
    except MigrationError as error:
        blockers.append(str(error))
    if not retain(config_path):
        return result()
    try:
        before = config_path.read_bytes()
        text = before.decode("utf-8")
        config = read_config(text)
        rewrite_config(text, (), root, root)
    except (OSError, UnicodeError, MigrationError) as error:
        blockers.append(f"Cannot inspect configuration {config_path}: {error}")
        return result()

    for name in _MANIFESTS:
        retain(root / name)
    try:
        package, package_sources = _package_metadata(root)
    except (OSError, UnicodeError, ValueError, configparser.Error) as error:
        blockers.append(f"Cannot inspect package metadata: {error}")
        package, package_sources = "", [root / "src"]
    module = config.get("module")
    if isinstance(module, str):
        package_sources.append(root / module.split(".")[0])
    try:
        generated = recognised_build_dirs(layout)
        if is_great_docs_build_dir(layout.build_dir):
            generated.append(layout.build_dir)
    except OSError as error:
        blockers.append(f"Cannot inspect generated projects: {error}")
        generated = []
    protected = [*package_sources, *(root / name for name in _RESERVED), *generated]
    for path in protected:
        if _overlaps(destination, path):
            blockers.append(
                f"Destination overlaps package sources, shared assets, or generated output: {path}"
            )

    try:
        selected = _dedicated_directories(config, root)
    except (OSError, MigrationError) as error:
        blockers.append(str(error))
        selected = []
    for name in ("index.qmd", "index.md"):
        if (root / name).exists() or (root / name).is_symlink():
            selected.append(root / name)
            break
    selected.append(config_path)
    for index, source in enumerate(selected):
        if source == root or not source.is_relative_to(root):
            blockers.append(f"Documentation source must be a package descendant: {source}")
            continue
        if _overlaps(source, destination):
            blockers.append(f"Documentation source overlaps the destination: {source}")
        for other in selected[:index]:
            if _overlaps(source, other):
                blockers.append(f"Selected documentation sources overlap: {other} and {source}")
        for path in protected:
            if _overlaps(source, path):
                blockers.append(
                    f"Documentation source overlaps package sources, shared assets, or generated output: {source} and {path}"
                )
        target = destination / source.relative_to(root)
        if source == config_path:
            target = destination / config_path.name
        if source.exists() or source.is_symlink():
            moves.append(Move(source, target))
        else:
            blockers.append(f"Documentation source does not exist: {source}")

    for name in (
        "user_guide",
        "user-guide",
        "custom",
        "notebooks",
        "index.qmd",
        "index.md",
        "README.md",
        "README.rst",
    ):
        if (root / name).is_file() or not (root / name).exists():
            retain(root / name)
        target = destination / name
        if target.exists() and not any(move.destination == target for move in moves):
            blockers.append(f"Existing destination input would change source discovery: {target}")

    implicit: dict[ConfigPath, Any] = {}
    for path, hero in ((("logo",), False), (("hero", "logo"), True)):
        parent = config.get("hero") if hero else config
        existing = parent.get("logo") if isinstance(parent, dict) else None
        if hero and parent is False or existing is False:
            continue
        explicit = (
            isinstance(existing, str)
            or isinstance(existing, dict)
            and any(existing.get(key) for key in ("light", "dark"))
        )
        if explicit:
            continue
        for candidate in _logo_candidates(package, hero=hero):
            candidate_path = root / candidate
            if not retain(candidate_path):
                break
            if candidate_path.is_file():
                light = Path(candidate)
                dark = light.with_name(light.stem.replace("-light", "") + "-dark" + light.suffix)
                retain(root / dark)
                variants = {
                    "light": candidate,
                    "dark": dark.as_posix() if (root / dark).is_file() else candidate,
                }
                if isinstance(existing, dict):
                    implicit.update({(*path, key): value for key, value in variants.items()})
                else:
                    implicit[path] = variants
                break
    try:
        materialised = set_config_values(text, implicit)
        amended = read_config(materialised)
    except MigrationError as error:
        blockers.append(str(error))
        materialised, amended = text, config
    documents: set[Path] = set()
    for option, value in config_paths(amended):
        try:
            source = local_path(value, root)
            if source is None:
                continue
            check_symlinks(root / value)
            readable = retain(source)
            if (
                readable
                and source.is_dir()
                and option[0] in {"sections", "user_guide", "custom_pages"}
            ):
                documents.update(
                    path for path in tree_files(source) if path.suffix.lower() in _DOCUMENT_SUFFIXES
                )
            if not source.exists():
                blockers.append(
                    f"Configured input does not exist for {'.'.join(map(str, option))}: {source}"
                )
            if not source.is_relative_to(root):
                follow_up.append(
                    f"Retain external input for {'.'.join(map(str, option))}: {source}"
                )
            if Path(value).is_absolute() and moved_path(source, tuple(moves)) != source:
                blockers.append(
                    f"An unchanged absolute reference would point into a moved source: {source}"
                )
            if "pre_render" in option:
                follow_up.append(f"Review working-directory assumptions in render script {source}")
        except (OSError, ValueError) as error:
            blockers.append(f"Cannot inspect configured input {option}: {error}")
    try:
        rewritten = rewrite_config(materialised, tuple(moves), root, destination).encode("utf-8")
        if rewritten != before:
            edits.append(Edit(config_path, before, rewritten))
    except MigrationError as error:
        blockers.append(str(error))

    for move in moves:
        if not retain(move.source):
            continue
        try:
            for path in tree_files(move.source):
                if path == config_path:
                    continue
                if path.name == "__init__.py" or path.name in _MANIFESTS:
                    blockers.append(
                        f"Documentation directory contains package sources or metadata: {path}"
                    )
                if path.suffix.lower() in _DOCUMENT_SUFFIXES:
                    documents.add(path)
                elif path.suffix.lower() in {".ipynb", ".py", ".r", ".jl"}:
                    follow_up.append(
                        f"Review dynamic code, notebook references, and working-directory assumptions in {path}"
                    )
                elif path.suffix.lower() in {".rst", ".termshow"}:
                    follow_up.append(
                        f"Review unsupported document or companion-file references in {path}"
                    )
        except (OSError, MigrationError) as error:
            blockers.append(f"Cannot inspect {move.source}: {error}")
    for name in ("README.md", "README.rst", "index.qmd", "index.md"):
        path = root / name
        if path.is_file():
            retain(path)
            if path.suffix in _DOCUMENT_SUFFIXES:
                documents.add(path)
            elif moves:
                follow_up.append(
                    f"Review reStructuredText references to moved documentation in {path}"
                )
    for path in sorted(documents):
        try:
            content = path.read_bytes()
            rewritten_text, inputs, notes, conflicts = rewrite_document(
                content.decode("utf-8"), path, tuple(moves)
            )
            follow_up.extend(notes)
            blockers.extend(conflicts)
            for source in inputs:
                retain(source)
            after = rewritten_text.encode("utf-8")
            if content != after:
                if path.is_relative_to(root):
                    edits.append(Edit(path, content, after))
                else:
                    blockers.append(
                        f"A retained external document needs reference edits before migration: {path}"
                    )
        except (OSError, UnicodeError, ValueError) as error:
            blockers.append(f"Cannot inspect document {path}: {error}")

    assets = root / "assets"
    referenced = set(fingerprints)
    if assets.exists() and retain(assets):
        for path in tree_files(assets):
            if path not in referenced:
                blockers.append(
                    f"Cannot preserve unreferenced implicit asset publication automatically: {path}"
                )

    freeze = root / "_freeze"
    target_freeze = destination / "_freeze"
    retain(freeze)
    if target_freeze.exists() or target_freeze.is_symlink():
        blockers.append(f"Destination cache already exists: {target_freeze}")
    if freeze.exists() or freeze.is_symlink():
        if not freeze.is_dir():
            blockers.append(f"Persistent cache must be a directory: {freeze}")
        moves.append(Move(freeze, target_freeze))
    else:
        recovered: dict[Path, bytes] = {}
        for build in generated:
            cache = build / "_freeze"
            if not retain(cache) or not cache.exists():
                continue
            try:
                for path in tree_files(cache):
                    recovered[target_freeze / path.relative_to(cache)] = path.read_bytes()
            except (OSError, MigrationError) as error:
                blockers.append(f"Cannot recover cache from {cache}: {error}")
        for path, content in sorted(recovered.items()):
            if any(parent in recovered for parent in path.parents):
                blockers.append(f"Recovered cache files overlap a directory: {path}")
            edits.append(Edit(path, None, content))
    for build in generated:
        retain(build / "_quarto.yml")
        follow_up.append(
            f"Retain generated project {build}; the next build publishes to {destination / '_site'}"
        )
    ignore = root / ".gitignore"
    if retain(ignore):
        try:
            original = ignore.read_bytes() if ignore.exists() else None
            ignore_text = (original or b"").decode("utf-8")
            newline = "\r\n" if "\r\n" in ignore_text else "\n"
            prefix = destination.relative_to(root).as_posix()
            additions = [
                f"/{prefix}/{name}/"
                for name in ("_quarto", "_site")
                if f"/{prefix}/{name}/" not in ignore_text.splitlines()
            ]
            if additions:
                updated = (
                    ignore_text
                    + (newline if ignore_text and not ignore_text.endswith("\n") else "")
                    + newline.join(additions)
                    + newline
                )
                edits.append(Edit(ignore, original, updated.encode("utf-8")))
        except (OSError, UnicodeError) as error:
            blockers.append(f"Cannot inspect ignore rules {ignore}: {error}")

    automation = [root / "Makefile", root / "justfile", root / "tox.ini", root / "noxfile.py"]
    for directory in (root / ".github/workflows", root / "scripts"):
        if directory.exists() and retain(directory):
            automation.extend(tree_files(directory))
    for path in sorted(root.iterdir()):
        if path.suffix in {".sh", ".bash", ".zsh", ".ps1"}:
            automation.append(path)

    def report_walk_error(error: OSError) -> None:
        blockers.append(f"Cannot inspect implicit documentation inputs: {error}")

    for directory, children, names in os.walk(root, onerror=report_walk_error, followlinks=False):
        parent = Path(directory)
        children[:] = [
            name
            for name in children
            if not name.startswith(".")
            and name not in {"_quarto", "_site", "_freeze"}
            and parent / name not in generated
            and not (parent / name).is_symlink()
        ]
        for name in names:
            path = parent / name
            if path.suffix == ".termshow":
                retain(path)
                retain(path.with_suffix(".yml"))
                retain(path.with_suffix(".yaml"))
                follow_up.append(f"Review terminal recording and companion YAML paths in {path}")
    for path in dict.fromkeys(automation):
        if not path.is_file() or not retain(path):
            continue
        try:
            automation_text = path.read_bytes().decode("utf-8")
            if re.search(
                r"great-docs(?:-[\w.-]+)?[/\\]_site|great-docs(?:-[\w.-]+)?/", automation_text
            ):
                follow_up.append(
                    f"Update old output paths in {path}; publish {destination / '_site'}"
                )
        except (OSError, UnicodeError) as error:
            blockers.append(f"Cannot inspect automation {path}: {error}")
    skill = root / "skills" / package / "SKILL.md"
    if package and skill.is_file():
        retain(skill)
        follow_up.append(f"Review implicit skill discovery for retained input {skill}")
    sources = (
        config.get("interlinks", {}).get("sources", {})
        if isinstance(config.get("interlinks"), dict)
        else {}
    )
    if isinstance(sources, dict):
        for name, entry in sources.items():
            value = entry.get("url") if isinstance(entry, dict) else None
            if isinstance(value, str) and local_path(value, root) is not None:
                follow_up.append(
                    f"Review inventory location and published URL together for interlinks.sources.{name}.url: {value}"
                )
    site = config.get("site")
    if isinstance(site, dict):
        for name, value in site.items():
            if (
                name != "css"
                and isinstance(value, str)
                and re.search(r"[/\\]|\.(?:html|qmd|css|js|png|svg)$", value)
                and local_path(value, root) is not None
            ):
                follow_up.append(f"Review unsupported Quarto path option site.{name}: {value}")
    targets = [move.destination for move in moves]
    targets.extend(edit.path for edit in edits if edit.before is None)
    for target in targets:
        try:
            check_symlinks(target)
            if target.exists() or target.is_symlink():
                blockers.append(f"Destination already exists: {target}")
            for parent in target.parents:
                if parent.exists() and not parent.is_dir():
                    blockers.append(f"Destination component is not a directory: {parent}")
        except (OSError, MigrationError) as error:
            blockers.append(str(error))
    return result()
