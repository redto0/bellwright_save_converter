"""Command line interface."""

from __future__ import annotations

from pathlib import Path

from .oodle import Oodle, find_oodle
from .respawn import ORE_MESH_PATTERN, RESPAWN_TESTED_VERSION, respawn_nodes, write_respawned
from .saves import SaveSet, is_save_file, parse_header, scan_folder
from .steam import (
    DetectionError,
    detect_local_steam_id,
    find_candidate_sources,
    is_steam_id64,
    local_save_folder,
)
from .transfer import (
    TransferError,
    backups_root,
    execute_import,
    export_zip,
    game_is_running,
    list_snapshots,
    plan_import,
    resolve_source,
    snapshot,
)


def _format_sets(sets: list[SaveSet]) -> str:
    """Render save sets as an aligned table."""
    if not sets:
        return "  (no save files found)"

    rows = []
    for item in sets:
        when = item.saved.strftime("%Y-%m-%d %H:%M") if item.saved else "unknown"
        rows.append(
            (
                item.name,
                str(len(item.files)),
                f"{item.total_bytes / 1e6:.1f} MB",
                item.label,
                item.map_name,
                when,
            )
        )

    headers = ("SAVE", "FILES", "SIZE", "LABEL", "MAP", "LAST SAVED")
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    line = "  " + "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [line, "  " + "  ".join("-" * w for w in widths)]
    out += ["  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)) for row in rows]

    warnings = [s.name for s in sets if not s.name_matches_header]
    if warnings:
        out.append(f"\n  note: filename and in-file character differ for {', '.join(warnings)}")
    return "\n".join(out)


def _require_game_closed(force: bool) -> None:
    if game_is_running() and not force:
        raise TransferError(
            "Bellwright is running. Close the game first so no save is mid-write "
            "(or pass --force if you are sure)."
        )


def _resolve_default_source(explicit: str | None) -> Path:
    """Use --from if given, else a single obvious USB/drive folder."""
    if explicit:
        return Path(explicit)
    candidates = find_candidate_sources()
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise TransferError("No source given. Use --from <folder or .zip>.")
    listing = ", ".join(str(c) for c in candidates)
    raise TransferError(f"Several possible sources ({listing}). Pick one with --from.")


def _resolve_save_file(name: str, steam_id: str | None) -> Path:
    """A path to a .sav, or a slot name like RedDevil_1 in your own folder."""
    candidate = Path(name)
    if candidate.is_file():
        return candidate
    folder = local_save_folder(steam_id)
    filename = name if name.lower().endswith(".sav") else f"{name}.sav"
    path = folder / filename
    if path.is_file():
        return path
    slots = sorted(p for p in folder.glob("*.sav") if is_save_file(p))
    listing = "\n".join(f"  {p.stem:28} {parse_header(p).display_label}" for p in slots)
    raise TransferError(f"No save named {filename} in {folder}. Saves there:\n{listing}")


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="bellwright_save_converter",
        description="Move Bellwright co-op saves between Steam accounts.",
        epilog="Run with no arguments to open the GUI.",
    )
    parser.add_argument("--steam-id", help="override the detected local SteamID64")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("whoami", help="show the detected Steam ID and save folder")

    p_list = sub.add_parser("list", help="show save sets in a folder or zip")
    p_list.add_argument("--from", dest="source", help="folder or .zip (default: auto-detect)")
    p_list.add_argument("--mine", action="store_true", help="list your own saves instead")

    p_import = sub.add_parser("import", help="copy saves into your own folder")
    p_import.add_argument("--from", dest="source", help="folder or .zip (default: auto-detect)")
    p_import.add_argument(
        "--save", action="append", help="save set name; repeatable (default: all)"
    )
    p_import.add_argument("--dry-run", action="store_true", help="show what would happen")
    p_import.add_argument("--no-backups", action="store_true", help="skip .sav_backup0 files")
    p_import.add_argument("--no-snapshot", action="store_true", help="skip the safety snapshot")
    p_import.add_argument("--force", action="store_true", help="proceed even if the game is open")

    p_export = sub.add_parser("export", help="zip your saves to hand to someone else")
    p_export.add_argument(
        "--save", action="append", help="save set name; repeatable (default: all)"
    )
    p_export.add_argument("--to", required=True, help="output .zip path")
    p_export.add_argument("--from", dest="source", help="folder to export (default: your own)")
    p_export.add_argument("--no-backups", action="store_true", help="skip .sav_backup0 files")

    sub.add_parser("backup", help="snapshot your current saves")

    p_restore = sub.add_parser("restore", help="restore a snapshot")
    p_restore.add_argument("archive", nargs="?", help="snapshot .zip (default: the newest)")
    p_restore.add_argument("--force", action="store_true", help="proceed even if the game is open")

    p_respawn = sub.add_parser(
        "respawn", help="reset harvested ore nodes in one of your saves (experimental)"
    )
    p_respawn.add_argument(
        "save", help="slot in your folder, e.g. RedDevil_1, or a path to a .sav"
    )
    p_respawn.add_argument("--dry-run", action="store_true", help="report what would be reset")
    p_respawn.add_argument("--out", help="write the result here instead of replacing the save")
    p_respawn.add_argument("--oodle", help="path to an oo2core_*_win64.dll (default: search)")
    p_respawn.add_argument(
        "--mesh",
        default=ORE_MESH_PATTERN,
        help="regex of foliage mesh types to reset (default: copper, tin and iron ore; "
        "only the default has been tested in game)",
    )
    p_respawn.add_argument(
        "--force",
        action="store_true",
        help="proceed even if the game is open or the save format is untested",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        # No arguments means someone double-clicked the file; show the GUI.
        from .gui import run_gui

        return run_gui()

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    try:
        return _dispatch(args)
    except (TransferError, DetectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    steam_id = args.steam_id
    if steam_id and not is_steam_id64(steam_id):
        raise TransferError(f"Not a valid SteamID64: {steam_id}")

    if args.command == "whoami":
        detected = steam_id or detect_local_steam_id()
        folder = local_save_folder(detected)
        print(f"Steam ID:    {detected}")
        print(f"Save folder: {folder}")
        print(f"Exists:      {'yes' if folder.is_dir() else 'no'}")
        print(f"Backups:     {backups_root()}")
        return 0

    if args.command == "list":
        source = local_save_folder(steam_id) if args.mine else _resolve_default_source(args.source)
        with resolve_source(source) as folder:
            print(f"\n{source}\n")
            print(_format_sets(scan_folder(folder)))
            print()
        return 0

    if args.command == "import":
        _require_game_closed(args.force)
        source = _resolve_default_source(args.source)
        dest = local_save_folder(steam_id)
        with resolve_source(source) as folder:
            planned = plan_import(
                folder, dest, names=args.save, include_backups=not args.no_backups
            )
            if not args.dry_run and not args.no_snapshot:
                snapshot(dest, tag="pre-import")
            execute_import(planned, dry_run=args.dry_run)
        return 0

    if args.command == "export":
        source = Path(args.source) if args.source else local_save_folder(steam_id)
        export_zip(
            source,
            Path(args.to),
            names=args.save,
            include_backups=not args.no_backups,
        )
        return 0

    if args.command == "backup":
        snapshot(local_save_folder(steam_id), tag="manual")
        return 0

    if args.command == "restore":
        _require_game_closed(args.force)
        if args.archive:
            archive = Path(args.archive)
        else:
            snapshots = list_snapshots()
            if not snapshots:
                raise TransferError(f"No snapshots found in {backups_root()}")
            archive = snapshots[0]
            print(f"Restoring newest snapshot: {archive.name}")
        dest = local_save_folder(steam_id)
        with resolve_source(archive) as folder:
            execute_import(plan_import(folder, dest))
        return 0

    if args.command == "respawn":
        return _respawn(args, steam_id)

    raise TransferError(f"Unknown command: {args.command}")


def _respawn(args, steam_id: str | None) -> int:
    target = _resolve_save_file(args.save, steam_id)
    if not args.dry_run and not args.out:
        _require_game_closed(args.force)

    header = parse_header(target)
    if not header.recognized:
        raise TransferError(f"{target.name} is not a Bellwright save.")
    if header.version != RESPAWN_TESTED_VERSION and not args.force:
        raise TransferError(
            f"{target.name} is format v{header.version}; respawn is only tested on "
            f"v{RESPAWN_TESTED_VERSION}. Try --out first, then --force."
        )

    oodle = Oodle(find_oodle(args.oodle))
    new_raw, removed = respawn_nodes(target.read_bytes(), oodle, args.mesh)

    when = header.saved.strftime("%Y-%m-%d %H:%M") if header.saved else "unknown"
    print(f"\n{target.name}  ({header.display_label}, saved {when})")
    if not removed:
        print("  No harvested nodes of that kind are recorded; nothing to reset.")
        return 0
    for mesh, count in sorted(removed.items()):
        print(f"  {count:5}  {mesh}")
    print(f"  {sum(removed.values()):5}  nodes reset")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0

    if args.out:
        out = Path(args.out)
        if out.exists():
            raise TransferError(f"{out} already exists; not overwriting.")
        out.write_bytes(new_raw)
        print(f"\nWrote {out}")
        return 0

    left = write_respawned(target, new_raw, oodle, args.mesh)
    if left:
        raise TransferError(
            f"Check failed: {left} matching records still in {target.name}; "
            "restore the pre-respawn snapshot."
        )
    print(f"\nReset {target.name} in place. Read it back from disk: 0 matching records left.")
    print(f"Load '{header.display_label}' in game.")
    return 0
