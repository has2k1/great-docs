"""
Great Docs Gauntlet (GDG) — package directory generator.

Creates real directory structures on disk from declarative spec dicts.
Each spec defines the files, metadata, and expected outcomes for a
minimal GDG test package.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path
from typing import Any

from yaml12 import format_yaml


def generate_package(
    spec: dict[str, Any],
    target_dir: Path,
    config_override: Path | str | None = None,
) -> Path:
    """
    Create a synthetic package directory from a spec.

    Parameters
    ----------
    spec
        Package specification dict. Must have at least ``"name"`` and ``"files"``.
    target_dir
        Parent directory in which to create the package folder.
    config_override
        Optional path to a YAML file whose contents replace
        `docs/great-docs.yml` in the generated package.

    Returns
    -------
    Path
        Absolute path to the created package directory.
    """
    pkg_name: str = spec["name"]
    pkg_dir = target_dir / pkg_name
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # --- pyproject.toml --------------------------------------------------
    if "pyproject_toml" in spec:
        _write_pyproject_toml(pkg_dir, spec["pyproject_toml"])

    # --- setup.cfg (for packages that use setup.cfg only) ----------------
    if "setup_cfg" in spec:
        (pkg_dir / "setup.cfg").write_text(spec["setup_cfg"], encoding="utf-8")

    # --- setup.py (for legacy packages) ----------------------------------
    if "setup_py" in spec:
        (pkg_dir / "setup.py").write_text(spec["setup_py"], encoding="utf-8")

    # --- Arbitrary files -------------------------------------------------
    files: dict[str, str] = spec.get("files", {})
    for rel_path, content in files.items():
        file_path = pkg_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Dedent the content to allow indented multi-line strings in specs
        file_path.write_text(textwrap.dedent(content), encoding="utf-8")

    # --- Binary files ----------------------------------------------------
    binary_files: dict[str, bytes] = spec.get("binary_files", {})
    for rel_path, blob in binary_files.items():
        file_path = pkg_dir / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(blob)

    # --- Relocate documentation content under docs/ ----------------------
    # A docs-layout project keeps its documentation content under `docs/`,
    # where `great_docs.core` resolves it. Specs define `"files"` keys
    # relative to the package root, so move the content after writing it.
    for entry in _documentation_entries(spec):
        _relocate_under_docs(pkg_dir, entry)

    # --- docs/great-docs.yml (config) ------------------------------------
    config_path = pkg_dir / "docs" / "great-docs.yml"
    if "config" in spec:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _write_yaml(config_path, spec["config"])

    # --- Config override (takes precedence) ------------------------------
    if config_override is not None:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        override_path = Path(config_override)
        if override_path.exists():
            config_path.write_text(override_path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            # Treat as raw YAML string
            config_path.write_text(str(config_override), encoding="utf-8")

    return pkg_dir


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Names `great_docs.core` looks for under `source_dir` without being told to:
# the two spellings of the default user guide, the asset and notebook folders,
# and the homepage override that outranks `README.md`.
_CONVENTIONAL_ENTRIES = (
    "user_guide",
    "user-guide",
    "assets",
    "notebooks",
    "index.qmd",
    "index.md",
)


def _documentation_entries(spec: dict[str, Any]) -> list[str]:
    """
    List the package-root entries that belong under ``docs/``

    Parameters
    ----------
    spec
        Package specification dict.

    Returns
    -------
    list[str]
        Single path segments, without duplicates, covering both the
        conventional names and the first segment of every configured
        source-relative path.
    """
    entries = list(_CONVENTIONAL_ENTRIES)
    for value in _source_relative_paths(spec.get("config") or {}):
        segment = _root_segment(value)
        if segment is not None and segment not in entries:
            entries.append(segment)
    return entries


def _relocate_under_docs(pkg_dir: Path, entry: str) -> None:
    """
    Move one package-root file or directory into ``docs/``

    Does nothing when the entry is absent, or when ``docs/`` already holds
    something under that name, since a spec whose ``"files"`` keys are
    already ``docs/``-prefixed is correct as authored.

    Parameters
    ----------
    pkg_dir
        The generated package directory.
    entry
        A single path segment directly under `pkg_dir`.

    Returns
    -------
    None
    """
    source = pkg_dir / entry
    destination = pkg_dir / "docs" / entry
    if not source.exists() or destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def _root_segment(value: str) -> str | None:
    """
    Name the package-root entry a source-relative configuration value reaches

    Parameters
    ----------
    value
        A configuration value resolved as ``source_dir / value``.

    Returns
    -------
    str | None
        The value's first path segment, or `None` for a value that names
        nothing relocatable: an absolute path, one that climbs out of
        ``docs/``, or one already written relative to it.
    """
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute() or not path.parts:
        return None
    first = path.parts[0]
    return None if first in (".", "..", "docs") else first


def _source_relative_paths(config: dict[str, Any]) -> list[str]:
    """
    Collect the configuration values that great-docs resolves against ``source_dir``

    Mirrors the resolution sites in `great_docs.core`; a value listed here but
    absent from the generated package is simply skipped when relocating.

    Parameters
    ----------
    config
        A spec's ``"config"`` dict, as written to ``docs/great-docs.yml``.

    Returns
    -------
    list[str]
        Every path-valued entry, flattened out of the lists and nested
        mappings the configuration schema allows.
    """
    values: list[str] = [
        *_as_paths(config.get("bibliography")),
        *_as_paths(config.get("csl")),
        *_as_paths(config.get("pre_render")),
    ]
    for group, key in (("freeze", "pre_render"), ("site", "css"), ("favicon", "icon")):
        section = config.get(group)
        if isinstance(section, dict):
            values += _as_paths(section.get(key))

    # `user_guide` names a directory only in its string form; a list is an
    # explicit section ordering and `True` merely enables the default.
    user_guide = config.get("user_guide")
    if isinstance(user_guide, str):
        values.append(user_guide)

    for section in config.get("sections") or []:
        if isinstance(section, dict):
            values += _as_paths(section.get("dir"))

    for entry in _as_entries(config.get("custom_pages")):
        values += _as_paths(entry if isinstance(entry, str) else entry.get("dir"))

    for entry in _as_entries(config.get("include_in_header")):
        if isinstance(entry, dict):
            values += _as_paths(entry.get("file"))

    logo = config.get("logo")
    if isinstance(logo, dict):
        values += _as_paths(logo.get("light")) + _as_paths(logo.get("dark"))

    skill = config.get("skill")
    if isinstance(skill, dict):
        values += _as_paths(skill.get("file")) + _as_paths(skill.get("extra_body"))
        for entry in _as_entries(skill.get("skills")):
            if isinstance(entry, dict):
                values += _as_paths(entry.get("file"))

    return values


def _as_paths(value: Any) -> list[str]:
    """
    Normalise a path-valued configuration entry to a list of strings

    Parameters
    ----------
    value
        A string, a list of strings, or anything else the schema permits.

    Returns
    -------
    list[str]
        The strings the value contributes, empty for any other type.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def _as_entries(value: Any) -> list[Any]:
    """
    Normalise a configuration key that accepts one entry or several

    Parameters
    ----------
    value
        A single entry, a list of entries, or `None`/`False` for no entries.

    Returns
    -------
    list
        The entries to inspect.
    """
    if value is None or value is False:
        return []
    return value if isinstance(value, list) else [value]


def _write_pyproject_toml(pkg_dir: Path, data: dict[str, Any]) -> None:
    """Produce a minimal pyproject.toml from a nested dict."""
    lines: list[str] = []
    _toml_section(lines, data, prefix="")
    (pkg_dir / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _toml_section(lines: list[str], data: dict[str, Any], prefix: str) -> None:
    """Recursively serialize a dict into TOML-ish text.

    Good enough for the simple structures we need (no arrays-of-tables, etc.).
    """
    scalars: dict[str, Any] = {}
    tables: dict[str, Any] = {}

    for k, v in data.items():
        if isinstance(v, dict):
            tables[k] = v
        else:
            scalars[k] = v

    # Emit scalars under current header
    if scalars:
        if prefix:
            lines.append(f"[{prefix}]")
        for k, v in scalars.items():
            # Keys with special characters or empty keys must be quoted
            key_str = f'"{k}"' if (not k or "-" in k or " " in k) else k
            lines.append(f"{key_str} = {_toml_value(v)}")
        lines.append("")

    # Recurse into sub-tables
    for k, v in tables.items():
        # Quote table key segments that contain special characters
        quoted_k = f'"{k}"' if ("-" in k or " " in k) else k
        sub_prefix = f"{prefix}.{quoted_k}" if prefix else quoted_k
        _toml_section(lines, v, sub_prefix)


def _toml_value(v: Any) -> str:
    """Format a Python value as a TOML literal."""
    if isinstance(v, str):
        return f'"{v}"'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, dict):
        # Inline table: {key = "value", ...}
        parts = [f"{k} = {_toml_value(val)}" for k, val in v.items()]
        return "{" + ", ".join(parts) + "}"
    if isinstance(v, list):
        inner = ", ".join(_toml_value(i) for i in v)
        return f"[{inner}]"
    return f'"{v}"'


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write a dict as YAML."""
    path.write_text(
        format_yaml(data),
        encoding="utf-8",
    )
