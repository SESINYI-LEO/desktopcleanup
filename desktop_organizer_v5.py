import hashlib
import json
import logging
import math
import re
import shutil
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

from PIL import ImageGrab, ImageTk


APP_DIR = Path.home() / ".desktop_organizer_v5"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "app.log"
MANIFEST_DIR = APP_DIR / "manifests"
APP_DIR.mkdir(parents=True, exist_ok=True)
MANIFEST_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8")],
        force=True,
    )


setup_logging()


DEFAULT_CONFIG = {
    "default_path": str(Path.home() / "Desktop"),
    "max_files": 2000,
    "skip_hidden_files": True,
    "skip_symlinks": True,
    "rules": {
        ".pdf": "Documents",
        ".docx": "Documents",
        ".txt": "Documents",
        ".rtf": "Documents",
        ".odt": "Documents",
        ".csv": "Documents",
        ".jpg": "Images",
        ".jpeg": "Images",
        ".png": "Images",
        ".gif": "Images",
        ".webp": "Images",
        ".mp4": "Videos",
        ".mkv": "Videos",
        ".avi": "Videos",
        ".mov": "Videos",
        ".mp3": "Music",
        ".wav": "Music",
        ".flac": "Music",
        ".aac": "Music",
        ".zip": "Archives",
        ".rar": "Archives",
        ".7z": "Archives",
        ".tar": "Archives",
        ".exe": "Applications",
        ".msi": "Applications",
        ".bat": "Applications",
        ".lnk": "Applications",
    },
    "games_keywords": [
        "steam",
        "minecraft",
        "valorant",
        "epic",
        "roblox",
        "game",
        "launcher",
        "riot",
    ],
    "suspicious": {
        "folder": "_Suspicious",
        "entropy_threshold": 7.5,
        "risky_extensions": [".exe", ".bat", ".cmd", ".ps1", ".js", ".vbs", ".scr", ".msi"],
        "double_extension_targets": [".exe", ".bat", ".cmd", ".ps1", ".js", ".vbs", ".scr"],
        "bait_words": ["invoice", "receipt", "payment", "urgent", "password", "bank"],
    },
    "folders": {"duplicates": "Duplicates", "other": "Other", "games": "Games"},
    "custom_categories": {},
}


def merge_config(defaults: dict, user: dict) -> dict:
    merged = dict(defaults)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_config(config: dict) -> dict:
    rules = config.get("rules", {})
    normalized_rules = {}
    for ext, folder in rules.items():
        ext_key = str(ext).lower()
        if not ext_key.startswith("."):
            ext_key = f".{ext_key}"
        normalized_rules[ext_key] = folder
    config["rules"] = normalized_rules

    config["games_keywords"] = [str(keyword).lower() for keyword in config.get("games_keywords", [])]

    suspicious = config.get("suspicious", {})
    suspicious_defaults = DEFAULT_CONFIG["suspicious"]
    suspicious = merge_config(suspicious_defaults, suspicious)
    suspicious["risky_extensions"] = [str(ext).lower() for ext in suspicious.get("risky_extensions", [])]
    suspicious["double_extension_targets"] = [
        str(ext).lower() for ext in suspicious.get("double_extension_targets", [])
    ]
    suspicious["bait_words"] = [str(word).lower() for word in suspicious.get("bait_words", [])]
    config["suspicious"] = suspicious

    folders = merge_config(DEFAULT_CONFIG["folders"], config.get("folders", {}))
    config["folders"] = folders

    normalized_custom_categories = {}
    for category, data in config.get("custom_categories", {}).items():
        category_name = str(category).strip()
        if not category_name:
            continue

        if isinstance(data, list):
            data = {"extensions": data, "keywords": []}
        if not isinstance(data, dict):
            continue

        extensions = []
        for ext in data.get("extensions", []):
            ext_key = str(ext).strip().lower()
            if not ext_key:
                continue
            if not ext_key.startswith("."):
                ext_key = f".{ext_key}"
            extensions.append(ext_key)

        keywords = [str(keyword).strip().lower() for keyword in data.get("keywords", []) if str(keyword).strip()]
        if extensions or keywords:
            normalized_custom_categories[category_name] = {
                "extensions": sorted(set(extensions)),
                "keywords": sorted(set(keywords)),
            }

    config["custom_categories"] = normalized_custom_categories
    return config


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return normalize_config(dict(DEFAULT_CONFIG))

    try:
        user_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        merged = merge_config(DEFAULT_CONFIG, user_config)
        return normalize_config(merged)
    except Exception as exc:
        backup_name = CONFIG_PATH.with_suffix(f".broken_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            shutil.copy2(CONFIG_PATH, backup_name)
        except OSError:
            pass
        logging.exception("Failed to load config, falling back to defaults: %s", exc)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return normalize_config(dict(DEFAULT_CONFIG))


CONFIG = load_config()


def persist_config() -> None:
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")


def md5(file_path: Path):
    digest = hashlib.md5()
    try:
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        logging.warning("Hash failed for %s: %s", file_path, exc)
        return None


def entropy(file_path: Path, max_bytes: int = 128000):
    try:
        with file_path.open("rb") as handle:
            data = handle.read(max_bytes)
    except OSError as exc:
        logging.warning("Entropy read failed for %s: %s", file_path, exc)
        return 0.0

    if not data:
        return 0.0

    freq = [0] * 256
    for byte in data:
        freq[byte] += 1

    value = 0.0
    total = len(data)
    for count in freq:
        if count:
            p = count / total
            value -= p * math.log2(p)
    return round(value, 3)


def get_safe_dest(target_dir: Path, filename: str) -> Path:
    destination = target_dir / filename
    if not destination.exists():
        return destination

    stem, ext = destination.stem, destination.suffix
    index = 1
    while True:
        candidate = target_dir / f"{stem}_{index}{ext}"
        if not candidate.exists():
            return candidate
        index += 1


def tokenize_filename(file_path: Path) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", file_path.stem.lower())
    return set(normalized.split())


def is_game(file_path: Path):
    name = file_path.name.lower()
    tokens = tokenize_filename(file_path)
    for keyword in CONFIG["games_keywords"]:
        if " " in keyword:
            if keyword in name:
                return True
        elif keyword in tokens or keyword in name:
            return True
    return False


def looks_suspicious(file_path: Path) -> bool:
    suspicious_cfg = CONFIG["suspicious"]
    lower_name = file_path.name.lower()
    extension = file_path.suffix.lower()

    double_targets = "|".join(re.escape(ext.lstrip(".")) for ext in suspicious_cfg["double_extension_targets"])
    if double_targets and re.search(rf"\.\w+\.({double_targets})$", lower_name):
        return True

    if extension in set(suspicious_cfg["risky_extensions"]):
        if any(word in lower_name for word in suspicious_cfg["bait_words"]):
            return True

    return False


def match_custom_category(file_path: Path) -> tuple[str, str] | None:
    categories = CONFIG.get("custom_categories", {})
    if not categories:
        return None

    extension = file_path.suffix.lower()
    lower_name = file_path.name.lower()
    tokens = tokenize_filename(file_path)

    for category, data in categories.items():
        extensions = set(data.get("extensions", []))
        keywords = data.get("keywords", [])

        if extension and extension in extensions:
            return category, f"custom extension {extension}"

        for keyword in keywords:
            if " " in keyword:
                if keyword in lower_name:
                    return category, f"custom keyword {keyword}"
            elif keyword in tokens or keyword in lower_name:
                return category, f"custom keyword {keyword}"

    return None


def decide(file_path: Path, hashes: dict) -> tuple[str, str]:
    digest = md5(file_path)
    if digest and digest in hashes:
        return CONFIG["folders"]["duplicates"], "duplicate hash"
    if digest:
        hashes[digest] = file_path

    if looks_suspicious(file_path):
        return CONFIG["suspicious"]["folder"], "suspicious filename"

    custom_match = match_custom_category(file_path)
    if custom_match:
        return custom_match

    if is_game(file_path):
        return CONFIG["folders"]["games"], "game keyword"

    extension = file_path.suffix.lower()
    if extension in CONFIG["rules"]:
        return CONFIG["rules"][extension], "extension rule"

    if entropy(file_path) > CONFIG["suspicious"]["entropy_threshold"]:
        return CONFIG["suspicious"]["folder"], "high entropy"

    return CONFIG["folders"]["other"], "unknown type"


def list_files(path: Path):
    files = []
    for entry in path.iterdir():
        try:
            if not entry.is_file():
                continue
            if CONFIG.get("skip_hidden_files", True) and entry.name.startswith("."):
                continue
            if CONFIG.get("skip_symlinks", True) and entry.is_symlink():
                continue
            files.append(entry)
        except OSError as exc:
            logging.warning("Skipping file during enumeration: %s (%s)", entry, exc)
    return sorted(files, key=lambda item: item.name.lower())


def write_manifest(run_type: str, root_path: Path, manifest: list[tuple[str, str]], stats: dict) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = MANIFEST_DIR / f"{run_type}_{timestamp}.json"
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "path": str(root_path),
        "manifest": [{"source": source, "destination": dest} for source, dest in manifest],
        "stats": stats,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return manifest_path


def scan_preview(path: Path, callback):
    try:
        files = list_files(path)
    except PermissionError:
        callback(("error", "Permission denied. Choose another folder or run with proper access."))
        return [], 1
    except Exception as exc:
        callback(("error", f"Could not read folder: {exc}"))
        return [], 1

    if len(files) > CONFIG.get("max_files", 2000):
        callback(("error", f"Too many files ({len(files)}). Increase max_files in config."))
        return [], 1

    hashes = {}
    preview = []
    errors = 0
    total = len(files)
    if total == 0:
        callback(("progress", 1.0))
        return [], 0

    for index, file_path in enumerate(files):
        try:
            size_mb = file_path.stat().st_size / (1024 * 1024)
            folder, reason = decide(file_path, hashes)
            preview.append((file_path.name, folder, size_mb, reason))
        except Exception as exc:
            errors += 1
            callback(("error", f"Skipped {file_path.name}: {exc}"))
            logging.exception("Preview failed for %s", file_path)
        callback(("progress", (index + 1) / total))

    return preview, errors


def apply_sort(path: Path, callback):
    stats = {"moved": 0, "scanned": 0, "errors": 0, "skipped": 0}
    manifest = []
    hashes = {}

    try:
        files = list_files(path)
    except Exception as exc:
        callback(("error", f"Could not read directory: {exc}"))
        return stats, manifest, None

    if len(files) > CONFIG.get("max_files", 2000):
        callback(("error", f"Too many files ({len(files)}). Increase max_files in config."))
        return stats, manifest, None

    total = len(files)
    if total == 0:
        callback(("progress", 1.0))
        return stats, manifest, None

    for index, file_path in enumerate(files):
        stats["scanned"] += 1
        try:
            folder, reason = decide(file_path, hashes)
            target_dir = path / folder
            target_dir.mkdir(parents=True, exist_ok=True)
            destination = get_safe_dest(target_dir, file_path.name)

            if destination.resolve() == file_path.resolve():
                stats["skipped"] += 1
                callback(("log", f"Skipped: {file_path.name} (already in place)"))
                callback(("progress", (index + 1) / total))
                continue

            manifest.append((str(file_path), str(destination)))
            shutil.move(str(file_path), str(destination))
            stats["moved"] += 1
            callback(("log", f"Moved: {file_path.name} -> {folder} ({reason})"))
        except Exception as exc:
            stats["errors"] += 1
            callback(("log", f"Failed: {file_path.name} -> {exc}"))
            logging.exception("Sort failed for %s", file_path)

        callback(("progress", (index + 1) / total))

    manifest_path = write_manifest("apply", path, manifest, stats)
    callback(("log", f"Manifest saved: {manifest_path}"))
    return stats, manifest, manifest_path


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Desktop Organizer v5")
        self.root.geometry("950x700")
        self.root.configure(bg="#1e1e1e")

        self.path_var = tk.StringVar(value=CONFIG["default_path"])
        self.undo_stack = deque(maxlen=10)
        self.redo_stack = deque(maxlen=10)
        self.queue = Queue()
        self.is_running = False

        self._build_ui()
        self.root.after(100, self._process_queue)

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=6, font=("Segoe UI", 9))
        style.configure(
            "Header.TLabel",
            background="#1e1e1e",
            foreground="#ffffff",
            font=("Segoe UI", 16, "bold"),
        )

        ttk.Label(self.root, text="Desktop Organizer v5", style="Header.TLabel").pack(pady=(15, 5))

        path_frame = tk.Frame(self.root, bg="#1e1e1e")
        path_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(path_frame, text="Path:", fg="white", bg="#1e1e1e").pack(side="left")
        tk.Entry(path_frame, textvariable=self.path_var, width=70).pack(side="left", padx=5, fill="x", expand=True)
        ttk.Button(path_frame, text="Browse", command=self._browse).pack(side="left")

        button_frame = tk.Frame(self.root, bg="#1e1e1e")
        button_frame.pack(fill="x", padx=20, pady=10)
        self.btn_preview = ttk.Button(button_frame, text="Preview", command=self._preview)
        self.btn_preview.pack(side="left", padx=2)
        self.btn_apply = ttk.Button(button_frame, text="Apply Sort", command=self._apply)
        self.btn_apply.pack(side="left", padx=2)
        ttk.Button(button_frame, text="Undo", command=self._undo).pack(side="left", padx=2)
        ttk.Button(button_frame, text="Redo", command=self._redo).pack(side="left", padx=2)
        ttk.Button(button_frame, text="Screenshot", command=self._screenshot_delayed).pack(side="left", padx=2)
        ttk.Button(button_frame, text="Add Category", command=self._add_category).pack(side="left", padx=2)
        ttk.Button(button_frame, text="Save Config", command=self._save_config).pack(side="right", padx=2)

        self.progress = ttk.Progressbar(self.root, mode="determinate", length=800)
        self.progress.pack(fill="x", padx=20, pady=5)

        self.status = tk.Label(self.root, text="Ready", fg="#aaaaaa", bg="#1e1e1e", anchor="w")
        self.status.pack(fill="x", padx=20)

        split = tk.Frame(self.root, bg="#1e1e1e")
        split.pack(fill="both", expand=True, padx=20, pady=10)

        left = tk.Frame(split, bg="#1e1e1e")
        left.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.log = scrolledtext.ScrolledText(left, bg="#111", fg="#ddd", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

        right = tk.Frame(split, bg="#1e1e1e")
        right.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self.img_label = tk.Label(right, bg="#222", text="Screenshot will appear here", fg="#666")
        self.img_label.pack(fill="both", expand=True)

    def _browse(self):
        chosen = filedialog.askdirectory(initialdir=self.path_var.get())
        if chosen:
            self.path_var.set(chosen)

    def _log(self, message):
        self.log.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {message}\n")
        self.log.see(tk.END)

    def _set_status(self, text):
        self.status.config(text=text)

    def _set_running(self, running: bool):
        self.is_running = running
        state = "disabled" if running else "normal"
        self.btn_preview.config(state=state)
        self.btn_apply.config(state=state)

    def _validate_path(self):
        path = Path(self.path_var.get()).expanduser()
        if not path.exists() or not path.is_dir():
            raise ValueError("Please choose a valid directory.")
        return path

    def _process_queue(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self.progress["value"] = max(0.0, min(100.0, payload * 100))
                    self._set_status(f"Processing: {payload * 100:.1f}%")
                elif kind == "done":
                    self._set_running(False)
                    self.progress["value"] = 100
                    done_status = "Done"
                    if isinstance(payload, dict) and payload.get("errors", 0) > 0:
                        done_status = "Done with errors"
                    self._set_status(done_status)
                    if isinstance(payload, dict):
                        manifest_path = payload.get("manifest_path")
                        if manifest_path:
                            self._log(f"Manifest: {manifest_path}")
                        self._log(
                            f"Moved: {payload['moved']} | Scanned: {payload['scanned']} | "
                            f"Skipped: {payload.get('skipped', 0)} | Errors: {payload['errors']}"
                        )
                elif kind == "error":
                    self._set_running(False)
                    self._log(f"Error: {payload}")
                    self._set_status("Error")
        except Empty:
            pass
        finally:
            self.root.after(50, self._process_queue)

    def _preview(self):
        if self.is_running:
            return

        try:
            path = self._validate_path()
        except ValueError as exc:
            messagebox.showerror("Invalid Path", str(exc))
            return

        self._set_running(True)
        self.log.delete("1.0", tk.END)
        self.progress["value"] = 0

        def worker():
            try:
                result, errors = scan_preview(path, lambda event: self.queue.put(event))
                self.queue.put(("log", "=== PREVIEW ==="))
                for name, folder, size_mb, reason in result:
                    self.queue.put(("log", f"{name} -> {folder} ({size_mb:.1f}MB) [{reason}]"))
                self.queue.put(("done", {"moved": 0, "scanned": len(result), "skipped": 0, "errors": errors}))
            except Exception as exc:
                logging.exception("Preview worker failed")
                self.queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self):
        if self.is_running:
            return

        try:
            path = self._validate_path()
        except ValueError as exc:
            messagebox.showerror("Invalid Path", str(exc))
            return

        if not messagebox.askyesno("Confirm", "Move files now? This action can be undone."):
            return

        self._set_running(True)
        self.log.delete("1.0", tk.END)
        self.progress["value"] = 0

        def worker():
            try:
                stats, manifest, manifest_path = apply_sort(path, lambda event: self.queue.put(event))
                if manifest:
                    self.undo_stack.append(manifest)
                    self.redo_stack.clear()
                stats["manifest_path"] = str(manifest_path) if manifest_path else None
                self.queue.put(("done", stats))
            except Exception as exc:
                logging.exception("Apply worker failed")
                self.queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _undo(self):
        if self.is_running:
            return
        if not self.undo_stack:
            self._log("Nothing to undo")
            return

        manifest = self.undo_stack.pop()
        self.redo_stack.append(manifest)
        self._log("Undoing...")

        restored = 0
        errors = 0
        for src, dest in reversed(manifest):
            source, destination = Path(src), Path(dest)
            if not destination.exists():
                continue
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                restore_target = get_safe_dest(source.parent, source.name) if source.exists() else source
                shutil.move(str(destination), str(restore_target))
                restored += 1
            except Exception as exc:
                errors += 1
                self._log(f"Undo failed for {destination.name}: {exc}")
                logging.exception("Undo move failed for %s", destination)

        self._log(f"Undo complete. Restored={restored}, Errors={errors}")

    def _redo(self):
        if self.is_running:
            return
        if not self.redo_stack:
            self._log("Nothing to redo")
            return

        manifest = self.redo_stack.pop()
        self.undo_stack.append(manifest)
        self._log("Redoing...")

        reapplied = 0
        errors = 0
        for src, dest in manifest:
            source, destination = Path(src), Path(dest)
            if not source.exists():
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                final_dest = get_safe_dest(destination.parent, destination.name)
                shutil.move(str(source), str(final_dest))
                reapplied += 1
            except Exception as exc:
                errors += 1
                self._log(f"Redo failed for {source.name}: {exc}")
                logging.exception("Redo move failed for %s", source)

        self._log(f"Redo complete. Reapplied={reapplied}, Errors={errors}")

    def _screenshot_delayed(self):
        if self.is_running:
            return
        self._log("Capture in 3 seconds...")

        def countdown(remaining):
            if remaining > 0:
                self._set_status(f"Screenshot in {remaining}...")
                self.root.after(1000, countdown, remaining - 1)
            else:
                self._take_screenshot()

        countdown(3)

    def _take_screenshot(self):
        try:
            image = ImageGrab.grab()
            image.thumbnail((600, 400))
            tk_image = ImageTk.PhotoImage(image)
            self.img_label.config(image=tk_image, text="")
            self.img_label.image = tk_image
            self._log("Screenshot captured")
            self._set_status("Ready")
        except Exception as exc:
            self._log(f"Screenshot failed: {exc}")
            logging.exception("Screenshot failed")

    def _save_config(self):
        try:
            persist_config()
            self._log(f"Config saved to {CONFIG_PATH}")
        except Exception as exc:
            self._log(f"Config save failed: {exc}")
            logging.exception("Config save failed")

    def _add_category(self):
        if self.is_running:
            return

        category = simpledialog.askstring(
            "Add Category",
            "Category name (folder name):",
            parent=self.root,
        )
        if category is None:
            return
        category = category.strip()
        if not category:
            messagebox.showerror("Invalid Category", "Category name cannot be empty.")
            return
        if any(char in category for char in "\\/:*?\"<>|"):
            messagebox.showerror("Invalid Category", "Category name contains invalid folder characters.")
            return

        extensions_text = simpledialog.askstring(
            "Add Category",
            "Extensions (comma-separated, optional)\nExample: psd, ai, blend",
            parent=self.root,
        )
        if extensions_text is None:
            return

        keywords_text = simpledialog.askstring(
            "Add Category",
            "Keywords (comma-separated, optional)\nExample: invoice, contract, design",
            parent=self.root,
        )
        if keywords_text is None:
            return

        extensions = []
        for part in extensions_text.split(","):
            cleaned = part.strip().lower()
            if not cleaned:
                continue
            if not cleaned.startswith("."):
                cleaned = f".{cleaned}"
            extensions.append(cleaned)

        keywords = [part.strip().lower() for part in keywords_text.split(",") if part.strip()]

        if not extensions and not keywords:
            messagebox.showerror("Invalid Category", "Add at least one extension or keyword.")
            return

        custom_categories = CONFIG.setdefault("custom_categories", {})
        current = custom_categories.setdefault(category, {"extensions": [], "keywords": []})

        current["extensions"] = sorted(set(current.get("extensions", []) + extensions))
        current["keywords"] = sorted(set(current.get("keywords", []) + keywords))

        CONFIG["custom_categories"] = normalize_config({"custom_categories": custom_categories}).get(
            "custom_categories", custom_categories
        )

        try:
            persist_config()
            self._log(
                f"Added/updated category '{category}' "
                f"(extensions={len(current['extensions'])}, keywords={len(current['keywords'])})"
            )
            messagebox.showinfo("Category Saved", f"Category '{category}' is ready for sorting.")
        except Exception as exc:
            self._log(f"Failed to save category: {exc}")
            logging.exception("Failed to save custom category")
            messagebox.showerror("Save Failed", f"Could not save category: {exc}")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
