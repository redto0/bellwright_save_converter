"""Finding the local Steam account, its Bellwright save folder and Steam libraries."""

from __future__ import annotations

from pathlib import Path

from .saves import is_save_file


#: SteamID64 = account id + this. Used to convert the 32-bit ids Steam stores
#: in the registry and in ``userdata`` folder names.
STEAM_ID64_BASE = 76561197960265728

_DEFAULT_STEAM_PATHS = (
    Path(r"C:\Program Files (x86)\Steam"),
    Path(r"C:\Program Files\Steam"),
)


def is_steam_id64(value: str) -> bool:
    """True for a plausible SteamID64 (17 digits, individual-account range)."""
    return len(value) == 17 and value.isdigit() and value.startswith("7656119")


def account_id_to_steam_id64(account_id: int) -> str:
    return str(account_id + STEAM_ID64_BASE)


def steam_install_path() -> Path | None:
    """Locate the Steam install, preferring the registry over guessing."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            raw, _ = winreg.QueryValueEx(key, "SteamPath")
            candidate = Path(str(raw))
            if candidate.is_dir():
                return candidate
    except (ImportError, OSError):
        pass

    for candidate in _DEFAULT_STEAM_PATHS:
        if candidate.is_dir():
            return candidate
    return None


def _steam_id_from_registry() -> str | None:
    """Currently signed-in user, from Steam's ActiveUser account id."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam\ActiveProcess"
        ) as key:
            account_id, _ = winreg.QueryValueEx(key, "ActiveUser")
    except (ImportError, OSError):
        return None

    if not account_id:  # zero means Steam is not running
        return None
    candidate = account_id_to_steam_id64(int(account_id))
    return candidate if is_steam_id64(candidate) else None


def _steam_id_from_loginusers() -> str | None:
    """Last auto-login account, from Steam's loginusers.vdf."""
    import re

    steam_path = steam_install_path()
    if steam_path is None:
        return None
    vdf = steam_path / "config" / "loginusers.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    blocks = re.findall(r'"(\d{17})"\s*\{(.*?)\}', text, re.DOTALL)
    for steam_id, body in blocks:
        if re.search(r'"AutoLogin"\s*"1"', body) and is_steam_id64(steam_id):
            return steam_id
    # No explicit auto-login flag: only safe to guess when there is one account.
    if len(blocks) == 1 and is_steam_id64(blocks[0][0]):
        return blocks[0][0]
    return None


def savegames_root() -> Path:
    r"""``%LOCALAPPDATA%\Bellwright\Saved\SaveGames``."""
    import os

    local_appdata = os.environ.get("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base / "Bellwright" / "Saved" / "SaveGames"


def _steam_id_from_savegames() -> str | None:
    """Fall back to the save folder itself when exactly one account has played."""
    root = savegames_root()
    if not root.is_dir():
        return None
    folders = [d.name for d in root.iterdir() if d.is_dir() and is_steam_id64(d.name)]
    return folders[0] if len(folders) == 1 else None


class DetectionError(RuntimeError):
    """Raised when the local Steam account cannot be determined."""


def detect_local_steam_id() -> str:
    """Determine this machine's Steam account, trying the most reliable first.

    Nothing is hardcoded: the registry knows who is signed in right now, the
    login file knows who signed in last, and the save folder knows who has
    actually played. Any one of them is enough.
    """
    for source in (
        _steam_id_from_registry,
        _steam_id_from_loginusers,
        _steam_id_from_savegames,
    ):
        found = source()
        if found:
            return found
    raise DetectionError(
        "Could not determine your Steam ID automatically. "
        "Pass --steam-id <17-digit id> explicitly."
    )


def local_save_folder(steam_id: str | None = None) -> Path:
    """The folder this machine's Bellwright reads and writes."""
    return savegames_root() / (steam_id or detect_local_steam_id())


def _has_saves(folder: Path) -> bool:
    try:
        return any(p.is_file() and is_save_file(p) for p in folder.iterdir())
    except OSError:
        return False


def find_save_sources(own_steam_id: str | None) -> list[tuple[Path, str]]:
    """Save folders to offer as a source, most likely first, each with a description.

    Other Steam accounts' saves on this PC come first, then your own -- so the
    window shows your saves even when there is nothing to import.
    """
    sources: list[tuple[Path, str]] = []
    root = savegames_root()
    if root.is_dir():
        others = sorted(
            folder for folder in root.iterdir()
            if folder.is_dir() and is_steam_id64(folder.name)
            and folder.name != own_steam_id and _has_saves(folder)
        )
        sources += [(folder, "another Steam account on this PC") for folder in others]
        own = root / own_steam_id if own_steam_id else None
        if own is not None and own.is_dir():
            sources.append((own, "your saves"))
    return sources


def steam_library_paths() -> list[Path]:
    """Every Steam library folder listed in libraryfolders.vdf."""
    import re

    steam = steam_install_path()
    if steam is None:
        return []
    libraries = [steam]
    try:
        text = (steam / "steamapps" / "libraryfolders.vdf").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return libraries
    for raw in re.findall(r'"path"\s*"([^"]+)"', text):
        path = Path(raw.replace("\\\\", "\\"))
        if path.is_dir() and path not in libraries:
            libraries.append(path)
    return libraries
