# Technical notes

## Code layout

`bellwright_save_converter.py` is a launcher for the `bellwright` package. Standard library only.

| Module | Responsibility |
|---|---|
| `saves.py` | Save header parsing and grouping files by character |
| `steam.py` | Steam account, save folder and library discovery |
| `transfer.py` | Import, export, snapshots and the game-running check |
| `oodle.py` | Locating and calling the Oodle library |
| `protobuf.py` | Schema-less protobuf reading and rewriting |
| `respawn.py` | Unpacking and repacking saves; ore node removal |
| `cli.py` | Command line |
| `gui.py` | Tkinter window |

## Transfers

Bellwright stores each world under the host's Steam ID:

```
%LOCALAPPDATA%\Bellwright\Saved\SaveGames\<SteamID64>\
```

Ownership is carried by the folder name, not by anything in the save, so a transfer is a
byte-for-byte copy into the receiving player's folder. Nothing in the file is rewritten.

- Saves are grouped by the exact token before the first `_` in the file name. Prefix matching
  would merge distinct characters such as `SirRed`, `SirRedDevil` and `SirRedDevil1`.
- `steam_autocloud.vdf` is never copied; it holds the folder owner's account ID.
- Every copied file is verified by SHA-256. A failure mid-import removes the copies and restores
  the originals.
- Files an import would replace are moved to `SaveBackups\pre-import-<timestamp>\` first.
- The local Steam ID comes from `HKCU\Software\Valve\Steam\ActiveProcess\ActiveUser`, then the
  `AutoLogin` entry in `loginusers.vdf`, then a sole folder under `SaveGames`.
  SteamID64 = account ID + 76561197960265728.

### Steam Cloud

Bellwright syncs saves through Steam Cloud. A save removed while the game is closed is restored
from the cloud on the next launch; a deletion made while the game is running is synced when it
exits. Delete unwanted saves in game, or while the game is running.

## Save file format

### Header

| Offset | Type | Field |
|---|---|---|
| `0x000` | 4 bytes | Magic `VSWB` |
| `0x004` | u32 | Format version: 7 in saves written by build 0.0.47128 and earlier, 8 by build 0.0.52204. Selects the deserializer. |
| `0x008` | u64 | Created, Unix seconds |
| `0x010` | u64 | Last saved, Unix seconds |
| `0x018` | u64 | Uncompressed body size; the engine reads exactly this many bytes |
| `0x020` | 256 bytes | Character name, NUL-terminated UTF-8 |
| `0x120` | 256 bytes | Map name |
| `0x220` | 256 bytes | Save label |
| `0x320` | 256 bytes | Settlement name |
| `0x420` | f64 | World clock, in-game seconds |

### Compressed archive

A UE5 compressed archive follows the header. Its offset is not fixed, so the tool locates it by
its tag:

| Field | Type |
|---|---|
| Tag `0x222222229E2A83C1` (`ARCHIVE_V2_HEADER_TAG`) | u64 |
| Chunk size (131072) | i64 |
| Compressor ID (preserved as found) | u8 |
| Total compressed size, total uncompressed size | 2 × i64 |
| Per chunk: compressed size, uncompressed size | 2 × i64 |
| Chunk data, one Oodle Kraken block per chunk, to end of file | — |

The header, archive and body carry no checksum, and the game ships no anti-cheat.

### Body

The decompressed body is protobuf without a published schema; field numbers below were inferred.

| Field | Content |
|---|---|
| 1 | Character name |
| 2 | Map name |
| 8 | String table (repeated); other messages refer to strings by index |
| 9 | Save label |
| 10 | Settlement name |
| 12 / 13 | Created / last saved, Unix seconds |
| 14 / 15 | Game build that created / last saved the world, e.g. `0.0.52204` |
| 17 | Identifier of the host that wrote the save |
| 18 | In-game day |
| 21 | World |

World records are at `21 > 2 > 2 > 2`. Each has a class (field 5, a string-table index) and an
identity (field 4).

## Ore respawn

Bellwright persists only the foliage the player has interacted with. Each harvested node has one
`MistFoliageDataComponent` record, identified by mesh type and instance ID; untouched foliage has
no record. Removing a node's record returns it to its default, harvestable state on the next load.
This was confirmed in game on format version 8.

`respawn` removes records whose mesh type fully matches:

```
SM_(CopperOre_\d+|Tin|MEDIUM_(Copper|Iron|Tin)Rock_\d+)
```

These are the copper and tin surface pickups and the copper, iron and tin rock deposits. Matching
is exact: a pattern such as `ore` would also match every `Forest` mesh.

Before writing, the tool checks that:

1. the number of records removed equals the number matched;
2. every other record is byte-identical and in its original order;
3. body fields outside the world are unchanged;
4. the repacked file decompresses to exactly the edited body.

It then snapshots the save folder, writes through a temporary file and an atomic rename, reads the
file back and counts the remaining matching records, which should be 0. Saves with a format version
other than 8 are refused unless `--force` is given.

### Oodle

Bellwright links Oodle statically, so its install contains no Oodle DLL. Any
`oo2core_*_win64.dll` works; Warframe ships one under `Tools\Oodle` and Destiny 2 under `bin\x64`.
The tool looks for one in this order:

1. `--oodle PATH`
2. the `BELLWRIGHT_OODLE` environment variable
3. the folder containing `bellwright_save_converter.py`, or the .exe in a packaged build
4. every Steam library (slow; copy the DLL next to the program to skip this)

Only `OodleLZ_Decompress` and `OodleLZ_Compress` (Kraken, level Normal) are used. Oodle is
proprietary, so it is never bundled into the .exe or a release, and `.gitignore` excludes `*.dll`.

### Format version

Changing `0x004` from 8 to 7 does not make the game repeat its v7-to-v8 migration. The engine reads
the v8 body with the v7 deserializer and aborts with `BulkData compressed header read error`
(`Archive.cpp`, line 797).

## Building the .exe

`.github/workflows/build.yml` builds a single windowed executable with PyInstaller on a Windows
runner. Before publishing, it runs the built exe with `--help` and requires a clean exit, which
proves the bundle imports everything it needs.

| Channel | Trigger | Release |
|---|---|---|
| Rolling | Every push to `main` | The `rolling` pre-release, replaced each time |
| Stable | Pushing a `v*` tag, e.g. `git tag v1.1.0 && git push origin v1.1.0` | A release marked Latest |

The README links to `releases/latest/download/BellwrightSaveConverter.exe`, which always serves
the newest stable release. The rolling build is a pre-release so it never takes that slot.

To build locally:

```bash
python -m pip install "pyinstaller>=6.10,<7"
```

```bash
pyinstaller --onefile --windowed --name BellwrightSaveConverter --hidden-import bellwright.gui bellwright_save_converter.py
```

The result is `dist\BellwrightSaveConverter.exe`. It is not code-signed, so Windows SmartScreen
warns on first launch, and some antivirus products flag unsigned PyInstaller builds.
