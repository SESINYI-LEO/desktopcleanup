# Desktop Organizer v5

A small desktop app that tidies a folder (your Desktop by default) by moving files into category
folders such as `Documents`, `Images` and `Videos`. It detects duplicates, quarantines suspicious
files, shows a preview before touching anything, and can undo every sort.

## Requirements

- Python 3.9 or newer (developed and tested on 3.13)
- Tkinter (included with the Windows/macOS Python installers; on Debian/Ubuntu: `sudo apt install python3-tk`)
- [Pillow](https://pypi.org/project/Pillow/), only for the optional **Screenshot** button

## Setup and run

```bash
git clone https://github.com/SESINYI-LEO/desktopcleanup.git
cd desktopcleanup
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python desktop_organizer_v5.py
```

## How to use it

1. Check the path (the app tries to find your real Desktop, including OneDrive-redirected ones) or click **Browse**.
2. Click **Preview**. Nothing is moved; the log lists where each file would go and why.
3. Click **Apply Sort** and confirm. Only files directly inside the chosen folder are moved; existing sub-folders are left alone.
4. **Undo** / **Redo** step through your last 10 sorts. History is stored on disk, so Undo still works after you close and reopen the app.
5. **Add Category** creates your own folder with extensions and/or filename keywords (e.g. `Design` for `psd, ai`).

## How a file is classified

The first matching rule wins:

1. **Duplicate**: same content as a file already seen in this run goes to `Duplicates` (the first copy stays in its normal category; empty files are never duplicates). A renamed copy of a suspicious file is quarantined, not filed as a duplicate.
2. **Suspicious** (`_Suspicious`): double extensions like `report.pdf.exe`, right-to-left-override tricks in the name, or a risky extension (`.exe`, `.bat`, `.ps1`, ...) with bait words such as `invoice` or `password`.
3. **Custom categories** you added.
4. **Games**: filename contains a game keyword *and* the file is a launcher type (`.exe`, `.lnk`, `.url`, `.msi`, `.bat`). So `game_plan.docx` is not a game.
5. **Extension rules** (`.pdf` -> `Documents`, etc.).
6. **High entropy** unknown files (looks encrypted/packed) go to `_Suspicious`; everything else to `Other`.

Hidden files, Windows hidden/system files (such as `desktop.ini`) and symlinks are skipped by default.

## Files and settings

Everything the app writes lives in `~/.desktop_organizer_v5/` (override with the `DESKTOP_ORGANIZER_HOME` environment variable):

| Path | Purpose |
|------|---------|
| `config.json` | Your settings (created on first run) |
| `manifests/` | One JSON file per sort, used for Undo/Redo |
| `app.log` | Rotating log |

Useful `config.json` keys:

| Key | Meaning |
|-----|---------|
| `default_path` | Folder shown at startup |
| `max_files` | Refuse to sort more files than this (default 2000) |
| `skip_hidden_files`, `skip_symlinks` | Skip these (default `true`) |
| `skip_extensions` | Extensions never moved, e.g. `[".lnk"]` to leave shortcuts on the Desktop |
| `rules` | Extension -> folder |
| `games_keywords`, `games_extensions` | Game detection (`[]` for extensions = any file type) |
| `suspicious` | Folder name, entropy threshold, risky extensions, bait words |
| `folders` | Names for `duplicates`, `other`, `games` |
| `custom_categories` | Added with the Add Category button |

Folder names in the config must be plain names (no `/`, `\`, `..`, drive letters or reserved Windows names); unsafe values are replaced by a safe default and logged.
Your saved config is merged on top of the built-in defaults, so a list you saved earlier (for example `risky_extensions`) replaces the default list rather than adding to it; delete the key to pick up newer defaults.

## Project layout

```
desktop_organizer_v5.py   Tkinter UI (thin; no sorting logic)
organizer_core.py         Classification, sorting, manifests, undo/redo, config (no GUI imports)
tests/                    pytest suite
SECURITY.md               Security notes and known limitations
```

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests use temporary folders and never touch your real Desktop or config.

## Technical notes

- **Move safety.** A name collision never overwrites: the incoming file becomes `name_1.ext`, `name_2.ext`, and so on.
- **Manifests.** Each move is recorded as it happens and the manifest is written even if a run fails halfway, so partial runs can be undone. Undo only marks a sort as undone if every file was restored, so a failed undo can be retried.
- **Duplicate detection** compares file sizes first and only hashes (SHA-256) files that share a size.
- **Threading.** Scans, sorts and undo/redo run on a worker thread and report back through a queue, so the window stays responsive.
