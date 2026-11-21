# Bellwright Save Converter

Move a Bellwright co-op world between two Steam accounts, so whoever is hosting tonight has the
save in their own folder.

It can also reset harvested ore nodes in a save — see [Respawning ore nodes](#respawning-ore-nodes).

Single file, standard library only, no install. (`respawn` also borrows an Oodle DLL from another
installed game.)

## Why this exists

Bellwright stores each world under the host's Steam ID:

```
%LOCALAPPDATA%\Bellwright\Saved\SaveGames\<SteamID64>\
```

Only the account owning that folder can host from it. Swapping hosts means copying files between
two ID-named folders every session, which is fiddly and easy to get wrong at 1am.

## How it works

**Transfers copy saves byte for byte. Nothing inside them is modified.**

Ownership is carried by the *folder name*, not by anything in the file, so moving a world between
accounts never needs to rewrite it. Any tool that claims to "convert" the contents of a Bellwright
save for a transfer is doing something you don't want. The only command here that edits a save is
`respawn`, and only because you asked it to.

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
| `respawn SLOT` | Reset harvested ore nodes in one save (`--dry-run`, `--out FILE`) |

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

## Respawning ore nodes

Bellwright only saves the foliage you've interacted with: every ore chunk picked up or deposit
mined gets one record in the save, and untouched nodes have none. Deleting a node's record puts it
back in its untouched, harvestable state.

```bash
python bellwright_save_converter.py respawn RedDevil_1 --dry-run
```

```bash
python bellwright_save_converter.py respawn RedDevil_1
```

`RedDevil_1` is the slot's filename without `.sav`; run with a wrong name and it lists your slots
with their in-game labels. It resets copper and tin surface pickups plus copper, iron and tin rock
deposits — that set is what was tested in game.

In the window, **Respawn ores...** does the same: pick a save, and it shows how many nodes will be
reset (split by copper, tin and iron) before asking to confirm. After writing it reads the save
back from disk and reports how many harvested ore records are left, which should be 0.

- **Close the game first.** In-place writes refuse to run while it's open.
- **A snapshot is taken first** (`pre-respawn-*.zip` in `SaveBackups`), and the edit is checked
  before writing: every other record must come through byte-identical and in order, and the
  repacked file must decompress back to exactly the edited data. After writing, the save is read
  back from disk and the remaining ore records are counted.
- **`--out FILE`** writes the result somewhere else and leaves the save alone — use it to try
  this on a copy first.
- **Format v8 only.** A game patch that changes the save format makes it refuse until checked;
  `--force` overrides.
- Steam Cloud will upload the edited save. That's what you want here, but it also means the old
  ore state is only recoverable from the snapshot.

It needs Oodle, the compression library Bellwright uses. Bellwright builds it into its executable,
but many games ship it as `oo2core_9_win64.dll` (Warframe, Destiny 2, ...) and any copy works. The
tool searches your Steam libraries for one; copy it next to the script, set `BELLWRIGHT_OODLE`, or
pass `--oodle` to skip the search.

## Notes

- Windows only in practice (Steam registry lookup, `%LOCALAPPDATA%` paths, `tasklist`).
- Python 3.10+.
- `.sav_backup0` fallback files are included by default; `--no-backups` to skip them.
- Save sets group on the exact character token, so `SirRed`, `SirRedDevil` and `SirRedDevil1` stay
  separate rather than being folded together by a prefix match.
