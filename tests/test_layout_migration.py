import io
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner
from yaml12 import read_yaml

from great_docs._layout import Layout
from great_docs._layout_migration import analyse
from great_docs._layout_migration.model import Move, fingerprint
from great_docs._utils import QUARTO_YML_HEADER
from great_docs.cli import cli


class TerminalInput(io.BytesIO):
    """Confirmation input from an interactive terminal"""

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(
    "flags, reply, applied",
    [
        ([], b"y\n", True),
        ([], b"n\n", False),
        (["--yes"], None, True),
        (["--dry-run", "--yes"], None, False),
    ],
)
def test_migration_command_confirmation(
    project: Path, flags: list[str], reply: bytes | None, applied: bool
) -> None:
    before = snapshot(project)
    result = CliRunner().invoke(
        cli,
        ["migrate-layout", "--project-path", str(project), *flags],
        input=TerminalInput(reply) if reply else None,
    )
    assert result.exit_code == 0, result.output
    assert (project / "docs/great-docs.yml").is_file() == applied
    if not applied:
        assert snapshot(project) == before


def test_migration_command_requires_interactive_confirmation(project: Path) -> None:
    before = snapshot(project)
    result = CliRunner().invoke(
        cli, ["migrate-layout", "--project-path", str(project)], input="y\n"
    )
    assert result.exit_code != 0
    assert "--yes" in result.output
    assert snapshot(project) == before


@pytest.mark.parametrize("destination", ["docs", "website", "website pages"])
def test_migration_command_repeat_is_read_only(
    project: Path, destination: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project)
    args = ["migrate-layout", "--project-path", str(project), "--to", destination]
    result = CliRunner().invoke(cli, [*args, "--yes"])
    assert result.exit_code == 0, result.output
    assert (project / destination / "great-docs.yml").is_file()
    assert str(Path(destination) / "_site") in result.output
    if destination != "docs":
        import shlex

        commands = [
            line.strip()
            for line in result.output.splitlines()
            if line.strip().startswith("great-docs build")
        ]
        assert ["great-docs", "build", "--config", str(Path(destination) / "great-docs.yml")] in [
            shlex.split(command) for command in commands
        ]
    before = snapshot(project)
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "no migration is needed" in result.output.lower()
    assert snapshot(project) == before


def test_migration_command_reports_conflicts_even_with_yes(project: Path) -> None:
    put(project, "docs/great-docs.yml", "display_name: Occupied\n")
    before = snapshot(project)
    result = CliRunner().invoke(
        cli,
        [
            "migrate-layout",
            "--project-path",
            str(project),
            "--config",
            str(project / "great-docs.yml"),
            "--yes",
        ],
    )
    assert result.exit_code != 0
    assert "already exists" in result.output.lower()
    assert snapshot(project) == before


@pytest.mark.parametrize("failure", ["absent", "unreadable", "symlink", "escape"])
def test_migration_custom_fallback_is_validated(
    project: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    (project / "great-docs.yml").unlink()
    destination = "website"
    if failure == "unreadable":
        selected = put(project, "website/great-docs.yml", "module: sample\n")
        read_bytes = Path.read_bytes

        def unreadable(path: Path) -> bytes:
            if path == selected:
                raise PermissionError("Configuration is unreadable")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
    elif failure == "symlink":
        put(project, "actual/great-docs.yml", "module: sample\n")
        (project / "website").symlink_to(project / "actual", target_is_directory=True)
    elif failure == "escape":
        destination = "../outside"
    before = {str(path.relative_to(project)) for path in project.rglob("*")}
    result = CliRunner().invoke(
        cli, ["migrate-layout", "--project-path", str(project), "--to", destination, "--yes"]
    )
    assert result.exit_code != 0, result.output
    assert {str(path.relative_to(project)) for path in project.rglob("*")} == before


def test_explicit_migrated_config_is_noop_unless_relocation_requested(project: Path) -> None:
    (project / "great-docs.yml").unlink()
    selected = put(project, "website/settings.yml", "module: sample\n")
    args = ["migrate-layout", "--project-path", str(project), "--config", str(selected)]
    before = snapshot(project)
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert "no migration is needed" in result.output.lower()
    result = CliRunner().invoke(cli, [*args, "--to", "docs", "--yes"])
    assert result.exit_code != 0
    assert "unsupported" in result.output.lower()
    assert snapshot(project) == before


def test_migration_uses_one_proposal_for_preview_and_application(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from great_docs import _layout_migration as migration_api
    from great_docs._layout_migration import Migration

    analysed: list[Migration] = []
    analyse_original, apply_original = migration_api.analyse, migration_api.apply

    def analyse_once(layout: Layout, target: Path) -> Migration:
        proposal = analyse_original(layout, target)
        analysed.append(proposal)
        return proposal

    def apply_reviewed(proposal: Migration) -> None:
        assert len(analysed) == 1
        assert proposal is analysed[0]
        apply_original(proposal)

    monkeypatch.setattr(migration_api, "analyse", analyse_once)
    monkeypatch.setattr(migration_api, "apply", apply_reviewed)
    result = CliRunner().invoke(cli, ["migrate-layout", "--project-path", str(project), "--yes"])
    assert result.exit_code == 0, result.output
    assert len(analysed) == 1
    assert (project / "docs/great-docs.yml").is_file()


def test_migration_rejects_changes_made_during_confirmation(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def confirm(*args: object, **kwargs: object) -> bool:
        (project / "great-docs.yml").write_text("display_name: Later user edit\n")
        return True

    monkeypatch.setattr("click.confirm", confirm)
    result = CliRunner().invoke(
        cli, ["migrate-layout", "--project-path", str(project)], input=TerminalInput(b"y\n")
    )
    assert result.exit_code != 0
    assert "fresh preview" in result.output
    assert (project / "great-docs.yml").read_text() == "display_name: Later user edit\n"
    assert not (project / "docs").exists()


def test_migration_command_preserves_noncanonical_filename_selection(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shlex

    monkeypatch.chdir(project)
    selected = project / "settings file.yml"
    (project / "great-docs.yml").rename(selected)
    result = CliRunner().invoke(
        cli, ["migrate-layout", "--project-path", str(project), "--config", str(selected), "--yes"]
    )
    assert result.exit_code == 0, result.output
    command = next(
        line.strip()
        for line in result.output.splitlines()
        if line.strip().startswith("great-docs build")
    )
    assert shlex.split(command) == ["great-docs", "build", "--config", "docs/settings file.yml"]
    before = snapshot(project)
    result = CliRunner().invoke(
        cli,
        [
            "migrate-layout",
            "--project-path",
            str(project),
            "--config",
            str(project / "docs/settings file.yml"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "no migration is needed" in result.output.lower()
    assert snapshot(project) == before


@pytest.mark.parametrize("working_directory", ["root", "nested", "outside"])
@pytest.mark.parametrize("destination", ["docs", "website pages"])
def test_migration_suggested_commands_select_project_from_invocation_directory(
    project: Path, monkeypatch: pytest.MonkeyPatch, working_directory: str, destination: str
) -> None:
    import shlex

    cwd = {"root": project, "nested": project / "sample", "outside": project.parent}[
        working_directory
    ]
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(
        cli, ["migrate-layout", "--project-path", str(project), "--to", destination, "--yes"]
    )
    assert result.exit_code == 0, result.output
    selection = [] if working_directory == "root" else ["--project-path", str(project)]
    if destination != "docs":
        selected = project / destination / "great-docs.yml"
        selection += [
            "--config",
            str(selected.relative_to(project) if working_directory == "root" else selected),
        ]
    commands = [
        shlex.split(line.strip())
        for line in result.output.splitlines()
        if line.strip().startswith("great-docs ")
    ]
    assert ["great-docs", "build", *selection] in commands
    assert ["great-docs", "preview", *selection] in commands


def test_migration_help_is_directly_available_but_hidden_from_public_reference(
    project: Path,
) -> None:
    from great_docs import GreatDocs

    result = CliRunner().invoke(cli, ["migrate-layout", "--help"])
    assert result.exit_code == 0, result.output
    assert "--config" in result.output and "--dry-run" in result.output
    result = CliRunner().invoke(cli, ["--help"])
    assert "migrate-layout" not in result.output
    info = GreatDocs(str(project))._extract_click_command(cli, "great-docs")
    assert all(command["name"] != "migrate-layout" for command in info["commands"])


@pytest.mark.parametrize("selected", ["missing", "ambiguous", "migrated"])
def test_pending_recovery_precedes_config_selection(
    project: Path, monkeypatch: pytest.MonkeyPatch, selected: str
) -> None:
    import importlib

    from great_docs._layout_migration import apply

    application = importlib.import_module("great_docs._layout_migration.apply")
    move = application._move

    def interrupt(source: Path, destination: Path) -> None:
        move(source, destination)
        if source == project / "great-docs.yml":
            raise KeyboardInterrupt

    monkeypatch.setattr(application, "_move", interrupt)
    with pytest.raises(KeyboardInterrupt):
        apply(analyse(Layout.make(project), Path("docs")))
    args = ["migrate-layout", "--project-path", str(project), "--yes"]
    if selected == "missing":
        args += ["--config", str(project / "great-docs.yml")]
    elif selected == "ambiguous":
        put(project, "great-docs.yml", "module: sample\n")
    before = snapshot(project)
    result = CliRunner().invoke(cli, args)
    assert result.exit_code != 0
    assert "recovery" in result.output.lower()
    assert "manifest.json" in result.output
    assert "no completion record" in result.output
    assert snapshot(project) == before


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


@pytest.fixture
def isolated_git(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(project / "git-config"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(project / "xdg"))
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    return project


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("cache", ["moved", "empty", "recovered"])
@pytest.mark.parametrize("rules", ["/_freeze/\n", "/docs/_freeze/\n"])
def test_migration_blocks_changed_freeze_ignore_policy(
    project: Path, cache: str, rules: str
) -> None:
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    put(project, ".gitignore", rules)
    if cache == "empty":
        (project / "_freeze").mkdir()
    elif cache == "moved":
        put(project, "_freeze/result.json", b"cache\x00")
    else:
        put(project, "great-docs/_quarto.yml", QUARTO_YML_HEADER)
        put(project, "great-docs/_freeze/result.json", b"cache\x00")
    before = snapshot(project)

    result = analyse(Layout.make(project), Path("docs"))

    assert any("ignore policy" in message for message in result.blockers)
    assert snapshot(project) == before


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("rules", ["", "**/_freeze/*\n!**/_freeze/kept.json\n"])
def test_migration_preserves_matching_freeze_ignore_policy(project: Path, rules: str) -> None:
    from great_docs._layout_migration import apply

    subprocess.run(["git", "init", "-q", str(project)], check=True)
    put(project, ".gitignore", rules)
    put(project, "_freeze/result.json", b"cache\x00")
    put(project, "_freeze/kept.json", b"tracked cache\x00")
    subprocess.run(["git", "-C", str(project), "add", "-f", "_freeze/kept.json"], check=True)

    result = analyse(Layout.make(project), Path("docs"))

    assert not result.blockers
    apply(result)
    assert (project / "docs/_freeze/result.json").read_bytes() == b"cache\x00"
    assert (project / "docs/_freeze/kept.json").read_bytes() == b"tracked cache\x00"
    for name in ("result.json", "kept.json"):
        statuses = [
            subprocess.run(
                ["git", "-C", str(project), "check-ignore", "--no-index", "-q", path],
                check=False,
            ).returncode
            for path in (f"_freeze/{name}", f"docs/_freeze/{name}")
        ]
        assert statuses[0] == statuses[1]


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("ignored", [False, True])
def test_migration_preserves_forced_cache_tracking_or_refuses(project: Path, ignored: bool) -> None:
    from great_docs._layout_migration import apply

    rules = "_freeze/\n" if ignored else "/_freeze/\n/docs/_freeze/*\n!/docs/_freeze/kept.json\n"
    put(project, ".gitignore", rules)
    put(project, "_freeze/kept.json", b"tracked cache\x00")
    subprocess.run(["git", "-C", str(project), "add", "-f", "_freeze/kept.json"], check=True)
    proposal = analyse(Layout.make(project), Path("docs"))
    if not ignored:
        assert not proposal.blockers
    if proposal.blockers:
        assert any("tracked cache" in message for message in proposal.blockers)
        assert (project / "_freeze/kept.json").read_bytes() == b"tracked cache\x00"
        assert not (project / "docs/_freeze/kept.json").exists()
        return

    apply(proposal)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
    tracked = subprocess.run(
        ["git", "-C", str(project), "ls-files", "docs/_freeze/kept.json"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert tracked.strip() == "docs/_freeze/kept.json"


@pytest.mark.usefixtures("isolated_git")
def test_migration_revalidates_cache_tracking_index(project: Path) -> None:
    from great_docs._layout_migration import MigrationError, apply

    put(project, "_freeze/result.json", b"cache\x00")
    proposal = analyse(Layout.make(project), Path("docs"))
    assert not proposal.blockers
    subprocess.run(["git", "-C", str(project), "add", "_freeze/result.json"], check=True)
    before = snapshot(project)

    with pytest.raises(MigrationError):
        apply(proposal)

    assert snapshot(project) == before


@pytest.mark.usefixtures("isolated_git")
def test_migration_revalidates_shared_cache_tracking_index(project: Path) -> None:
    from great_docs._layout_migration import MigrationError, apply

    put(project, "_freeze/result.json", b"cache\x00")
    subprocess.run(["git", "-C", str(project), "add", "_freeze/result.json"], check=True)
    subprocess.run(["git", "-C", str(project), "update-index", "--split-index"], check=True)
    shared = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "--shared-index-path"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    shared_path = project / shared
    proposal = analyse(Layout.make(project), Path("docs"))
    assert not proposal.blockers
    assert shared_path in dict(proposal.fingerprints)
    shared_path.write_bytes(shared_path.read_bytes() + b"changed")
    before = snapshot(project)

    with pytest.raises(MigrationError):
        apply(proposal)

    assert snapshot(project) == before


@pytest.mark.usefixtures("isolated_git")
def test_migration_revalidates_freeze_ignore_inputs(project: Path) -> None:
    from great_docs._layout_migration import MigrationError, apply

    subprocess.run(["git", "init", "-q", str(project)], check=True)
    put(project, "_freeze/result.json", b"cache\x00")
    proposal = analyse(Layout.make(project), Path("docs"))
    assert not proposal.blockers
    put(project, ".git/info/exclude", "/docs/_freeze/\n")
    before = snapshot(project)

    with pytest.raises(MigrationError):
        apply(proposal)

    assert snapshot(project) == before


@pytest.mark.usefixtures("isolated_git")
@pytest.mark.parametrize("change", ["contents", "retarget", "remove", "ancestor"])
def test_migration_revalidates_linked_external_ignore_policy(
    project: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from great_docs._layout_migration import MigrationError, apply

    external = tmp_path_factory.mktemp("external-ignore")
    policy = put(external, "policies/excludes", "")
    link = external / "linked"
    link.symlink_to(policy.parent, target_is_directory=True)
    config = put(external, "config", f"[core]\nexcludesFile = {link / 'excludes'}\n")
    config_link = external / "gitconfig"
    config_link.symlink_to(config)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config_link))
    put(project, "_freeze/result.json", b"cache\x00")
    proposal = analyse(Layout.make(project), Path("docs"))
    assert not proposal.blockers
    if change == "contents":
        policy.write_text("/docs/_freeze/\n")
    elif change == "remove":
        config_link.unlink()
    elif change == "retarget":
        config_link.unlink()
        config_link.symlink_to(put(external, "other-config", config.read_bytes()))
    else:
        link.unlink()
        alternative = external / "alternative"
        alternative.mkdir()
        put(alternative, "excludes", "")
        link.symlink_to(alternative, target_is_directory=True)
    before = snapshot(project)

    with pytest.raises(MigrationError):
        apply(proposal)

    assert snapshot(project) == before


def test_migration_freeze_policy_with_host_git_configuration(project: Path) -> None:
    from great_docs._layout_migration import apply

    subprocess.run(["git", "init", "-q", str(project)], check=True)
    put(project, "_freeze/result.json", b"cache\x00")
    proposal = analyse(Layout.make(project), Path("docs"))
    assert not proposal.blockers

    apply(proposal)

    assert (project / "docs/_freeze/result.json").read_bytes() == b"cache\x00"


@pytest.mark.usefixtures("isolated_git")
def test_migration_revalidates_missing_included_git_config(
    project: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from great_docs._layout_migration import MigrationError, apply

    external = tmp_path_factory.mktemp("included-ignore")
    config = put(external, "config", "[include]\npath = extra-config\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    put(project, "_freeze/result.json", b"cache\x00")
    proposal = analyse(Layout.make(project), Path("docs"))
    assert not proposal.blockers
    put(external, "extra-config", "[core]\nexcludesFile = changed-rules\n")
    before = snapshot(project)

    with pytest.raises(MigrationError):
        apply(proposal)

    assert snapshot(project) == before


@pytest.mark.usefixtures("isolated_git")
def test_migration_blocks_conditional_git_ignore_configuration(project: Path) -> None:
    put(project, "git-config", '[includeIf "onbranch:future"]\npath = extra-config\n')
    put(project, "_freeze/result.json", b"cache\x00")

    proposal = analyse(Layout.make(project), Path("docs"))

    assert any("conditional" in message for message in proposal.blockers)


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


def test_bare_numeric_prefix_reference_is_not_a_broken_link(project: Path) -> None:
    put(project, "user_guide/00-introduction.qmd", "[Install](installation.qmd)")
    put(project, "user_guide/01-installation.qmd", "# Installation")
    result = analyse(Layout.make(project), Path("docs"))
    assert not result.blockers
