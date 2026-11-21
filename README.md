# Bellwright Save Converter

Move a Bellwright world between two Steam accounts, and reset harvested ore nodes.

[![Download for Windows](https://img.shields.io/badge/Download_for_Windows-BellwrightSaveConverter.exe-2ea44f?style=for-the-badge)](https://github.com/redto0/bellwright_save_converter/releases/latest/download/BellwrightSaveConverter.exe)

## Download

1. Click **Download for Windows** above to get the current stable version.
2. Double-click the downloaded file. The first time, Windows may show "Windows protected your PC" because the app
   is not code-signed: click **More info**, then **Run anyway**.
3. To keep it handy, right-click its taskbar icon while it is open and choose **Pin to taskbar**.

The newest untested build is always available as the
[rolling pre-release](https://github.com/redto0/bellwright_save_converter/releases/tag/rolling).

Ore respawn needs an Oodle library (`oo2core_9_win64.dll`) from a game installed on the same PC,
such as Warframe or Destiny 2. It is found automatically; see [TECHNICAL.md](TECHNICAL.md#oodle).

## Before you start

Close Bellwright before importing, restoring or respawning. If Steam reports a cloud conflict on
the next launch, choose the **local** files.

## Usage

The .exe opens the window. To use the command line instead, run from source with Python 3.10 or
later (64-bit for ore respawn): double-click `bellwright_save_converter.py` for the window, or use
the commands below. Keep the `bellwright` folder next to it.

### Take over a world

```bash
python bellwright_save_converter.py list --from E:\76561198034828952
```

```bash
python bellwright_save_converter.py import --save RedDevil
```

Without `--from`, the tool looks for a `<drive>:\<SteamID64>` folder, such as a USB stick.

### Hand a world back

```bash
python bellwright_save_converter.py export --save RedDevil --to E:\RedDevil.zip
```

The other player imports the zip with `import --from E:\RedDevil.zip`.

### Respawn ore nodes

```bash
python bellwright_save_converter.py respawn RedDevil_1 --dry-run
```

```bash
python bellwright_save_converter.py respawn RedDevil_1
```

`RedDevil_1` is the save's file name without `.sav`. Harvested copper, tin and iron nodes are
reset. In the window, use **Respawn ores...**.

### Commands

| Command | Description |
|---|---|
| `whoami` | Show the detected Steam ID and save folder |
| `list` | List saves in a folder or zip; `--mine` for your own |
| `import` | Copy saves into your folder; `--save NAME`, `--dry-run` |
| `export --to FILE.zip` | Zip saves to hand to another player |
| `backup` | Snapshot your save folder |
| `restore [FILE.zip]` | Restore a snapshot; the newest by default |
| `respawn SAVE` | Reset harvested ore nodes; `--dry-run`, `--out FILE` |

Run any command with `--help` for all options.

## Backups

Imports and respawns snapshot your save folder to `%LOCALAPPDATA%\Bellwright\SaveBackups\` before
writing, and files an import would replace are moved there instead of overwritten. Use `restore`
to undo.

## Technical details

Save format, safety design and how ore respawn works: [TECHNICAL.md](TECHNICAL.md).
