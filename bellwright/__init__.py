r"""Bellwright save converter -- move a co-op world between two Steam accounts.

Bellwright keeps each world under::

    %LOCALAPPDATA%\Bellwright\Saved\SaveGames\<SteamID64>\

Whoever hosts needs the world files sitting in *their own* Steam ID folder, so
handing a campaign back and forth means copying files between two ID-named
folders every session. This automates that.

Important: transfers copy save files **byte for byte**. Ownership is carried by
the folder name, not by anything inside the file, so moving a world between
accounts never needs to rewrite it.

The one command that does edit a save is ``respawn``, which resets harvested
ore nodes. It is opt-in, snapshots the folder first, and proves its output
round-trips before writing anything.

Run with no arguments for the GUI, or with arguments for the CLI.
Standard library only. ``respawn`` also needs Oodle's oo2core DLL, which it
finds in other installed games.
"""
