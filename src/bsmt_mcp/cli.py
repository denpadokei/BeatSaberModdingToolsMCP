"""Command line interface for the Beat Saber modding tools."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__, assembly, beatsaber, buildtools, manifest, ops, templates
from .buildtools import BuildOptions
from .errors import BsmtError
from .fileops import replace_in_file
from .gitinfo import git_info
from .project import copy_game_references, load_project
from .util import find_project_file


def _print(data: Any) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("utf-8", "replace").decode("utf-8"))


def _exit_for(data: Any) -> int:
    if isinstance(data, dict) and data.get("ok") is False:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bsmt",
        description="Beat Saber modding tools for AI-driven builds (no Visual Studio required).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("detect", help="Detect Beat Saber installations")
    p.add_argument("--game", help="Validate a specific game folder")
    p.add_argument("paths", nargs="*", help="Additional candidate folders")

    sub.add_parser("doctor", help="Report MSBuild/dotnet/git/game setup")

    p = sub.add_parser("inspect", help="Inspect a mod project")
    p.add_argument("project")
    p.add_argument("-c", "--configuration", default="Debug")

    p = sub.add_parser("refs", help="Manage game DLL references")
    refs = p.add_subparsers(dest="refs_command", required=True)
    p_list = refs.add_parser("list", help="List game references")
    p_list.add_argument("project", nargs="?")
    p_list.add_argument("--game")
    p_list.add_argument("--filter")
    p_list.add_argument("--missing", action="store_true")
    p_list.add_argument("--limit", type=int, default=200)
    p_add = refs.add_parser("add", help="Add game references")
    p_add.add_argument("project")
    p_add.add_argument("--name", action="append", default=[])
    p_add.add_argument("--preset", action="append", default=[])
    p_add.add_argument("--game")
    p_add.add_argument("--hint-root", default="$(BeatSaberDir)")
    p_add.add_argument("--force", action="store_true")
    p_add.add_argument("--dry-run", action="store_true")
    p_remove = refs.add_parser("remove", help="Remove game references")
    p_remove.add_argument("project")
    p_remove.add_argument("--name", action="append", required=True)
    p_remove.add_argument("--keep-non-game", action="store_true")
    p_remove.add_argument("--dry-run", action="store_true")
    p_copy = refs.add_parser("copy", help="Copy game DLLs into a local Refs folder")
    p_copy.add_argument("project")
    p_copy.add_argument("--destination")
    p_copy.add_argument("--game")
    p_copy.add_argument("--force", action="store_true")
    p_copy.add_argument("--no-set-dir", action="store_true")

    p = sub.add_parser("setup", help="Write BeatSaberDir/GameDirectory")
    p.add_argument("project")
    p.add_argument("--game")
    p.add_argument("--target", choices=["auto", "user", "project", "props"], default="auto")
    p.add_argument("--reference-path", action="store_true")

    p = sub.add_parser("manifest", help="Read/generate/validate manifest.json")
    m = p.add_subparsers(dest="manifest_command", required=True)
    p_show = m.add_parser("show", help="Read and validate the manifest")
    p_show.add_argument("project")
    p_validate = m.add_parser("validate", help="Validate the manifest")
    p_validate.add_argument("project")
    p_gen = m.add_parser("generate", help="Generate/merge the manifest")
    p_gen.add_argument("project")
    p_gen.add_argument("--id")
    p_gen.add_argument("--name")
    p_gen.add_argument("--author")
    p_gen.add_argument("--version")
    p_gen.add_argument("--game-version")
    p_gen.add_argument("--description")
    p_gen.add_argument("--depends-on", action="append", default=[], metavar="ID=RANGE")
    p_gen.add_argument("--link", action="append", default=[], metavar="KEY=URL")
    p_gen.add_argument("--update", action="append", default=[], metavar="KEY=JSON")
    p_gen.add_argument("--target")
    p_gen.add_argument("--base")
    p_gen.add_argument("--no-embed", action="store_true")

    p = sub.add_parser("assembly", help="Read assembly version info")
    p.add_argument("project")
    p.add_argument("-c", "--configuration", default="Debug")

    p = sub.add_parser("set-version", help="Set manifest and AssemblyInfo versions together")
    p.add_argument("project")
    p.add_argument("version")
    p.add_argument("--no-manifest", action="store_true")
    p.add_argument("--no-assembly", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("compare", help="Compare manifest and assembly versions")
    p.add_argument("manifest_version")
    p.add_argument("assembly_version")

    p = sub.add_parser("git", help="Show git information")
    p.add_argument("project")
    p.add_argument("--hash-length", type=int, default=7)

    p = sub.add_parser("build", help="Build the project")
    p.add_argument("project")
    p.add_argument("-c", "--configuration", default="Debug")
    p.add_argument("--target", choices=["Build", "Rebuild", "Clean"], default="Build")
    p.add_argument("--no-restore", action="store_true")
    p.add_argument("--no-incremental", action="store_true")
    p.add_argument("--prefer", choices=["auto", "msbuild", "dotnet"], default="auto")
    p.add_argument("--timeout", type=float, default=900)
    p.add_argument("--property", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--package", action="store_true")
    p.add_argument("--deploy", action="store_true")
    p.add_argument("--game")

    p = sub.add_parser("package", help="Create a BeatMods zip from an existing build")
    p.add_argument("project")
    p.add_argument("-c", "--configuration", default="Debug")
    p.add_argument("--artifact-dir")
    p.add_argument("--zip-dir")
    p.add_argument("--extra", action="append", default=[], metavar="SOURCE=DEST")
    p.add_argument("--copy-pdb", action="store_true", default=True)
    p.add_argument("--no-pdb", dest="copy_pdb", action="store_false")
    p.add_argument("--include-manifest", action="store_true")

    p = sub.add_parser("deploy", help="Copy the build into the game")
    p.add_argument("project")
    p.add_argument("-c", "--configuration", default="Debug")
    p.add_argument("--game")

    p = sub.add_parser("create", help="Create a new BSIPA plugin project")
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--id")
    p.add_argument("--author")
    p.add_argument("--description", default="")
    p.add_argument("--version", default="0.1.0")
    p.add_argument("--game-version")
    p.add_argument("--game")
    p.add_argument("--framework", default="net472")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("replace", help="Replace text in a file")
    p.add_argument("file")
    p.add_argument("pattern")
    p.add_argument("substitute")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--multiline", action="store_true")
    p.add_argument("--singleline", action="store_true")
    p.add_argument("--escape-backslash", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("github-output", help="Append NAME=VALUE to GITHUB_OUTPUT")
    p.add_argument("name")
    p.add_argument("value")
    p.add_argument("--path")

    sub.add_parser("mcp", help="Run the MCP server on stdio")
    return parser


def _pairs(values: list[str], *, as_json: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator:
            raise BsmtError(f"Expected KEY=VALUE, got {item!r}")
        result[key.strip()] = json.loads(value) if as_json else value
    return result


def main(argv: Sequence[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    try:
        if args.command == "mcp":
            from .server import create_server

            create_server().run("stdio")
            return 0
        if args.command == "detect":
            if args.game:
                result = {
                    "ok": True,
                    "install": beatsaber.ensure_install(args.game).to_dict(),
                    "installs": [item.to_dict() for item in beatsaber.detect_installs(args.paths)],
                }
            else:
                installs = beatsaber.detect_installs(args.paths)
                result = {
                    "ok": True,
                    "installs": [item.to_dict() for item in installs],
                    "selection_reason": beatsaber.resolve_install(installs=installs)[2] if installs else None,
                }
        elif args.command == "doctor":
            result = buildtools.build_environment()
            result["ok"] = True
        elif args.command == "inspect":
            result = ops.inspect_project_data(args.project, configuration=args.configuration)
        elif args.command == "refs":
            if args.refs_command == "list":
                result = ops.list_references_data(
                    args.project,
                    game_dir=args.game,
                    filter_value=args.filter,
                    only_missing=args.missing,
                    limit=args.limit,
                )
            elif args.refs_command == "add":
                result = ops.add_references_data(
                    args.project,
                    names=args.name,
                    presets=args.preset,
                    game_dir=args.game,
                    hint_root=args.hint_root,
                    force=args.force,
                    dry_run=args.dry_run,
                )
            elif args.refs_command == "remove":
                info = load_project(args.project)
                from .project import remove_references

                result = remove_references(
                    info,
                    args.name,
                    only_game_references=not args.keep_non_game,
                    dry_run=args.dry_run,
                )
                result.update({"ok": True, "project": str(info.path)})
            else:
                info = load_project(args.project)
                if args.game:
                    install = beatsaber.ensure_install(args.game)
                    source = "explicit"
                else:
                    install, source, _ = beatsaber.resolve_install()
                target = Path(args.destination) if args.destination else info.directory.parent / "Refs"
                if not target.is_absolute():
                    target = info.directory / target
                result = copy_game_references(install.path, target, force=args.force)
                result.update({"ok": True, "project": str(info.path), "game_dir": str(install.path), "game_source": source})
                if not args.no_set_dir:
                    from .project import set_game_properties

                    result["project_changes"] = set_game_properties(info, game_dir=target, set_game_directory=False)
        elif args.command == "setup":
            result = ops.set_game_directory_data(
                args.project,
                game_dir=args.game,
                target=args.target,
                set_reference_path=args.reference_path,
            )
        elif args.command == "manifest":
            info = load_project(args.project)
            path = info.manifest_path()
            if args.manifest_command == "show" or args.manifest_command == "validate":
                if path is None:
                    raise BsmtError(f"No manifest.json found in {info.directory}")
                result = manifest.read_manifest_info(path)
                result["ok"] = True
            else:
                base = Path(args.base) if args.base else path
                if base and not base.is_absolute():
                    base = info.directory / base
                target = Path(args.target) if args.target else (path or info.directory / "manifest.json")
                if not target.is_absolute():
                    target = info.directory / target
                payload: dict[str, Any] = {}
                for key, value in (
                    ("id", args.id),
                    ("name", args.name),
                    ("author", args.author),
                    ("version", args.version),
                    ("gameVersion", args.game_version),
                    ("description", args.description),
                ):
                    if value is not None:
                        payload[key] = value
                if args.depends_on:
                    payload["dependsOn"] = _pairs(args.depends_on)
                if args.link:
                    payload["links"] = _pairs(args.link)
                payload.update(_pairs(args.update, as_json=True))
                generated = manifest.generate_manifest(
                    base_manifest=base if base and base.is_file() else None,
                    target_path=target,
                    updates=payload,
                )
                if not args.no_embed:
                    from .project import ensure_manifest_embedded

                    try:
                        relative = target.resolve().relative_to(info.directory.resolve()).as_posix()
                    except ValueError:
                        relative = "manifest.json"
                    generated["embed"] = ensure_manifest_embedded(load_project(info.path), manifest_rel=relative)
                result = {"ok": True, **generated}
        elif args.command == "assembly":
            info = load_project(args.project)
            result = {
                "ok": True,
                "assembly": assembly.read_assembly_info(info),
                "comparison": assembly.compare_project_versions(info),
            }
        elif args.command == "set-version":
            info = load_project(args.project)
            result = assembly.set_project_version(
                info,
                args.version,
                update_manifest=not args.no_manifest,
                update_assembly=not args.no_assembly,
                dry_run=args.dry_run,
            )
        elif args.command == "compare":
            result = assembly.compare_versions(args.manifest_version, args.assembly_version)
            result["ok"] = True
        elif args.command == "git":
            info = find_project_file(args.project)
            result = git_info(info.parent, hash_length=args.hash_length)
            result["ok"] = True
        elif args.command == "build":
            options = BuildOptions(
                configuration=args.configuration,
                target=args.target,
                restore=not args.no_restore,
                no_incremental=args.no_incremental,
                prefer=args.prefer,
                timeout=args.timeout,
                extra_properties=_pairs(args.property),
            )
            build = buildtools.build_mod(args.project, options=options)
            result = {"ok": build["ok"], "build": build, "package": None, "deploy": None}
            if build["ok"] and args.package:
                result["package"] = buildtools.package_mod(args.project, configuration=args.configuration)
            if build["ok"] and args.deploy:
                result["deploy"] = buildtools.deploy_mod(
                    args.project, configuration=args.configuration, game_dir=args.game
                )
        elif args.command == "package":
            result = buildtools.package_mod(
                args.project,
                configuration=args.configuration,
                artifact_dir=args.artifact_dir,
                zip_dir=args.zip_dir,
                extra_files=_extra_files(args.extra),
                copy_pdb=args.copy_pdb,
                include_manifest=args.include_manifest,
            )
        elif args.command == "deploy":
            result = buildtools.deploy_mod(args.project, configuration=args.configuration, game_dir=args.game)
        elif args.command == "create":
            result = templates.create_project(
                args.path,
                name=args.name,
                mod_id=args.id,
                author=args.author,
                description=args.description,
                version=args.version,
                game_version=args.game_version,
                game_dir=args.game,
                target_framework=args.framework,
                force=args.force,
            )
        elif args.command == "replace":
            result = replace_in_file(
                args.file,
                args.pattern,
                args.substitute,
                use_regex=args.regex,
                multiline=args.multiline,
                singleline=args.singleline,
                escape_backslash=args.escape_backslash,
                dry_run=args.dry_run,
            )
        elif args.command == "github-output":
            from .fileops import set_action_output

            result = set_action_output(args.name, args.value, args.path)
        else:  # pragma: no cover - argparse enforces commands
            raise BsmtError(f"Unknown command: {args.command}")
    except BsmtError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print(result)
    return _exit_for(result)


def _extra_files(values: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in values:
        source, separator, destination = item.partition("=")
        if not separator:
            raise BsmtError(f"Expected SOURCE=DEST, got {item!r}")
        result.append({"source": source, "destination": destination})
    return result


if __name__ == "__main__":
    raise SystemExit(main())
