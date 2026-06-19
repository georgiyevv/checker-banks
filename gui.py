import os
import sys
import glob
import time
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from main import check_receipt, build_report, DedupStore


COLORS = {
    "bg": "#0f1115",
    "panel": "#171a21",
    "card": "#1e222b",
    "accent": "#3b82f6",
    "text": "#e6e8ec",
    "muted": "#8b919c",
    "fake": "#ef4444",
    "valid": "#22c55e",
    "susp": "#f59e0b",
}

VERDICT_VIEW = {
    "REJECTED": ("ФЕЙК", COLORS["fake"], "✖"),
    "SUSPICIOUS": ("ПОДОЗРИТЕЛЬНО", COLORS["susp"], "▲"),
    "CLEAN": ("ВАЛИД", COLORS["valid"], "✔"),
}


class CheckerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Сhecker App")
        self.geometry("1040x640")
        self.minsize(880, 540)
        self.configure(bg=COLORS["bg"])

        self.folder = None
        self.results = []

        self._build_style()
        self._build_header()
        self._build_body()
        self._build_footer()

    # ---------- стиль ----------

    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("TFrame", background=COLORS["bg"])
        st.configure("Card.TFrame", background=COLORS["card"])
        st.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"],
                     font=("Helvetica", 12))
        st.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"],
                     font=("Helvetica", 11))
        st.configure("Title.TLabel", background=COLORS["bg"], foreground=COLORS["text"],
                     font=("Helvetica", 20, "bold"))
        st.configure("Accent.TButton", font=("Helvetica", 12, "bold"),
                     padding=10, background=COLORS["accent"], foreground="white",
                     borderwidth=0)
        st.map("Accent.TButton",
               background=[("active", "#2f6fe0"), ("disabled", "#33363f")])
        st.configure("Ghost.TButton", font=("Helvetica", 11), padding=8,
                     background=COLORS["panel"], foreground=COLORS["text"],
                     borderwidth=0)
        st.map("Ghost.TButton", background=[("active", "#262a33")])

        st.configure("Treeview",
                     background=COLORS["card"], fieldbackground=COLORS["card"],
                     foreground=COLORS["text"], rowheight=30, borderwidth=0,
                     relief="flat", bordercolor=COLORS["card"],
                     lightcolor=COLORS["card"], darkcolor=COLORS["card"],
                     font=("Helvetica", 11))
        st.configure("Treeview.Heading",
                     background=COLORS["panel"], foreground=COLORS["muted"],
                     font=("Helvetica", 11, "bold"), borderwidth=0,
                     relief="flat")
        st.map("Treeview", background=[("selected", "#2a3040")])
        st.map("Treeview.Heading", relief=[("active", "flat")])

        st.layout("Dark.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {
                "children": [("Vertical.Scrollbar.thumb",
                              {"expand": "1", "sticky": "nswe"})],
                "sticky": "ns"}),
        ])
        st.configure("Dark.Vertical.TScrollbar",
                     troughcolor=COLORS["bg"], background=COLORS["bg"],
                     borderwidth=0, relief="flat", arrowcolor=COLORS["bg"],
                     bordercolor=COLORS["bg"], lightcolor=COLORS["bg"],
                     darkcolor=COLORS["bg"], gripcount=0)
        st.map("Dark.Vertical.TScrollbar",
               background=[("active", "#2a3040"), ("pressed", "#323a4a"),
                          ("!active", "#23272f")])

        st.configure("Dark.Horizontal.TProgressbar",
                     troughcolor="#23272f", background=COLORS["accent"],
                     borderwidth=0, thickness=8,
                     bordercolor="#23272f", lightcolor=COLORS["accent"],
                     darkcolor=COLORS["accent"])

    # ---------- шапка ----------

    def _build_header(self):
        head = ttk.Frame(self, style="TFrame")
        head.pack(fill="x", padx=20, pady=(18, 8))

        bar = ttk.Frame(self, style="TFrame")
        bar.pack(fill="x", padx=20, pady=(6, 4))

        self.btn_folder = ttk.Button(bar, text="📁  Выбрать папку с чеками",
                                     style="Accent.TButton", command=self.choose_folder)
        self.btn_folder.pack(side="left")

        self.btn_report = ttk.Button(bar, text="Сохранить отчёт",
                                     style="Ghost.TButton", command=self.save_report,
                                     state="disabled")
        self.btn_report.pack(side="right")

    # ---------- тело ----------

    def _build_body(self):
        body = ttk.Frame(self, style="TFrame")
        body.pack(fill="both", expand=True, padx=20, pady=10)

        left = ttk.Frame(body, style="TFrame")
        left.pack(side="left", fill="both", expand=True)
        self.left = left

        cols = ("verdict", "file")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("verdict", text="СТАТУС")
        self.tree.heading("file", text="ФАЙЛ")
        self.tree.column("verdict", width=150, anchor="w")
        self.tree.column("file", width=510, anchor="w")
        self.tree.tag_configure("REJECTED", foreground=COLORS["fake"])
        self.tree.tag_configure("SUSPICIOUS", foreground=COLORS["susp"])
        self.tree.tag_configure("CLEAN", foreground=COLORS["valid"])
        self.tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview,
                           style="Dark.Vertical.TScrollbar")
        sb.pack(side="right", fill="y", padx=(4, 0))
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        self.loading = tk.Frame(left, bg=COLORS["card"])
        inner = tk.Frame(self.loading, bg=COLORS["card"])
        inner.place(relx=0.5, rely=0.5, anchor="center")
        self.loading_lbl = tk.Label(inner, text="Проверка…", bg=COLORS["card"],
                                    fg=COLORS["text"], font=("Helvetica", 13))
        self.loading_lbl.pack(pady=(0, 12))
        self.bar = ttk.Progressbar(inner, mode="determinate", length=300,
                                   style="Dark.Horizontal.TProgressbar")
        self.bar.pack()

        right = tk.Frame(body, bg=COLORS["card"], width=320)
        right.pack(side="right", fill="y", padx=(14, 0))
        right.pack_propagate(False)

        tk.Label(right, text="ДЕТАЛИ", bg=COLORS["card"], fg=COLORS["muted"],
                 font=("Helvetica", 10, "bold")).pack(anchor="w", padx=16, pady=(16, 6))
        self.detail = tk.Text(right, bg=COLORS["card"], fg=COLORS["text"],
                              wrap="word", relief="flat", font=("Helvetica", 11),
                              padx=16, pady=4, highlightthickness=0)
        self.detail.pack(fill="both", expand=True)
        self.detail.configure(state="disabled")
        self.detail.tag_configure("h", font=("Helvetica", 13, "bold"))
        self.detail.tag_configure("fail", foreground=COLORS["fake"])
        self.detail.tag_configure("flag", foreground=COLORS["susp"])
        self.detail.tag_configure("ok", foreground=COLORS["valid"])
        self.detail.tag_configure("muted", foreground=COLORS["muted"])

    # ---------- подвал ----------

    def _build_footer(self):
        self.footer = tk.Frame(self, bg=COLORS["panel"], height=46)
        self.footer.pack(fill="x", side="bottom")
        self.footer.pack_propagate(False)
        self.summary = tk.Label(self.footer, text="Готово к проверке",
                                bg=COLORS["panel"], fg=COLORS["muted"],
                                font=("Helvetica", 11))
        self.summary.pack(side="left", padx=20)

    # ---------- действия ----------

    def _pick_folders(self):
        """Несколько папок за раз. На macOS — нативный диалог с Cmd-кликом."""
        if sys.platform == "darwin":
            script = (
                'set theFolders to choose folder with prompt '
                '"Выберите папки (Cmd-клик — несколько)" with multiple selections allowed\n'
                'set out to ""\n'
                'repeat with f in theFolders\n'
                'set out to out & POSIX path of f & linefeed\n'
                'end repeat\n'
                'return out'
            )
            try:
                res = subprocess.run(["osascript", "-e", script],
                                     capture_output=True, text=True)
                if res.returncode == 0:
                    return [ln for ln in res.stdout.splitlines() if ln.strip()]
                return []
            except Exception:
                pass
        # Фолбэк (не-macOS): добавление папок по одной до «Отмены»
        folders = []
        while True:
            f = filedialog.askdirectory(
                title=f"Добавить папку ({len(folders)} выбрано), «Отмена» — закончить")
            if not f:
                break
            if f not in folders:
                folders.append(f)
        return folders

    def choose_folder(self):
        folders = self._pick_folders()
        if not folders:
            return
        self.folder = folders[0]

        paths = []
        for f in folders:
            paths += glob.glob(os.path.join(f, "*.pdf"))
            paths += glob.glob(os.path.join(f, "*.PDF"))
        paths = sorted(set(paths))
        if not paths:
            messagebox.showinfo("Пусто", "В выбранных папках нет PDF-файлов.")
            return
        self._start_loading(paths)

    def _start_loading(self, paths):
        self.btn_folder.config(state="disabled")
        self.btn_report.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self._set_detail_placeholder()
        self.summary.config(text="Проверка…")

        self.bar.config(maximum=len(paths), value=0)
        self.loading_lbl.config(text=f"Проверка 0 из {len(paths)}")
        self.loading.place(relx=0, rely=0, relwidth=1, relheight=1)

        def worker():
            results = []
            dedup_path = os.path.join(self.folder, "_run_dedup.json")
            if os.path.exists(dedup_path):
                os.remove(dedup_path)
            dedup = DedupStore(path=dedup_path)
            try:
                for i, path in enumerate(paths, 1):
                    try:
                        res = check_receipt(path, dedup=dedup)
                    except Exception as exc:
                        res = {"status": "REJECTED",
                               "advice": "Файл не удалось разобрать как PDF.",
                               "hard_fails": [f"ошибка разбора: {exc}"],
                               "soft_flags": [], "fields": {}}
                    dedup.register(path, res.get("fields", {}))
                    results.append((os.path.basename(path), res))
                    self.after(0, self._progress, i, len(paths))
                    time.sleep(0.15)
            except Exception as exc:
                self.after(0, lambda: self._scan_failed(str(exc)))
                return
            finally:
                if os.path.exists(dedup_path):
                    os.remove(dedup_path)
            self.after(0, lambda: self._scan_done(results))

        threading.Thread(target=worker, daemon=True).start()

    def _progress(self, done, total):
        self.bar.config(value=done)
        self.loading_lbl.config(text=f"Проверка {done} из {total}")

    def _scan_failed(self, msg):
        self.loading.place_forget()
        self.btn_folder.config(state="normal")
        messagebox.showerror("Ошибка", msg)
        self.summary.config(text="Ошибка проверки")

    def _scan_done(self, results):
        self.loading.place_forget()
        self.results = results
        self.tree.delete(*self.tree.get_children())

        counts = {"REJECTED": 0, "SUSPICIOUS": 0, "CLEAN": 0}
        for name, res in results:
            status = res["status"]
            counts[status] = counts.get(status, 0) + 1
            label, _, icon = VERDICT_VIEW.get(status, (status, "", "?"))
            self.tree.insert("", "end", values=(f"{icon} {label}", name), tags=(status,))

        self.btn_folder.config(state="normal")
        self.btn_report.config(state="normal" if results else "disabled")
        self.summary.config(
            text=f"Всего: {len(results)}    "
                 f"✖ Фейк: {counts['REJECTED']}    "
                 f"▲ Подозр.: {counts['SUSPICIOUS']}    "
                 f"✔ Валид: {counts['CLEAN']}"
        )
        self._set_detail_placeholder()

    def on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        name, res = self.results[idx]
        f = res.get("fields", {})

        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", name + "\n", "h")

        label, _, icon = VERDICT_VIEW.get(res["status"], (res["status"], "", "?"))
        tag = {"REJECTED": "fail", "SUSPICIOUS": "flag", "CLEAN": "ok"}.get(res["status"], "muted")
        self.detail.insert("end", f"{icon} {label}\n\n", tag)

        rows = [
            ("Сумма", f"{f.get('total')} ₽" if f.get("total") is not None else None),
            ("Время", f.get("datetime_visible")),
            ("Телефон", f.get("phone")),
            ("Банк получателя", f.get("recipient_bank")),
            ("ID операции", f.get("operation_id")),
        ]
        for k, v in rows:
            if v:
                self.detail.insert("end", f"{k}: ", "muted")
                self.detail.insert("end", f"{v}\n")
        self.detail.configure(state="disabled")

    def _set_detail_placeholder(self):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("end", "Выберите чек в списке,\nчтобы увидеть детали.", "muted")
        self.detail.configure(state="disabled")

    def save_report(self):
        if not self.results:
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить отчёт", defaultextension=".txt",
            initialfile="отчет.txt", filetypes=[("Текст", "*.txt")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(build_report(self.results, self.folder or ""))
        messagebox.showinfo("Готово", f"Отчёт сохранён")


if __name__ == "__main__":
    CheckerApp().mainloop()
