# Security notes

Desktop Organizer is a local, single-user tool. It makes no network connections and runs no
files. Its risk is mostly about moving the wrong files to the wrong place. This document
records what was checked and what is still a limitation.

## Protections in place

| Risk | Mitigation |
|------|------------|
| Config folder names such as `../../x` or `C:\Windows` sending files outside the chosen folder | Folder names are validated on load (single-level names only, no invalid characters, no trailing dots/spaces, no reserved Windows names) and the final destination is checked to be inside the chosen folder before every move |
| A tampered or corrupt undo manifest moving arbitrary files | Undo/Redo refuse any path that is not inside the folder recorded in the manifest, and reject malformed manifests |
| Half-written config or manifest after a crash or power loss | Files are written to a temp file and atomically replaced |
| Lost history after a mid-run failure | Moves are recorded as they happen; the manifest is written in a `finally` block |
| Overwriting existing files | Collisions get a numeric suffix |
| Renamed copy of a malicious file being filed as a harmless duplicate | Copies of a quarantined file are quarantined too |
| Extension spoofing with right-to-left override characters | Filenames containing bidi control characters are treated as suspicious |
| Moving system files (`desktop.ini`) | Windows hidden/system files are skipped |
| Other users reading logs/manifests (which contain file paths) | The app folder is created with mode 700 on POSIX; log is size-rotated |
| Weak hash | SHA-256 instead of MD5 for duplicate detection |
| Corrupt or wrongly typed config values | Values are type-checked and fall back to defaults |

## Known limitations

- **"Suspicious" is a heuristic, not antivirus.** It looks at file names, extensions and entropy. A malicious file with an innocent name will not be flagged. Quarantined files are only moved, not blocked or neutralised, and can still be run.
- **Race condition on collisions.** The destination name is chosen and then the file is moved. If another program creates a file with that exact name in between, the move could replace it on some platforms. This needs a second process acting in the same millisecond window; it is accepted for a local desktop tool.
- **Screenshot button** captures the whole screen into memory and shows it in the window. It is not saved to disk, but it can display sensitive content on screen.
- **Windows Desktop detection** (OneDrive redirection) uses the Windows shell API and could not be tested on Windows during development.
- **Config is trusted input.** Anyone who can edit `config.json` can change where files are sorted inside the chosen folder, though not outside it.
- **Saved lists are not auto-merged.** An older `config.json` keeps its own `risky_extensions` / `bait_words` lists; delete those keys to receive newer defaults.

## Reporting a problem

Please open an issue on the repository. For anything sensitive, contact the repository owner directly instead of posting details publicly.
