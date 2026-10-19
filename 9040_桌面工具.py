"""只读维护数据演示的 Windows/Tkinter 启动界面。"""
from pathlib import Path
from datetime import datetime
import importlib.util
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("maintenance9040", BASE / "9040_维护分析.py")
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Maintenance Analytics | 5 Read-only Demos")
        self.geometry("950x660")
        self.minsize(800, 580)
        self.configure(bg="#f3f6fa")
        self.events = queue.Queue()
        self.report = None
        self.result = None
        self.input_path = tk.StringVar(value=str(BASE / "9040_示例输入"))
        self.output_path = tk.StringVar(value=str(BASE / ("9040_运行结果_" + datetime.now().strftime("%Y%m%d_%H%M%S"))))
        self.as_of = tk.StringVar(value="2026-10-19")
        self.status = tk.StringVar(value="Choose local CSV inputs, then run all five analyses.")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fa")
        style.configure("TLabel", background="#f3f6fa", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"), foreground="#172d45")
        style.configure("TButton", padding=8, font=("Segoe UI", 10))
        frame = ttk.Frame(self, padding=26)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Maintenance analytics", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Independent portfolio demo · Synthetic data · No SAP connection or write-back").pack(anchor="w", pady=(6, 20))
        fields = ttk.Frame(frame)
        fields.pack(fill="x")
        fields.columnconfigure(1, weight=1)
        for row, label, variable in [(0, "Input folder", self.input_path), (1, "New output folder", self.output_path), (2, "As-of date (ISO)", self.as_of)]:
            ttk.Label(fields, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=7)
            ttk.Entry(fields, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=7)
        ttk.Button(fields, text="Browse", command=self.choose_input).grid(row=0, column=2, padx=(10, 0))
        ttk.Button(fields, text="Choose parent", command=self.choose_output).grid(row=1, column=2, padx=(10, 0))
        ttk.Label(frame, text="Five demonstrations", font=("Segoe UI", 13, "bold")).pack(anchor="w", pady=(20, 10))
        self.table = ttk.Treeview(frame, columns=("idea", "result"), show="headings", height=6)
        self.table.heading("idea", text="Read-only analysis")
        self.table.heading("result", text="Result rows")
        self.table.column("idea", width=600)
        self.table.column("result", width=100, anchor="center")
        self.table.pack(fill="both", expand=True)
        for i, name in enumerate(["Documentation quality scoring", "Seven-day maintenance report", "Equipment & plan master-data checks", "Mobile vs desktop entry-time comparison", "Due-date & measurement alerts"], 1):
            self.table.insert("", "end", iid=str(i), values=(name, "—"))
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(18, 10))
        self.run_button = ttk.Button(controls, text="Run all five analyses", command=self.start)
        self.run_button.pack(side="left")
        self.open_button = ttk.Button(controls, text="Open offline report", command=self.open_report, state="disabled")
        self.open_button.pack(side="left", padx=12)
        ttk.Label(frame, textvariable=self.status, wraplength=840).pack(anchor="w", pady=8)
        ttk.Label(frame, text="Source files remain unchanged. Results are written to a new empty folder.", foreground="#4e657b").pack(anchor="w")
        self.after(80, self.poll)

    def choose_input(self):
        folder = filedialog.askdirectory(title="Choose local demo CSV folder")
        if folder:
            self.input_path.set(folder)

    def choose_output(self):
        folder = filedialog.askdirectory(title="Choose parent for a NEW output folder")
        if folder:
            self.output_path.set(str(Path(folder) / ("9040_运行结果_" + datetime.now().strftime("%Y%m%d_%H%M%S"))))

    def start(self):
        self.run_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.report = None
        self.result = None
        self.status.set("Validating CSV files and running read-only analyses…")
        args = (Path(self.input_path.get()), Path(self.output_path.get()), self.as_of.get().strip())
        def worker():
            try:
                self.events.put((True, engine.run_all(*args)))
            except Exception as exc:
                self.events.put((False, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            ok, value = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self.run_button.configure(state="normal")
            if ok:
                self.result = value
                for number, idea in value["ideas"].items():
                    current = self.table.item(number, "values")
                    self.table.item(number, values=(current[0], len(idea["rows"])))
                self.report = Path(self.output_path.get()) / "9040_运行报告.html"
                self.open_button.configure(state="normal")
                self.status.set("Complete. Five CSV reports, JSON audit data and the offline HTML report are ready.")
            else:
                self.status.set("Please correct the input: " + value)
                messagebox.showerror("Analysis could not complete", value, parent=self)
        self.after(80, self.poll)

    def open_report(self):
        if self.report and self.report.is_file():
            webbrowser.open(self.report.resolve().as_uri())


if __name__ == "__main__":
    App().mainloop()
