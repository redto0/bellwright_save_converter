# Bellwright Save Converter

Move a Bellwright co-op world between two Steam accounts, so whoever is hosting tonight has the
save in their own folder.

Single file, standard library only, no install.

## Why this exists

Bellwright stores each world under the host's Steam ID:

```
%LOCALAPPDATA%\Bellwright\Saved\SaveGames\<SteamID64>\
```

Only the account owning that folder can host from it. Swapping hosts means copying files between
two ID-named folders every session, which is fiddly and easy to get wrong at 1am.

## How it works

**Saves are copied byte for byte. Nothing inside them is modified.**

Ownership is carried by the *folder name*, not by anything in the file. A save's body is
compressed, so rewriting it would not be safe even if it were necessary — and it isn't. Any tool
that claims to "convert" the contents of a Bellwright save is doing something you don't want.

For transfers the header is only read, to show you which save is which:

| Offset | Field |
|---|---|
| `0x00` | magic `VSWB` |
| `0x04` | format version — 7 before the mid-2026 patch, 8 after; selects the deserializer |
| `0x08` / `0x10` | created / last saved, unix epoch |
| `0x18` | uncompressed body size (the engine's BulkData "Read Size") |
| `0x20` / `0x120` / `0x220` | character / map / save label |
| `0x320` | settlement name |
| `0x420` | world clock, in-game seconds (f64) |
| then | UE5 compressed archive: 128 KiB chunks, Oodle Kraken, protobuf inside |

## Use it

Double-click `bellwright_save_converter.py` for the window, or use the command line:

```bash
python bellwright_save_converter.py whoami
```

```bash
python bellwright_save_converter.py list --from E:\76561198034828952
```

```bash
python bellwright_save_converter.py import --save RedDevil --dry-run
```

Drop `--dry-run` to actually copy. With no `--from`, it finds a `<drive>:\<SteamID64>` folder
(a handed-over USB stick) on its own.

### Commands

| Command | What it does |
|---|---|
| `whoami` | Show the detected Steam ID and save folder |
| `list` | Show save sets in a folder or zip (`--mine` for your own) |
| `import` | Copy saves into your folder (`--save NAME`, `--dry-run`) |
| `export --to X.zip` | Zip saves to hand to the other player |
| `backup` | Snapshot your current saves |
| `restore [archive]` | Restore a snapshot (newest if unnamed) |

Your Steam ID is detected from the registry, then `loginusers.vdf`, then the save folder — nothing
is hardcoded. Override with `--steam-id` if needed.

## Handing the world back

```bash
python bellwright_save_converter.py export --save RedDevil --to D:\RedDevil.zip
```

They import the zip directly — `import --from RedDevil.zip` — so it doesn't matter whose Steam ID
folder it came out of.

## Safety

- **Close Bellwright first.** Imports refuse to run while the game is open (`--force` overrides).
- **Nothing is overwritten.** Existing files are moved into a timestamped snapshot under
  `%LOCALAPPDATA%\Bellwright\SaveBackups\` before anything is written.
- **Every copy is verified** by SHA-256 against its source; a failure mid-run unwinds the copies
  and puts your originals back.
- **`steam_autocloud.vdf` is never copied.** It holds the *folder owner's* account id — copying it
  would point your folder at someone else's Steam account.
- A snapshot is taken automatically before each import (`--no-snapshot` to skip).

## Steam Cloud

Bellwright uses Steam Cloud, which is the one thing that can undo a good import.

Close **Steam** as well as the game before importing. On the next launch Steam may report a
conflict between your machine and the cloud — choose the **local** files, or the world you just
imported gets replaced by whatever the cloud had. For a long swapping session it's easiest to turn
off Steam Cloud for Bellwright in its properties.

If it does go wrong, `restore` puts back the snapshot taken before the import.

## Notes

- Windows only in practice (Steam registry lookup, `%LOCALAPPDATA%` paths, `tasklist`).
- Python 3.10+.
- `.sav_backup0` fallback files are included by default; `--no-backups` to skip them.
- Save sets group on the exact character token, so `SirRed`, `SirRedDevil` and `SirRedDevil1` stay
  separate rather than being folded together by a prefix match.
