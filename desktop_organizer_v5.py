"""Desktop Organizer v5 - Tkinter front end.

All sorting logic lives in organizer_core.py; this file only builds the UI.
"""
import logging
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from queue import Empty, Queue
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

import organizer_core as core


class App:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        self.root.title("Desktop Organizer v5")
        self.root.geometry("950x700")
        self.root.configure(bg="#1e1e1e")

        self.path_var = tk.StringVar(value=self.config["default_path"])
        # History is rebuilt from saved manifests, so undo still works after a restart.
        undo, redo = core.load_history(core.HISTORY_LIMIT)
        self.undo_stack = deque(undo, maxlen=core.HISTORY_LIMIT)
        self.redo_stack = deque(redo, maxlen=core.HISTORY_LIMIT)
        self.queue = Queue()
        self.is_running = False

        self._build_ui()
        if self.undo_stack:
            self._log(f"{len(self.undo_stack)} earlier sort(s) can be undone.")
        self.root.after(100, self._process_queue)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=6, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background="#1e1e1e", foreground="#ffffff", font=("Segoe UI", 16, "bold"))

        ttk.Label(self.root, text="Desktop Organizer v5", style="Header.TLabel").pack(pady=(15, 5))

        path_frame = tk.Frame(self.root, bg="#1e1e1e")
        path_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(path_frame, text="Path:", fg="white", bg="#1e1e1e").pack(side="left")
        tk.Entry(path_frame, textvariable=self.path_var, width=70).pack(side="left", padx=5, fill="x", expand=True)
        ttk.Button(path_frame, text="Browse", command=self._browse).pack(side="left")

        button_frame = tk.Frame(self.root, bg="#1e1e1e")
        button_frame.pack(fill="x", padx=20, pady=10)
        self.action_buttons = []
        for text, command in (
            ("Preview", self._preview),
            ("Apply Sort", self._apply),
            ("Undo", self._undo),
            ("Redo", self._redo),
            ("Add Category", self._add_category),
        ):
            button = ttk.Button(button_frame, text=text, command=command)
            button.pack(side="left", padx=2)
            self.action_buttons.append(button)
        ttk.Button(button_frame, text="Screenshot", command=self._screenshot_delayed).pack(side="left", padx=2)
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
        for button in self.action_buttons:
            button.config(state=state)

    def _validate_path(self):
        path = Path(self.path_var.get()).expanduser()
        if not path.exists() or not path.is_dir():
            raise ValueError("Please choose a valid directory.")
        return path

    # ------------------------------------------------------------------ worker plumbing

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
                    self._on_sort_done(payload)
                elif kind == "history_done":
                    self._on_history_done(*payload)
                elif kind == "error":
                    self._set_running(False)
                    self._log(f"Error: {payload}")
                    self._set_status("Error")
        except Empty:
            pass
        finally:
            self.root.after(50, self._process_queue)

    def _on_sort_done(self, payload):
        self._set_running(False)
        self.progress["value"] = 100
        self._set_status("Done with errors" if payload.get("errors", 0) > 0 else "Done")
        manifest_path = payload.get("manifest_path")
        if manifest_path:
            # Stacks are only touched on the UI thread.
            self.undo_stack.append(Path(manifest_path))
            self.redo_stack.clear()
        self._log(
            f"Moved: {payload['moved']} | Scanned: {payload['scanned']} | "
            f"Skipped: {payload.get('skipped', 0)} | Errors: {payload['errors']}"
        )

    def _on_history_done(self, action, manifest_path, result):
        self._set_running(False)
        self.progress["value"] = 100
        label = "Undo" if action == "undo" else "Redo"
        self._log(f"{label} complete. Moved={result['done']}, Skipped={result['skipped']}, Errors={result['errors']}")
        if result["errors"] == 0:
            source, target = (self.undo_stack, self.redo_stack) if action == "undo" else (self.redo_stack, self.undo_stack)
            if source and source[-1] == manifest_path:
                source.pop()
            target.append(manifest_path)
            self._set_status("Done")
        else:
            self._set_status(f"{label} finished with errors - you can try again")

    def _start_worker(self, target):
        self._set_running(True)
        self.log.delete("1.0", tk.END)
        self.progress["value"] = 0

        def runner():
            try:
                target()
            except core.SortError as exc:
                self.queue.put(("error", str(exc)))
            except Exception as exc:
                logging.exception("Worker failed")
                self.queue.put(("error", str(exc)))

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------ actions

    def _preview(self):
        if self.is_running:
            return
        try:
            path = self._validate_path()
        except ValueError as exc:
            messagebox.showerror("Invalid Path", str(exc))
            return

        def work():
            result, errors = core.scan_preview(path, self.config, self.queue.put)
            self.queue.put(("log", "=== PREVIEW ==="))
            for name, folder, size_mb, reason in result:
                self.queue.put(("log", f"{name} -> {folder} ({size_mb:.1f}MB) [{reason}]"))
            self.queue.put(("done", {"moved": 0, "scanned": len(result), "skipped": 0, "errors": errors}))

        self._start_worker(work)

    def _apply(self):
        if self.is_running:
            return
        try:
            path = self._validate_path()
        except ValueError as exc:
            messagebox.showerror("Invalid Path", str(exc))
            return
        if not messagebox.askyesno("Confirm", f"Move files in:\n{path}\n\nThis can be undone."):
            return

        def work():
            stats, manifest_path = core.apply_sort(path, self.config, self.queue.put)
            stats["manifest_path"] = str(manifest_path) if manifest_path else None
            self.queue.put(("done", stats))

        self._start_worker(work)

    def _history_action(self, action):
        if self.is_running:
            return
        stack = self.undo_stack if action == "undo" else self.redo_stack
        if not stack:
            self._log(f"Nothing to {action}")
            return
        manifest_path = stack[-1]
        function = core.undo_manifest if action == "undo" else core.redo_manifest

        def work():
            result = function(manifest_path, self.queue.put)
            self.queue.put(("history_done", (action, manifest_path, result)))

        self._start_worker(work)

    def _undo(self):
        self._history_action("undo")

    def _redo(self):
        self._history_action("redo")

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
            from PIL import ImageGrab, ImageTk  # optional: only needed for this feature
        except ImportError:
            self._log("Screenshot needs Pillow: pip install Pillow")
            self._set_status("Ready")
            return
        try:
            image = ImageGrab.grab()
            image.thumbnail((600, 400))
            tk_image = ImageTk.PhotoImage(image)
            self.img_label.config(image=tk_image, text="")
            self.img_label.image = tk_image  # keep a reference so Tk doesn't discard it
            self._log("Screenshot captured (shown here only, not saved)")
            self._set_status("Ready")
        except Exception as exc:
            self._log(f"Screenshot failed: {exc}")
            logging.exception("Screenshot failed")

    def _save_config(self):
        try:
            core.persist_config(self.config)
            self._log(f"Config saved to {core.CONFIG_PATH}")
        except Exception as exc:
            self._log(f"Config save failed: {exc}")
            logging.exception("Config save failed")

    def _add_category(self):
        if self.is_running:
            return

        category = simpledialog.askstring("Add Category", "Category name (folder name):", parent=self.root)
        if category is None:
            return
        category = category.strip()
        if not category or core.sanitize_folder_name(category, "") != category:
            messagebox.showerror(
                "Invalid Category",
                "Category name must be a plain folder name (no slashes or special characters).",
            )
            return

        extensions_text = simpledialog.askstring(
            "Add Category", "Extensions (comma-separated, optional)\nExample: psd, ai, blend", parent=self.root
        )
        if extensions_text is None:
            return
        keywords_text = simpledialog.askstring(
            "Add Category", "Keywords (comma-separated, optional)\nExample: invoice, contract, design", parent=self.root
        )
        if keywords_text is None:
            return

        extensions = core._ext_list(extensions_text.split(","))
        keywords = core._str_list(keywords_text.split(","))
        if not extensions and not keywords:
            messagebox.showerror("Invalid Category", "Add at least one extension or keyword.")
            return

        custom = self.config.setdefault("custom_categories", {})
        current = custom.setdefault(category, {"extensions": [], "keywords": []})
        current["extensions"] = sorted(set(current.get("extensions", []) + extensions))
        current["keywords"] = sorted(set(current.get("keywords", []) + keywords))

        try:
            core.persist_config(self.config)
            self._log(
                f"Added/updated category '{category}' "
                f"(extensions={len(current['extensions'])}, keywords={len(current['keywords'])})"
            )
            messagebox.showinfo("Category Saved", f"Category '{category}' is ready for sorting.")
        except Exception as exc:
            self._log(f"Failed to save category: {exc}")
            logging.exception("Failed to save custom category")
            messagebox.showerror("Save Failed", f"Could not save category: {exc}")


def main():
    core.setup_logging()
    config = core.load_config()
    root = tk.Tk()
    App(root, config)
    root.mainloop()


if __name__ == "__main__":
    main()
