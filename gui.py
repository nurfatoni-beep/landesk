"""
GUI LanDesk (tkinter) - satu aplikasi untuk dua peran, seperti RustDesk:
  * Komputer ini            : ID + password, status, client yang sedang terhubung
  * Kontrol komputer lain   : cari komputer di LAN / masukkan ID atau IP, lalu hubungkan
  * Pengaturan
Ditambah ikon system tray (pystray, opsional).
"""
import concurrent.futures
import logging
import os
import queue
import secrets
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import dashboard
import remote
import server
import updater

log = logging.getLogger("landesk.gui")

try:
    import pystray
    from PIL import Image

    HAVE_TRAY = True
except Exception:  # pragma: no cover
    HAVE_TRAY = False

ICON_PATH = server.STATIC_DIR / "icon.png"
FPS_CHOICES = ["10", "15", "24", "30", "45", "60"]
SCALE_CHOICES = {"100%": 1.0, "75%": 0.75, "50%": 0.5}
MAX_RECENT = 12


def display_problem():
    """None kalau GUI bisa dipakai, atau alasan kenapa tidak."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "tidak ada DISPLAY"
    return None


# ----------------------------------------------------------------------------
# Autostart saat login
# ----------------------------------------------------------------------------
def _launch_command():
    if getattr(sys, "frozen", False):
        return server.windows_launch_command() if sys.platform == "win32" else [sys.executable]
    if sys.platform.startswith("linux") and Path("/usr/bin/landesk").exists():
        return ["/usr/bin/landesk"]
    py = sys.executable
    if sys.platform == "win32":
        pyw = Path(py).with_name("pythonw.exe")
        if pyw.exists():
            py = str(pyw)
    return [py, str(Path(server.__file__).resolve())]


def _quote(parts):
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


AUTOSTART_DESKTOP = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "landesk.desktop"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, "LanDesk")
                return True
        except OSError:
            return False
    return AUTOSTART_DESKTOP.exists()


def set_autostart(enable: bool):
    cmd = _quote(_launch_command() + ["--minimized"])
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, "LanDesk", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, "LanDesk")
                except OSError:
                    pass
    else:
        if enable:
            AUTOSTART_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
            AUTOSTART_DESKTOP.write_text(
                "[Desktop Entry]\nType=Application\nName=LanDesk\n"
                f"Exec={cmd}\nIcon=landesk\nX-GNOME-Autostart-enabled=true\n",
                encoding="utf-8",
            )
        elif AUTOSTART_DESKTOP.exists():
            AUTOSTART_DESKTOP.unlink()


def short_ua(ua: str) -> str:
    browser = next((n for k, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"),
                                   ("Chrome/", "Chrome"), ("Safari/", "Safari")) if k in ua), "Browser")
    osname = next((n for k, n in (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                  ("Windows", "Windows"), ("Mac OS", "macOS"), ("Linux", "Linux")) if k in ua), "")
    return f"{browser} / {osname}" if osname else browser


class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__(logging.INFO)
        self.q = q

    def emit(self, record):
        self.q.put(("log", {"msg": record.getMessage(), "level": record.levelno}))


# ----------------------------------------------------------------------------
# Aplikasi
# ----------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk, start_minimized=False, use_dxgi=True):
        self.root = root
        self.use_dxgi = use_dxgi
        self.q = queue.Queue()
        self.server = None
        self.cfg = None
        self.clients = {}
        self.dashboard = None
        self.tray = None
        self.tray_hint_shown = False
        self.quitting = False

        self.conf = server.load_config()
        changed = False
        if not self.conf.get("password"):
            self.conf["password"] = secrets.token_hex(4)
            changed = True
        if not self.conf.get("device_id"):
            server.device_id(self.conf)
            changed = True
        if changed:
            server.save_config(self.conf)

        logging.getLogger().addHandler(QueueLogHandler(self.q))
        self.monitors = server.list_monitors()
        self._build_ui()
        self._init_tray()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.start_server()
        self.root.after(150, self._poll)
        self.root.after(20_000, self._update_tick)  # cek update pertama 20 detik setelah start
        if start_minimized:
            self.root.after(300, self.hide_window)

    # ------------------------------------------------------------- UI
    def _build_ui(self):
        r = self.root
        r.title(f"LanDesk {server.__version__}")
        r.minsize(600, 620)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        r.geometry(f"{min(960, sw - 80)}x{min(740, sh - 120)}")
        try:
            self._icon_img = tk.PhotoImage(file=str(ICON_PATH))
            r.iconphoto(True, self._icon_img)
        except Exception:
            pass

        style = ttk.Style(r)
        if sys.platform.startswith("linux") and "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Status.TLabel", font=("TkDefaultFont", 13, "bold"))
        style.configure("Id.TLabel", font=("TkDefaultFont", 22, "bold"))
        style.configure("Pw.TLabel", font=("TkFixedFont", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#666")
        style.configure("Accent.TButton", padding=(14, 6))

        outer = ttk.Frame(r, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)

        # --- status global
        head = ttk.Frame(outer)
        head.pack(fill="x")
        self.dot = tk.Canvas(head, width=16, height=16, highlightthickness=0, bd=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot = self.dot.create_oval(2, 2, 14, 14, fill="#999", outline="")
        txt = ttk.Frame(head)
        txt.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="Memulai...")
        self.sub_var = tk.StringVar(value="")
        ttk.Label(txt, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")
        ttk.Label(txt, textvariable=self.sub_var, style="Hint.TLabel", wraplength=380).pack(anchor="w")
        self.toggle_btn = ttk.Button(head, text="Hentikan", style="Accent.TButton", command=self.toggle_server)
        self.toggle_btn.pack(side="right")

        if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
            ttk.Label(outer, foreground="#c0392b", wraplength=500,
                      text="Sesi Wayland terdeteksi: layar tidak bisa di-capture. Logout dan pilih "
                           "'Ubuntu on Xorg' di layar login.").pack(fill="x", pady=(8, 0))

        # --- bar notifikasi update (disembunyikan sampai ada update)
        self.upd_bar = tk.Frame(outer, background="#fff4d6", highlightthickness=1, highlightbackground="#f0c36d")
        self.upd_text = tk.Label(self.upd_bar, background="#fff4d6", anchor="w", justify="left", wraplength=560)
        self.upd_text.pack(side="left", fill="x", expand=True, padx=10, pady=6)
        self.upd_later = ttk.Button(self.upd_bar, text="Nanti", command=self.dismiss_update)
        self.upd_later.pack(side="right", padx=(4, 8), pady=4)
        self.upd_btn = ttk.Button(self.upd_bar, text="Pasang & restart", style="Accent.TButton",
                                  command=self.install_update)
        self.upd_btn.pack(side="right", pady=4)
        self.upd_anchor = ttk.Frame(outer)  # penanda posisi bar
        self.upd_anchor.pack(fill="x")

        self.log_var = tk.StringVar(value="")
        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True, pady=(10, 0))
        self.tab_dash = ttk.Frame(self.nb, padding=12)
        self.tab_host = ttk.Frame(self.nb, padding=12)
        self.tab_settings = ttk.Frame(self.nb, padding=12)
        self.nb.add(self.tab_dash, text="  Dashboard  ")
        self.nb.add(self.tab_host, text="  Komputer ini  ")
        self.nb.add(self.tab_settings, text="  Pengaturan  ")
        self.dashboard = dashboard.Dashboard(self, self.tab_dash)
        self._build_host_tab(self.tab_host)
        self._build_settings_tab(self.tab_settings)
        self.nb.enable_traversal()  # Ctrl+Tab untuk pindah tab
        try:
            self.nb.select(int(self.conf.get("ui_tab", self.DASH_TAB)))
        except Exception:
            pass
        self.nb.bind("<<NotebookTabChanged>>", self._tab_changed)

        ttk.Label(outer, textvariable=self.log_var, style="Hint.TLabel").pack(fill="x", pady=(8, 0))

    DASH_TAB = 0

    def _tab_changed(self, _e=None):
        idx = self.nb.index(self.nb.select())
        if idx != self.conf.get("ui_tab"):
            self.conf["ui_tab"] = idx
            server.save_config(self.conf)
        if idx == self.DASH_TAB and self.dashboard:
            self.dashboard._thumb_tick(reschedule=False)

    def _build_host_tab(self, t):
        ttk.Label(t, text="Komputer ini bisa dikontrol dari LanDesk lain di jaringan yang sama "
                          "dengan ID dan password berikut:", style="Hint.TLabel", wraplength=480).pack(anchor="w")
        card = ttk.Frame(t)
        card.pack(fill="x", pady=(10, 0))
        ttk.Label(card, text="ID", style="Hint.TLabel").grid(row=0, column=0, sticky="w")
        self.id_var = tk.StringVar(value=server.format_id(self.conf["device_id"]))
        ttk.Label(card, textvariable=self.id_var, style="Id.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Button(card, text="Salin", width=7, command=lambda: self.copy(self.conf["device_id"], "ID disalin")
                   ).grid(row=1, column=1, padx=(12, 0), sticky="w")

        ttk.Label(card, text="Password", style="Hint.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 0))
        pwf = ttk.Frame(card)
        pwf.grid(row=3, column=0, columnspan=3, sticky="we")
        self.pw_var = tk.StringVar(value=self.conf["password"])
        self.pw_entry = ttk.Entry(pwf, textvariable=self.pw_var, show="•", width=16, font=("TkFixedFont", 13))
        self.pw_entry.pack(side="left")
        self.show_pw = tk.BooleanVar(value=False)
        ttk.Checkbutton(pwf, text="Lihat", variable=self.show_pw,
                        command=lambda: self.pw_entry.configure(show="" if self.show_pw.get() else "•")
                        ).pack(side="left", padx=(8, 4))
        ttk.Button(pwf, text="Salin", width=6, command=lambda: self.copy(self.pw_var.get(), "Password disalin")
                   ).pack(side="left", padx=2)
        ttk.Button(pwf, text="Acak", width=6, command=self.random_password).pack(side="left", padx=2)
        ttk.Button(pwf, text="Simpan", width=7, command=self.save_password).pack(side="left", padx=2)

        self.addr_var = tk.StringVar(value="")
        af = ttk.Frame(t)
        af.pack(fill="x", pady=(12, 0))
        ttk.Label(af, text="Tanpa aplikasi? Buka di browser:", style="Hint.TLabel").pack(side="left")
        ttk.Label(af, textvariable=self.addr_var, foreground="#2a6fdb", cursor="hand2").pack(side="left", padx=6)
        self.addr_copy = ttk.Button(af, text="Salin", width=6, command=self.copy_first_url)
        self.addr_copy.pack(side="left")

        cl = ttk.LabelFrame(t, text=" Sedang mengontrol komputer ini ", padding=8)
        cl.pack(fill="both", expand=True, pady=(12, 0))
        cols = ("addr", "ua", "since", "fps", "mbps")
        self.tree = ttk.Treeview(cl, columns=cols, show="headings", height=4)
        for col, title, w in (("addr", "Alamat", 110), ("ua", "Perangkat", 130), ("since", "Sejak", 60),
                              ("fps", "FPS", 50), ("mbps", "Mbps", 55)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=w, anchor="w" if col in ("addr", "ua") else "center")
        self.tree.pack(fill="both", expand=True)
        ttk.Button(cl, text="Putuskan semua", command=self.disconnect_all).pack(anchor="e", pady=(6, 0))

    def _build_settings_tab(self, st):
        c = self.conf
        self.port_var = tk.StringVar(value=str(c.get("port", 8765)))
        self.mon_var = tk.StringVar()
        mon_labels = [lbl for _, lbl in self.monitors]
        cur_mon = c.get("monitor")
        self.mon_var.set(next((lbl for idx, lbl in self.monitors if idx == cur_mon), mon_labels[0]))
        self.q_var = tk.IntVar(value=int(c.get("quality", 70)))
        self.fps_var = tk.StringVar(value=str(c.get("fps", 30)))
        scale = float(c.get("scale", 1.0))
        self.scale_var = tk.StringVar(value=next((k for k, v in SCALE_CHOICES.items() if abs(v - scale) < 0.01), "100%"))
        self.view_only_var = tk.BooleanVar(value=bool(c.get("view_only", False)))
        self.confirm_var = tk.BooleanVar(value=bool(c.get("confirm", False)))
        self.files_var = tk.BooleanVar(value=bool(c.get("allow_files", True)))
        self.clip_var = tk.BooleanVar(value=bool(c.get("clipboard", True)))
        self.preview_var = tk.BooleanVar(value=bool(c.get("allow_preview", True)))
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self.tray_var = tk.BooleanVar(value=bool(c.get("close_to_tray", True)) and HAVE_TRAY)

        g = ttk.LabelFrame(st, text=" Saat komputer ini dikontrol ", padding=10)
        g.pack(fill="x")

        def row(i, label, widget):
            ttk.Label(g, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 10))
            widget.grid(row=i, column=1, sticky="we", pady=3)

        row(0, "Monitor", ttk.Combobox(g, textvariable=self.mon_var, values=mon_labels, state="readonly"))
        qf = ttk.Frame(g)
        self.q_label = ttk.Label(qf, width=4)
        ttk.Scale(qf, from_=20, to=95, variable=self.q_var, orient="horizontal",
                  command=lambda v: self.q_label.configure(text=str(int(float(v))))).pack(side="left", fill="x", expand=True)
        self.q_label.pack(side="left", padx=(8, 0))
        self.q_label.configure(text=str(self.q_var.get()))
        row(1, "Kualitas gambar", qf)
        ff = ttk.Frame(g)
        ttk.Combobox(ff, textvariable=self.fps_var, values=FPS_CHOICES, width=5).pack(side="left")
        ttk.Label(ff, text="Resolusi").pack(side="left", padx=(14, 6))
        ttk.Combobox(ff, textvariable=self.scale_var, values=list(SCALE_CHOICES), state="readonly",
                     width=6).pack(side="left")
        ttk.Label(ff, text="Port").pack(side="left", padx=(14, 6))
        ttk.Spinbox(ff, textvariable=self.port_var, from_=1024, to=65535, width=7).pack(side="left")
        row(2, "FPS maksimum", ff)
        g.columnconfigure(1, weight=1)

        sec = ttk.LabelFrame(st, text=" Keamanan & fitur ", padding=10)
        sec.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(sec, text="Minta persetujuan saya setiap ada koneksi masuk "
                                  "(pratinjau dashboard otomatis mati)",
                        variable=self.confirm_var).pack(anchor="w")
        ttk.Checkbutton(sec, text="Izinkan pratinjau layar di dashboard LanDesk lain (butuh password)",
                        variable=self.preview_var).pack(anchor="w")
        ttk.Checkbutton(sec, text="Hanya lihat (client tidak bisa mengontrol mouse/keyboard)",
                        variable=self.view_only_var).pack(anchor="w")
        ttk.Checkbutton(sec, text="Izinkan transfer file", variable=self.files_var).pack(anchor="w")
        clip_cb = ttk.Checkbutton(sec, text="Sinkron clipboard (teks)", variable=self.clip_var)
        clip_cb.pack(anchor="w")
        if not server.clipboard.available():
            clip_cb.configure(text="Sinkron clipboard (butuh xclip: sudo apt install xclip)")

        app = ttk.LabelFrame(st, text=" Aplikasi ", padding=10)
        app.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(app, text="Jalankan otomatis saat login", variable=self.autostart_var,
                        command=self.apply_autostart).pack(anchor="w")
        tray_cb = ttk.Checkbutton(app, text="Saat jendela ditutup, tetap jalan di system tray",
                                  variable=self.tray_var, command=self.save_tray_pref)
        tray_cb.pack(anchor="w")
        if not HAVE_TRAY:
            tray_cb.state(["disabled"])

        upd = app  # bagian update ada di kotak "Aplikasi" supaya tab tetap muat saat bar update tampil
        self.autoupd_var = tk.BooleanVar(value=bool(c.get("update_check", True)))
        ttk.Checkbutton(upd, text="Cek update otomatis dari GitHub (tiap 6 jam, tanya sebelum memasang)",
                        variable=self.autoupd_var, command=self.save_update_pref).pack(anchor="w")
        urow = ttk.Frame(upd)
        urow.pack(fill="x", pady=(6, 0))
        src = "versi update" if updater.running_from_update() else "versi bawaan installer"
        self.ver_var = tk.StringVar(value=f"Versi {server.__version__} ({src})")
        ttk.Label(urow, textvariable=self.ver_var, style="Hint.TLabel").pack(side="left")
        self.check_btn = ttk.Button(urow, text="Cek update sekarang", command=lambda: self.check_update(manual=True))
        self.check_btn.pack(side="right")

        ttk.Button(st, text="Simpan & terapkan", style="Accent.TButton", command=self.apply_settings).pack(
            anchor="e", pady=(12, 0))

    # ------------------------------------------------------------- server
    def _make_cfg(self):
        c = self.conf
        port = int(c.get("port", 8765))
        cfg = {
            "password": c["password"],
            "source": server.get_source(c.get("monitor"), use_dxgi=self.use_dxgi),
            "use_dxgi": self.use_dxgi,
            "info": server.host_info(c, port),
            "quality": min(max(int(c.get("quality", 70)), 10), 95),
            "fps": min(max(int(c.get("fps", 30)), 1), 120),
            "scale": min(max(float(c.get("scale", 1.0)), 0.25), 1.0),
            "view_only": bool(c.get("view_only", False)),
            "allow_files": bool(c.get("allow_files", True)),
            "clipboard": bool(c.get("clipboard", True)),
            "allow_preview": bool(c.get("allow_preview", True)),
            "on_event": lambda kind, data: self.q.put((kind, data)),
        }
        if c.get("confirm"):
            cfg["confirm_cb"] = self._confirm_cb
        return cfg

    def _confirm_cb(self, peer, ua):
        """Dipanggil dari thread server; hasil dikirim lewat Future."""
        fut = concurrent.futures.Future()
        self.q.put(("confirm", {"peer": peer, "ua": ua, "fut": fut}))
        return fut

    def start_server(self):
        port = int(self.conf.get("port", 8765))
        try:
            self.cfg = self._make_cfg()
            self.server = server.HostServer(self.cfg, port=port)
            self.server.start()
        except OSError as e:
            self.server = None
            self._set_status("error", "Gagal mulai", f"Port {port} sedang dipakai program lain "
                                                    "(LanDesk lain sudah jalan?). Ganti port di Pengaturan.")
            log.warning("port %s tidak bisa dipakai: %s", port, e)
            self.toggle_btn.configure(text="Mulai")
            self._render_addresses(port)
            return
        except Exception as e:
            self.server = None
            self._set_status("error", "Gagal mulai", str(e))
            self.toggle_btn.configure(text="Mulai")
            return
        self._render_addresses(port)
        self.toggle_btn.configure(text="Hentikan")
        self._refresh_status()
        log.info("Server aktif di port %s (capture: %s)", port, self.cfg["source"].backend)

    def stop_server(self):
        if self.server:
            self.server.stop()
        self.server = None
        self.clients.clear()
        self._refresh_clients()
        self.toggle_btn.configure(text="Mulai")
        self._refresh_status()

    def toggle_server(self):
        if self.server and self.server.running:
            if self.clients and not messagebox.askyesno("LanDesk", "Ada yang sedang terhubung. Hentikan?"):
                return
            self.stop_server()
        else:
            self.start_server()

    def restart_server(self):
        self.stop_server()
        self.start_server()

    def disconnect_all(self):
        if self.server:
            self.server.disconnect_all()

    def _render_addresses(self, port):
        ips = server.local_ips()
        self.urls = [f"http://{ip}:{port}" for ip in ips]
        lan = [u for u, ip in zip(self.urls, ips) if remote._prefer(ip)] or self.urls
        self.addr_var.set(lan[0] if lan else "")

    # ------------------------------------------------------------- aksi host
    def copy(self, text, msg):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log_var.set(msg)

    def copy_first_url(self):
        if self.addr_var.get():
            self.copy(self.addr_var.get(), "Alamat disalin")

    def random_password(self):
        self.pw_var.set(secrets.token_hex(4))
        self.show_pw.set(True)
        self.pw_entry.configure(show="")
        self.save_password()

    def save_password(self):
        pw = self.pw_var.get().strip()
        if len(pw) < 4:
            messagebox.showwarning("LanDesk", "Password minimal 4 karakter.")
            return
        self.conf["password"] = pw
        server.save_config(self.conf)
        if self.cfg:
            self.cfg["password"] = pw
        self.log_var.set("Password disimpan")

    def apply_settings(self):
        try:
            port = int(self.port_var.get())
            fps = int(self.fps_var.get())
            assert 1024 <= port <= 65535 and 1 <= fps <= 120
        except Exception:
            messagebox.showwarning("LanDesk", "Port (1024-65535) atau FPS (1-120) tidak valid.")
            return
        mon = next((idx for idx, lbl in self.monitors if lbl == self.mon_var.get()), None)
        need_restart = port != int(self.conf.get("port", 8765)) or mon != self.conf.get("monitor")
        self.conf.update({
            "port": port, "monitor": mon, "fps": fps, "quality": int(self.q_var.get()),
            "scale": SCALE_CHOICES[self.scale_var.get()], "view_only": bool(self.view_only_var.get()),
            "confirm": bool(self.confirm_var.get()), "allow_files": bool(self.files_var.get()),
            "clipboard": bool(self.clip_var.get()), "allow_preview": bool(self.preview_var.get()),
        })
        server.save_config(self.conf)
        if need_restart or not (self.server and self.server.running):
            if self.clients and not messagebox.askyesno(
                    "LanDesk", "Perubahan port/monitor perlu restart dan memutus koneksi. Lanjutkan?"):
                return
            self.restart_server()
        elif self.cfg:
            for k in ("quality", "fps", "scale", "view_only", "allow_files", "clipboard", "allow_preview"):
                self.cfg[k] = self.conf[k]
            self.cfg["info"]["preview"] = self.conf["allow_preview"] and not self.conf["confirm"]
            if self.conf["confirm"]:
                self.cfg["confirm_cb"] = self._confirm_cb
            else:
                self.cfg.pop("confirm_cb", None)
        self.log_var.set("Pengaturan disimpan (berlaku untuk koneksi berikutnya)")

    def apply_autostart(self):
        try:
            set_autostart(self.autostart_var.get())
            self.log_var.set("Autostart " + ("aktif" if self.autostart_var.get() else "dimatikan"))
        except Exception as e:
            self.autostart_var.set(autostart_enabled())
            messagebox.showerror("LanDesk", f"Gagal mengubah autostart: {e}")

    def save_tray_pref(self):
        self.conf["close_to_tray"] = bool(self.tray_var.get())
        server.save_config(self.conf)

    # ------------------------------------------------------------- update
    UPDATE_EVERY_MS = 6 * 3600 * 1000

    def save_update_pref(self):
        self.conf["update_check"] = bool(self.autoupd_var.get())
        server.save_config(self.conf)
        if self.conf["update_check"]:
            self.check_update()

    def _update_tick(self):
        if self.quitting:
            return
        if self.conf.get("update_check", True):
            self.check_update()
        self.root.after(self.UPDATE_EVERY_MS, self._update_tick)

    def check_update(self, manual=False):
        if getattr(self, "_checking", False):
            return
        self._checking = True
        if manual:
            self.check_btn.state(["disabled"])
            self.log_var.set("Mengecek update...")

        def work():
            try:
                res = updater.check(server.__version__, self.conf)
                self.q.put(("update_info", {"info": res, "manual": manual}))
            except Exception as e:
                self.q.put(("update_info", {"error": str(e), "manual": manual}))

        threading.Thread(target=work, daemon=True).start()

    def _on_update_info(self, data):
        self._checking = False
        self.check_btn.state(["!disabled"])
        manual = data.get("manual")
        if data.get("error"):
            if manual:
                self.log_var.set("")
                messagebox.showwarning("LanDesk", data["error"])
            return
        info = data.get("info")
        if not info:
            if manual:
                self.log_var.set(f"LanDesk {server.__version__} sudah versi terbaru")
            return
        if not manual and self.conf.get("update_skip") == info["version"]:
            return
        self.pending_update = info
        notes = (info.get("notes") or "").strip().splitlines()
        note = f"  -  {notes[0][:90]}" if notes else ""
        if info.get("compatible"):
            self.upd_text.configure(text=f"Update LanDesk {info['version']} tersedia (sekarang {server.__version__})"
                                         f"{note}")
            self.upd_btn.configure(text="Pasang & restart", command=self.install_update)
        else:
            self.upd_text.configure(text=f"LanDesk {info['version']} tersedia, tapi butuh paket lengkap "
                                         f"(ada perubahan library). Download lalu pasang ulang.")
            self.upd_btn.configure(text="Buka halaman download", command=self.open_release_page)
        self.upd_btn.state(["!disabled"])
        self.upd_bar.pack(fill="x", pady=(8, 0), before=self.upd_anchor)
        self.notify(f"Update LanDesk {info['version']} tersedia")

    def dismiss_update(self):
        info = getattr(self, "pending_update", None)
        if info:
            self.conf["update_skip"] = info["version"]  # tidak ditawarkan lagi otomatis sampai ada versi lebih baru
            server.save_config(self.conf)
        self.upd_bar.pack_forget()

    def open_release_page(self):
        import webbrowser

        info = getattr(self, "pending_update", None) or {}
        webbrowser.open(info.get("page") or updater.release_page(self.conf))

    def install_update(self):
        info = getattr(self, "pending_update", None)
        if not info:
            return
        if self.clients and not messagebox.askyesno(
                "LanDesk", "Ada yang sedang meremote komputer ini. Pasang update dan restart sekarang?"):
            return
        self.upd_btn.state(["disabled"])
        self.upd_text.configure(text=f"Mengunduh LanDesk {info['version']}...")

        def progress(frac):
            self.q.put(("update_progress", frac))

        def work():
            try:
                updater.download_and_stage(info, self.conf, progress)
                self.q.put(("update_ready", info))
            except Exception as e:
                self.q.put(("update_failed", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _restart_into_update(self, info):
        self.upd_text.configure(text=f"LanDesk {info['version']} terpasang. Memulai ulang...")
        self.root.update_idletasks()
        try:
            updater.spawn_restart(["--minimized"] if not self.root.winfo_viewable() else [])
        except Exception as e:
            messagebox.showerror("LanDesk", f"Update terpasang, tapi gagal restart otomatis: {e}\n"
                                            "Tutup lalu buka LanDesk lagi.")
            return
        self.quit()

    def ask_confirm(self, peer, ua, fut):
        self.show_window()
        win = tk.Toplevel(self.root)
        win.title("LanDesk - koneksi masuk")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Ada yang ingin mengontrol komputer ini", style="Status.TLabel").pack(anchor="w")
        ttk.Label(f, text=f"Dari: {peer}   ({short_ua(ua)})").pack(anchor="w", pady=(6, 0))
        left = tk.IntVar(value=40)
        lbl = ttk.Label(f, style="Hint.TLabel")
        lbl.pack(anchor="w", pady=(4, 10))
        bf = ttk.Frame(f)
        bf.pack(fill="x")

        def done(ok):
            if not fut.done():
                fut.set_result(ok)
            win.destroy()

        ttk.Button(bf, text="Tolak", command=lambda: done(False)).pack(side="right")
        ttk.Button(bf, text="Terima", style="Accent.TButton", command=lambda: done(True)).pack(side="right", padx=8)
        win.protocol("WM_DELETE_WINDOW", lambda: done(False))

        def tick():
            if not win.winfo_exists():
                return
            n = left.get()
            if n <= 0:
                done(False)
                return
            lbl.configure(text=f"Otomatis ditolak dalam {n} detik")
            left.set(n - 1)
            win.after(1000, tick)

        tick()
        win.lift()
        win.focus_force()
        self.notify(f"Permintaan koneksi dari {peer}")

    # ------------------------------------------------------------- status
    def _set_status(self, kind, title, sub):
        color = {"ok": "#2ecc71", "busy": "#3498db", "off": "#999999", "error": "#e74c3c"}[kind]
        self.dot.itemconfigure(self._dot, fill=color)
        self.status_var.set(title)
        self.sub_var.set(sub)
        if self.tray:
            self.tray.title = f"LanDesk - {title}"

    def _refresh_status(self):
        if not (self.server and self.server.running):
            self._set_status("off", "Berhenti", "Komputer ini tidak bisa dikontrol dari jaringan.")
            return
        n = len(self.clients)
        if n:
            self._set_status("busy", f"Sedang dikontrol ({n})", "Komputer ini sedang diakses dari jarak jauh.")
        else:
            self._set_status("ok", "Siap", "Komputer ini bisa dikontrol dengan ID & password di bawah.")

    def _refresh_clients(self):
        self.tree.delete(*self.tree.get_children())
        for cid, c in sorted(self.clients.items()):
            self.tree.insert("", "end", iid=str(cid), values=(
                c["peer"], c["ua"], time.strftime("%H:%M", time.localtime(c["since"])),
                c.get("fps", "-"), f"{c.get('kbps', 0) / 1000:.1f}"))

    def _poll(self):
        changed = False
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "connect":
                    self.clients[data["id"]] = {"peer": data["peer"], "ua": short_ua(data.get("ua", "")),
                                                "since": time.time()}
                    self.log_var.set(f"{time.strftime('%H:%M')}  {data['peer']} mulai mengontrol komputer ini")
                    self.notify(f"{data['peer']} terhubung ke komputer ini")
                    changed = True
                elif kind == "disconnect":
                    self.clients.pop(data["id"], None)
                    self.log_var.set(f"{time.strftime('%H:%M')}  {data['peer']} terputus")
                    changed = True
                elif kind == "stats" and data["id"] in self.clients:
                    self.clients[data["id"]].update(fps=data["fps"], kbps=data["kbps"])
                    iid = str(data["id"])
                    if self.tree.exists(iid):
                        self.tree.set(iid, "fps", data["fps"])
                        self.tree.set(iid, "mbps", f"{data['kbps'] / 1000:.1f}")
                elif kind == "auth_fail":
                    self.log_var.set(f"{time.strftime('%H:%M')}  login gagal dari {data['peer']}")
                elif kind == "file":
                    self.log_var.set(f"{time.strftime('%H:%M')}  file diterima: {data['name']} -> {data['dir']}")
                    self.notify(f"File diterima: {data['name']}")
                elif kind == "confirm":
                    self.ask_confirm(data["peer"], data["ua"], data["fut"])
                elif kind == "log" and data["level"] >= logging.WARNING:
                    self.log_var.set(data["msg"])
                elif kind == "update_info":
                    self._on_update_info(data)
                elif kind == "update_progress":
                    self.upd_text.configure(text=f"Mengunduh update... {int(data * 100)}%")
                elif kind == "update_ready":
                    self._restart_into_update(data)
                elif kind == "update_failed":
                    self.upd_btn.state(["!disabled"])
                    self.upd_text.configure(text=f"Update gagal: {data}")
                elif kind == "tray":
                    getattr(self, data)()
        except queue.Empty:
            pass
        if changed:
            self._refresh_clients()
            self._refresh_status()
        if not self.quitting:
            self.root.after(150, self._poll)

    # ------------------------------------------------------------- tray / jendela
    def _init_tray(self):
        if not HAVE_TRAY:
            return
        try:
            img = Image.open(ICON_PATH)
            menu = pystray.Menu(
                pystray.MenuItem("Buka LanDesk", lambda: self.q.put(("tray", "show_window")), default=True),
                pystray.MenuItem("Salin ID", lambda: self.q.put(("tray", "copy_id"))),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Keluar", lambda: self.q.put(("tray", "quit"))),
            )
            self.tray = pystray.Icon("LanDesk", img, "LanDesk", menu)
            self.tray.run_detached()
        except Exception as e:
            log.info("system tray tidak tersedia: %s", e)
            self.tray = None

    def copy_id(self):
        self.copy(self.conf["device_id"], "ID disalin")

    def notify(self, msg):
        if self.tray and not self.root.winfo_viewable():
            try:
                self.tray.notify(msg, "LanDesk")
            except Exception:
                pass

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide_window(self):
        if self.tray:
            self.root.withdraw()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                try:
                    self.tray.notify("LanDesk tetap berjalan di system tray.", "LanDesk")
                except Exception:
                    pass
        else:
            self.root.iconify()

    def on_close(self):
        if self.tray and self.tray_var.get():
            self.hide_window()
            return
        if self.server and self.server.running and self.clients:
            if not messagebox.askyesno("LanDesk", "Ada yang sedang terhubung. Hentikan LanDesk dan keluar?"):
                return
        self.quit()

    def quit(self):
        self.quitting = True
        try:
            if self.dashboard:
                self.dashboard.shutdown()
            if self.server:
                self.server.stop()
        finally:
            if self.tray:
                try:
                    self.tray.stop()
                except Exception:
                    pass
            self.root.destroy()


def _instance_port() -> int:
    """Port lock per file config (instance dengan config berbeda tidak saling mengunci)."""
    import zlib

    return 47000 + zlib.crc32(str(server.config_path()).lower().encode()) % 1000


def single_instance(show_cb_holder, start_minimized):
    """Pastikan hanya satu GUI LanDesk per config. Return socket lock, atau None kalau sudah ada yang jalan
    (instance itu diminta memunculkan jendelanya)."""
    import socket as _s

    port = _instance_port()
    # setelah update, instance lama butuh beberapa detik untuk menutup -> tunggu lock-nya lepas
    deadline = time.time() + (20 if os.environ.pop("LANDESK_RESTART_WAIT", None) else 0)
    while True:
        lock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
        if sys.platform == "win32":
            lock.setsockopt(_s.SOL_SOCKET, getattr(_s, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFB), 1)
        try:
            lock.bind(("127.0.0.1", port))
            lock.listen(4)
            break
        except OSError:
            lock.close()
            if time.time() < deadline:
                time.sleep(0.5)
                continue
            lock = None
            break
    if lock is None:
        if not start_minimized:
            try:
                with _s.create_connection(("127.0.0.1", port), timeout=2) as c:
                    c.sendall(b"show")
            except OSError:
                pass
        return None

    def serve():
        while True:
            try:
                conn, _ = lock.accept()
            except OSError:
                return
            with conn:
                try:
                    if conn.recv(16).startswith(b"show") and show_cb_holder:
                        show_cb_holder[0]()
                except OSError:
                    pass

    import threading

    threading.Thread(target=serve, name="landesk-instance", daemon=True).start()
    return lock


def run(start_minimized=False, use_dxgi=True):
    holder = []
    lock = single_instance(holder, start_minimized)
    if lock is None:
        log.info("LanDesk sudah berjalan; jendela yang ada dimunculkan.")
        return
    if sys.platform == "win32":
        try:  # taskbar memakai ikon LanDesk, bukan ikon pythonw
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LanDesk.Host")
        except Exception:
            pass
    root = tk.Tk(className="LanDesk")
    app = App(root, start_minimized=start_minimized, use_dxgi=use_dxgi)
    holder.append(lambda: app.q.put(("tray", "show_window")))
    root.mainloop()
    lock.close()
