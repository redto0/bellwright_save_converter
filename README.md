# Bellwright Save Converter

Move a Bellwright world between two Steam accounts, and reset harvested ore nodes.

## Requirements

- Windows with Python 3.10 or later.
- Ore respawn also needs 64-bit Python and an Oodle library (`oo2core_9_win64.dll`) from any
  installed game that ships one. The tool finds it automatically; see
  [TECHNICAL.md](TECHNICAL.md#oodle).

## Before you start

Close Bellwright before importing, restoring or respawning. If Steam reports a cloud conflict on
the next launch, choose the **local** files.

## Usage

Double-click `bellwright_save_converter.py` to open the window, or use the command line below.

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
