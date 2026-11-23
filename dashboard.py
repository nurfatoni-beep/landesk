"""
Dashboard LanDesk: semua PC dalam satu layar (gaya aplikasi pemantau warnet / lab komputer).

* PC tersimpan (pernah terhubung / ditambahkan manual) + PC yang ditemukan di LAN.
* Tiap kartu: pratinjau layar live, status online/offline, nama, ID, IP.
* Klik dua kali -> langsung konek dengan password tersimpan (tidak perlu ingat ID).
* PC dikenali lewat ID, jadi IP yang berubah (DHCP) diperbarui otomatis dari hasil pencarian.
"""
import concurrent.futures
import io
import queue
import time
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

import remote
import server

THUMB_W, THUMB_H = 256, 144
CARD_PAD = 10
THUMB_EVERY_MS = 3000        # interval refresh pratinjau
OFFLINE_RETRY_S = 15         # PC offline dicoba lagi tiap n detik
DISCOVER_EVERY_S = 15
ONLINE_WINDOW_S = 25         # dianggap online kalau terlihat dalam n detik terakhir

CARD_BG = "#ffffff"
CARD_BORDER = "#d5d9e0"
CARD_SELECTED = "#4f8cff"
MUTED = "#6b7280"
STATUS_COLORS = {"online": "#22c55e", "busy": "#3b82f6", "offline": "#9ca3af", "warn": "#f59e0b",
                 "error": "#ef4444", "new": "#8b5cf6"}


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow lama
        return ImageFont.load_default()


def placeholder(text, bg="#262a33"):
    img = Image.new("RGB", (THUMB_W, THUMB_H), bg)
    d = ImageDraw.Draw(img)
    f = _font(15)
    box = d.textbbox((0, 0), text, font=f)
    d.text(((THUMB_W - (box[2] - box[0])) / 2, (THUMB_H - (box[3] - box[1])) / 2 - 2), text,
           fill="#cfd4dc", font=f)
    return img


def fit_thumb(img):
    """Letterbox ke ukuran kartu, menjaga rasio layar."""
    img = img.convert("RGB")
    img.thumbnail((THUMB_W, THUMB_H), Image.BILINEAR)
    canvas = Image.new("RGB", (THUMB_W, THUMB_H), "#000000")
    canvas.paste(img, ((THUMB_W - img.width) // 2, (THUMB_H - img.height) // 2))
    return canvas


def dev_key(dev):
    return dev.get("id") or f"{dev.get('ip')}:{dev.get('port', remote.DEFAULT_PORT)}"


class Dashboard:
    def __init__(self, app, parent):
        self.app = app
        self.root = app.root
        self.conf = app.conf
        self.q = queue.Queue()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="dash")
        self.devices = self._load_devices()          # key -> dict tersimpan di config
        self.found = {}                              # id -> info discovery (+ last_seen)
        self.state = {}                              # key -> status runtime
        self.cards = {}                              # key -> widget kartu
        self.selected = None
        self._ph = {}
        self._discovering = False
        self._build(parent)
        self.root.after(200, self._poll)
        self.root.after(400, self.discover_now)
        self.root.after(1200, self._thumb_tick)
        self.root.after(DISCOVER_EVERY_S * 1000, self._discover_tick)

    # ------------------------------------------------------------- data
    def _load_devices(self):
        devs = self.conf.get("devices")
        if isinstance(devs, dict):
            return devs
        devs = {}
        # migrasi dari versi 1.1 (riwayat + password tersimpan)
        saved = self.conf.get("saved_pw", {})
        for r in self.conf.get("recent", []):
            key = r.get("key", "")
            is_id = key.isdigit() and len(key) == 9
            host, _, port = (r.get("ip") or key).partition(":")
            dev = {"id": key if is_id else "", "name": r.get("name", ""), "alias": "",
                   "ip": host if not is_id else r.get("ip", ""), "port": int(port) if port.isdigit() else 8765,
                   "pw": saved.get(key, ""), "added": r.get("ts", int(time.time())),
                   "last_connected": r.get("ts", 0)}
            devs[dev_key(dev)] = dev
        self.conf["devices"] = devs
        server.save_config(self.conf)
        return devs

    def _save(self):
        self.conf["devices"] = self.devices
        server.save_config(self.conf)

    def _me(self):
        return self.conf.get("device_id")

    def _display_name(self, dev):
        return dev.get("alias") or dev.get("name") or dev.get("ip") or "?"

    def _entries(self):
        """List (key, dev, is_new) yang ditampilkan, urut: online dulu, lalu nama."""
        me = self._me()
        out = [(k, d, False) for k, d in self.devices.items() if d.get("id") != me]
        for fid, info in self.found.items():
            if fid != me and fid not in self.devices:
                out.append((fid, {"id": fid, "name": info.get("name", ""), "ip": info.get("ip"),
                                  "port": info.get("port", 8765), "os": info.get("os", "")}, True))
        flt = self.filter_var.get().strip().lower()
        if flt:
            out = [e for e in out if flt in " ".join(str(e[1].get(x, "")) for x in
                                                     ("alias", "name", "id", "ip")).lower()]
        out.sort(key=lambda e: (e[2], not self._is_online(e[0], e[1]), self._display_name(e[1]).lower()))
        return out

    def _is_online(self, key, dev):
        now = time.time()
        st = self.state.get(key, {})
        seen = self.found.get(dev.get("id") or "", {}).get("last_seen", 0)
        return now - seen < ONLINE_WINDOW_S or now - st.get("last_ok", 0) < ONLINE_WINDOW_S

    # ------------------------------------------------------------- UI
    def _build(self, t):
        top = ttk.Frame(t)
        top.pack(fill="x")
        ttk.Label(top, text="ID / IP").pack(side="left")
        self.target_var = tk.StringVar(value="")
        te = ttk.Entry(top, textvariable=self.target_var, width=20)
        te.pack(side="left", padx=(6, 10))
        ttk.Label(top, text="Password").pack(side="left")
        self.pw_var = tk.StringVar()
        pe = ttk.Entry(top, textvariable=self.pw_var, show="•", width=14)
        pe.pack(side="left", padx=(6, 10))
        self.connect_btn = ttk.Button(top, text="Hubungkan", style="Accent.TButton", command=self.connect_typed)
        self.connect_btn.pack(side="left")
        te.bind("<Return>", lambda e: pe.focus_set())
        pe.bind("<Return>", lambda e: self.connect_typed())

        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=(10, 6))
        ttk.Button(bar, text="+ Tambah PC", command=self.add_dialog).pack(side="left")
        ttk.Button(bar, text="Cari di LAN", command=self.discover_now).pack(side="left", padx=6)
        self.count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.count_var, foreground=MUTED).pack(side="left", padx=6)
        self.filter_var = tk.StringVar()
        fe = ttk.Entry(bar, textvariable=self.filter_var, width=18)
        fe.pack(side="right")
        ttk.Label(bar, text="Filter").pack(side="right", padx=(0, 6))
        self.filter_var.trace_add("write", lambda *a: self.render())

        wrap = ttk.Frame(t)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, highlightthickness=0, bd=0, background="#eef0f3")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, background="#eef0f3")
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(self._win, width=e.width),
                                                   self._layout()))
        self.empty = tk.Label(self.inner, background="#eef0f3", foreground=MUTED, justify="center",
                              text="Belum ada PC.\n\nPC yang menjalankan LanDesk di jaringan ini akan muncul "
                                   "otomatis,\natau klik \"+ Tambah PC\" / hubungkan lewat ID atau IP di atas.")
        # scroll dengan roda mouse saat kursor di atas dashboard
        self.canvas.bind("<Enter>", lambda e: self.root.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda e: self.root.unbind_all("<MouseWheel>"))
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))
        self.render()

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) * 2, "units")

    def _ph_img(self, text, bg="#262a33"):
        k = (text, bg)
        if k not in self._ph:
            self._ph[k] = ImageTk.PhotoImage(placeholder(text, bg))
        return self._ph[k]

    def _make_card(self, key):
        f = tk.Frame(self.inner, background=CARD_BG, highlightthickness=2, highlightbackground=CARD_BORDER,
                     cursor="hand2")
        img = tk.Label(f, background="#000000", bd=0)
        img.pack(padx=CARD_PAD, pady=(CARD_PAD, 6))
        row = tk.Frame(f, background=CARD_BG)
        row.pack(fill="x", padx=CARD_PAD)
        dot = tk.Canvas(row, width=12, height=12, highlightthickness=0, bd=0, background=CARD_BG)
        dot.pack(side="left", padx=(0, 6))
        oval = dot.create_oval(1, 1, 11, 11, fill=STATUS_COLORS["offline"], outline="")
        name = tk.Label(row, background=CARD_BG, font=("TkDefaultFont", 11, "bold"), anchor="w",
                        width=1)  # width=1 + fill: lebar mengikuti kartu, teks panjang terpotong
        name.pack(side="left", fill="x", expand=True)
        sub = tk.Label(f, background=CARD_BG, foreground=MUTED, anchor="w", font=("TkDefaultFont", 9))
        sub.pack(fill="x", padx=CARD_PAD)
        status = tk.Label(f, background=CARD_BG, anchor="w", justify="left", font=("TkDefaultFont", 9),
                          wraplength=THUMB_W)
        status.pack(fill="x", padx=CARD_PAD, pady=(0, CARD_PAD))
        card = {"frame": f, "img": img, "dot": dot, "oval": oval, "name": name, "sub": sub, "status": status}
        for w in (f, img, row, dot, name, sub, status):
            w.bind("<Double-1>", lambda e, k=key: self.connect(k))
            w.bind("<Button-1>", lambda e, k=key: self.select(k))
            w.bind("<Button-3>", lambda e, k=key: self.context_menu(e, k))
            w.bind("<Button-2>", lambda e, k=key: self.context_menu(e, k))  # macOS / beberapa mouse
        return card

    def render(self):
        entries = self._entries()
        keys = [e[0] for e in entries]
        for k in list(self.cards):
            if k not in keys:
                self.cards.pop(k)["frame"].destroy()
        for key, dev, is_new in entries:
            card = self.cards.get(key) or self.cards.setdefault(key, self._make_card(key))
            self._update_card(card, key, dev, is_new)
        self._order = keys
        saved = sum(1 for _, d, n in entries if not n)
        online = sum(1 for k, d, n in entries if not n and self._is_online(k, d))
        new = sum(1 for _, _, n in entries if n)
        txt = f"{saved} PC tersimpan, {online} online"
        if new:
            txt += f"  ·  {new} baru ditemukan"
        self.count_var.set(txt)
        self._layout()

    def _update_card(self, card, key, dev, is_new):
        st = self.state.get(key, {})
        online = self._is_online(key, dev)
        card["name"].configure(text=self._display_name(dev))
        ip = self.found.get(dev.get("id") or "", {}).get("ip") or dev.get("ip") or "-"
        idtxt = server.format_id(dev["id"]) if dev.get("id") else "tanpa ID"
        card["sub"].configure(text=f"{idtxt}  ·  {ip}")
        if is_new:
            kind, text, ph = "new", "Baru ditemukan - klik 2x untuk menambahkan", ("PC baru", "#3b2f63")
        elif not dev.get("pw"):
            kind, text, ph = ("warn" if online else "offline"), "Belum ada password - klik 2x", \
                ("Butuh password", "#3a3324")
        elif st.get("pw_bad"):
            kind, text, ph = "error", "Password salah - klik kanan > Ubah password", ("Password salah", "#4a2626")
        elif not online:
            seen = max(st.get("last_ok", 0), self.found.get(dev.get("id") or "", {}).get("last_seen", 0),
                       dev.get("last_seen", 0), dev.get("last_connected", 0), dev.get("added", 0))
            when = time.strftime("%d/%m %H:%M", time.localtime(seen)) if seen else "belum pernah"
            kind, text, ph = "offline", f"Offline  ·  terakhir terlihat {when}", ("Offline", "#262a33")
        elif st.get("disabled") == "old":
            kind, text, ph = "online", "Online  ·  LanDesk versi lama, update untuk pratinjau", \
                ("Versi lama", "#1f2937")
        elif st.get("disabled"):
            kind, text, ph = "online", "Online  ·  pratinjau dimatikan di PC itu", ("Pratinjau dimatikan", "#1f2937")
        else:
            kind, text, ph = "online", "Online", None
        card["dot"].itemconfigure(card["oval"], fill=STATUS_COLORS[kind])
        card["status"].configure(text=text, foreground=STATUS_COLORS["error"] if kind == "error" else MUTED)
        photo = st.get("photo") if ph is None else None
        if photo is None and ph is None:
            ph = ("Memuat...", "#262a33")
        card["img"].configure(image=photo or self._ph_img(*ph))
        card["frame"].configure(highlightbackground=CARD_SELECTED if key == self.selected else CARD_BORDER)

    def _layout(self):
        order = getattr(self, "_order", [])
        width = max(self.canvas.winfo_width(), 1)
        card_w = THUMB_W + 2 * CARD_PAD + 4
        cols = max(1, (width - 4) // (card_w + 12))
        if not order:
            self.empty.grid(row=0, column=0, padx=20, pady=40)
        else:
            self.empty.grid_forget()
        for i, key in enumerate(order):
            c = self.cards.get(key)
            if c:
                c["frame"].grid(row=i // cols, column=i % cols, padx=6, pady=6, sticky="nsew")
        for col in range(12):
            self.inner.columnconfigure(col, weight=0, uniform="card" if col < cols else "")

    def select(self, key):
        self.selected = key
        for k, c in self.cards.items():
            c["frame"].configure(highlightbackground=CARD_SELECTED if k == key else CARD_BORDER)

    # ------------------------------------------------------------- menu & dialog
    def context_menu(self, e, key):
        self.select(key)
        m = tk.Menu(self.root, tearoff=0)
        if key in self.devices:
            m.add_command(label="Hubungkan", command=lambda: self.connect(key))
            m.add_separator()
            m.add_command(label="Ganti nama...", command=lambda: self.rename(key))
            m.add_command(label="Ubah password tersimpan...", command=lambda: self.ask_password(key, connect=False))
            m.add_command(label="Salin ID", command=lambda: self.app.copy(self.devices[key].get("id", ""), "ID disalin"))
            m.add_separator()
            m.add_command(label="Hapus dari dashboard", command=lambda: self.remove(key))
        else:
            m.add_command(label="Tambahkan & hubungkan...", command=lambda: self.connect(key))
            m.add_command(label="Tambahkan ke dashboard...", command=lambda: self.ask_password(key, connect=False))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _form(self, title, fields, check=None):
        """Dialog modal sederhana. fields: [(label, default, secret)], check: teks checkbox atau None."""
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.resizable(False, False)
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)
        vars_ = []
        first = None
        for i, (label, default, secret) in enumerate(fields):
            ttk.Label(f, text=label).grid(row=i * 2, column=0, sticky="w", pady=(0 if i == 0 else 8, 2))
            v = tk.StringVar(value=default or "")
            ent = ttk.Entry(f, textvariable=v, width=34, show="•" if secret else "")
            ent.grid(row=i * 2 + 1, column=0, sticky="we")
            first = first or ent
            vars_.append(v)
        cv = tk.BooleanVar(value=False)
        if check:
            ttk.Checkbutton(f, text=check, variable=cv).grid(row=len(fields) * 2, column=0, sticky="w", pady=(10, 0))
        result = {}

        def ok(_e=None):
            result["values"] = [v.get().strip() for v in vars_]
            result["check"] = cv.get()
            win.destroy()

        bf = ttk.Frame(f)
        bf.grid(row=len(fields) * 2 + 1, column=0, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="Batal", command=win.destroy).pack(side="right")
        ttk.Button(bf, text="OK", style="Accent.TButton", command=ok).pack(side="right", padx=8)
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: win.destroy())
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + 120
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        if first:
            first.focus_set()
        self.root.wait_window(win)
        return result if "values" in result else None

    def _upsert(self, dev, pw=None):
        key = dev_key(dev)
        cur = self.devices.get(key, {"alias": "", "added": int(time.time())})
        cur.update({k: v for k, v in dev.items() if v not in (None, "")})
        if pw is not None:
            cur["pw"] = pw
        self.devices[key] = cur
        st = self.state.setdefault(key, {})
        st.pop("pw_bad", None)
        st["next_try"] = 0
        return key

    def ask_password(self, key, connect=True):
        dev = self.devices.get(key) or self._entry_dev(key)
        if not dev:
            return
        new_count = sum(1 for fid in self.found if fid not in self.devices and fid != self._me() and fid != key)
        check = (f"Pakai password ini juga untuk {new_count} PC baru lainnya (mis. semua PC warnet)"
                 if new_count else None)
        res = self._form(f"Password - {self._display_name(dev)}",
                         [(f"Password LanDesk di {self._display_name(dev)}", dev.get("pw", ""), True)], check)
        if not res or not res["values"][0]:
            return
        pw = res["values"][0]
        key = self._upsert(dict(dev), pw)
        if res["check"]:
            for fid, info in list(self.found.items()):
                if fid not in self.devices and fid != self._me():
                    self._upsert({"id": fid, "name": info.get("name", ""), "ip": info.get("ip"),
                                  "port": info.get("port", 8765), "os": info.get("os", "")}, pw)
        self._save()
        self.render()
        self._thumb_tick(reschedule=False)
        if connect:
            self.connect(key)

    def _entry_dev(self, key):
        info = self.found.get(key)
        if info:
            return {"id": key, "name": info.get("name", ""), "ip": info.get("ip"),
                    "port": info.get("port", 8765), "os": info.get("os", "")}
        return None

    def rename(self, key):
        dev = self.devices.get(key)
        if not dev:
            return
        res = self._form("Ganti nama", [(f"Nama tampilan (nama komputer: {dev.get('name', '-')})",
                                         dev.get("alias", ""), False)])
        if res is None:
            return
        dev["alias"] = res["values"][0]
        self._save()
        self.render()

    def remove(self, key):
        dev = self.devices.get(key)
        if dev and messagebox.askyesno("LanDesk", f"Hapus {self._display_name(dev)} dari dashboard?\n"
                                                  "Password yang tersimpan untuk PC ini ikut dihapus."):
            self.devices.pop(key, None)
            self.state.pop(key, None)
            self._save()
            self.render()

    def add_dialog(self):
        res = self._form("Tambah PC", [("ID (9 digit) atau alamat IP", "", False),
                                       ("Nama tampilan (opsional)", "", False),
                                       ("Password LanDesk di PC itu", "", True)])
        if not res or not res["values"][0]:
            return
        target, alias, pw = res["values"]
        kind = remote.parse_target(target)

        def work():
            try:
                if kind[0] == "id":
                    info = next((d for d in remote.discover() if d.get("id") == kind[1]), None) or {"id": kind[1]}
                    host, port = info.get("ip"), info.get("port", 8765)
                else:
                    host, port = kind[1], kind[2]
                    try:
                        info = remote.get_info(host, port)
                    except remote.RemoteError:
                        info = {}
                dev = {"id": info.get("id", ""), "name": info.get("name", ""), "ip": host, "port": port,
                       "os": info.get("os", ""), "alias": alias}
                self.q.put(("added", dev, pw))
            except Exception as e:
                self.q.put(("error", f"Gagal menambahkan: {e}"))

        self.pool.submit(work)

    # ------------------------------------------------------------- koneksi
    def connect(self, key):
        dev = self.devices.get(key)
        if not dev or not dev.get("pw"):
            self.ask_password(key, connect=True)
            return
        if self.state.setdefault(key, {}).get("connecting"):
            return
        self.state[key]["connecting"] = True
        self.app.log_var.set(f"Menghubungkan ke {self._display_name(dev)}...")
        dev_copy = dict(dev)
        if dev.get("id") in self.found:
            dev_copy["ip"] = self.found[dev["id"]].get("ip", dev_copy.get("ip"))

        def work():
            try:
                self.q.put(("connected", key, remote.connect_device(dev_copy, dev["pw"])))
            except remote.RemoteError as e:
                self.q.put(("connect_err", key, e))
            except Exception as e:
                self.q.put(("connect_err", key, remote.RemoteError(str(e))))

        self.pool.submit(work)

    def connect_typed(self):
        target, pw = self.target_var.get().strip(), self.pw_var.get()
        if not target or not pw:
            messagebox.showinfo("LanDesk", "Isi ID/IP dan password komputer tujuan.")
            return
        self.connect_btn.state(["disabled"])
        self.app.log_var.set(f"Menghubungkan ke {target}...")
        found = list(self.found.values())

        def work():
            try:
                info = remote.connect(target, pw, found)
                self.q.put(("typed_ok", info, pw))
            except remote.RemoteError as e:
                self.q.put(("typed_err", e))
            except Exception as e:
                self.q.put(("typed_err", remote.RemoteError(str(e))))

        self.pool.submit(work)

    def _remember_connected(self, info, pw):
        dev = {"id": info.get("id", ""), "name": info.get("name", ""), "ip": info.get("ip"),
               "port": info.get("port", 8765), "os": info.get("os", ""), "last_seen": int(time.time()),
               "last_connected": int(time.time())}
        key = self._upsert(dev, pw)
        self.state.setdefault(key, {})["last_ok"] = time.time()
        self._save()
        self.render()
        return key

    # ------------------------------------------------------------- background
    def _visible(self):
        try:
            return (self.root.winfo_viewable()
                    and self.app.nb.index(self.app.nb.select()) == self.app.DASH_TAB)
        except tk.TclError:
            return False

    def discover_now(self):
        if self._discovering:
            return
        self._discovering = True

        def work():
            try:
                self.q.put(("found", remote.discover()))
            except Exception as e:
                self.q.put(("found", e))

        self.pool.submit(work)

    def _discover_tick(self):
        if self.app.quitting:
            return
        self.discover_now()
        self.root.after(DISCOVER_EVERY_S * 1000, self._discover_tick)

    def _thumb_tick(self, reschedule=True):
        if self.app.quitting:
            return
        if self._visible() or not reschedule:
            now = time.time()
            for key, dev in list(self.devices.items()):
                st = self.state.setdefault(key, {})
                if (dev.get("id") == self._me() or not dev.get("pw") or st.get("pw_bad")
                        or st.get("busy") or now < st.get("next_try", 0)):
                    continue
                ip = self.found.get(dev.get("id") or "", {}).get("ip") or dev.get("ip")
                if not ip:
                    continue
                st["busy"] = True
                self.pool.submit(self._fetch, key, ip, int(dev.get("port") or 8765), dev["pw"])
        if reschedule:
            self.root.after(THUMB_EVERY_MS, self._thumb_tick)

    def _fetch(self, key, ip, port, pw):
        try:
            jpg = remote.fetch_thumb(ip, port, pw, width=320, timeout=3)
            img = fit_thumb(Image.open(io.BytesIO(jpg)))
            self.q.put(("thumb", key, img, ip))
        except remote.RemoteError as e:
            self.q.put(("thumb_err", key, e.kind, ip))
        except Exception:
            self.q.put(("thumb_err", key, "other", ip))

    def _poll(self):
        dirty = False
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "thumb":
                    _, key, img, ip = item
                    st = self.state.setdefault(key, {})
                    st.update(busy=False, last_ok=time.time(), disabled=False, next_try=0)
                    st["photo"] = ImageTk.PhotoImage(img)
                    dev = self.devices.get(key)
                    if dev is not None:
                        if dev.get("ip") != ip:
                            dev["ip"] = ip
                            self._save()
                        dev["last_seen"] = int(time.time())
                    card = self.cards.get(key)
                    if card and dev is not None:
                        self._update_card(card, key, dev, False)
                elif kind == "thumb_err":
                    _, key, err, ip = item
                    st = self.state.setdefault(key, {})
                    st["busy"] = False
                    if err == "auth":
                        st["pw_bad"] = True
                    elif err in ("disabled", "old"):
                        st.update(disabled=err, last_ok=time.time(), next_try=time.time() + OFFLINE_RETRY_S)
                    else:
                        st["next_try"] = time.time() + OFFLINE_RETRY_S
                    dirty = True
                elif kind == "found":
                    self._discovering = False
                    if isinstance(item[1], Exception):
                        continue
                    now = time.time()
                    for d in item[1]:
                        d["last_seen"] = now
                        self.found[d["id"]] = d
                        dev = self.devices.get(d["id"])
                        if dev is not None:
                            changed = dev.get("ip") != d["ip"] or dev.get("name") != d.get("name")
                            dev.update(ip=d["ip"], port=d.get("port", 8765), name=d.get("name", dev.get("name")),
                                       os=d.get("os", dev.get("os", "")), last_seen=int(now))
                            if changed:
                                self._save()
                            if d.get("preview") is False:
                                self.state.setdefault(d["id"], {})["disabled"] = True
                    dirty = True
                elif kind == "connected":
                    _, key, info = item
                    self.state.setdefault(key, {}).pop("connecting", None)
                    dev = self.devices.get(key, {})
                    self._remember_connected(info, dev.get("pw", ""))
                    how = "browser default" if info.get("browser") == "default" else "jendela viewer"
                    self.app.log_var.set(f"Terhubung ke {self._display_name(dev or info)} - dibuka di {how}")
                elif kind == "connect_err":
                    _, key, e = item
                    self.state.setdefault(key, {}).pop("connecting", None)
                    self.app.log_var.set("")
                    if e.kind == "auth":
                        self.state[key]["pw_bad"] = True
                        self.render()
                        messagebox.showwarning("LanDesk", "Password tersimpan sudah tidak cocok. Masukkan password baru.")
                        self.ask_password(key, connect=True)
                    else:
                        messagebox.showerror("LanDesk", str(e))
                elif kind == "typed_ok":
                    _, info, pw = item
                    self.connect_btn.state(["!disabled"])
                    self._remember_connected(info, pw)
                    self.target_var.set("")
                    self.pw_var.set("")
                    self.app.log_var.set(f"Terhubung ke {info.get('name', info.get('ip'))} - disimpan di dashboard")
                elif kind == "typed_err":
                    self.connect_btn.state(["!disabled"])
                    self.app.log_var.set("")
                    messagebox.showerror("LanDesk", str(item[1]))
                elif kind == "added":
                    _, dev, pw = item
                    self._upsert(dev, pw)
                    self._save()
                    dirty = True
                    self._thumb_tick(reschedule=False)
                    self.app.log_var.set(f"{dev.get('alias') or dev.get('name') or dev.get('ip')} ditambahkan")
                elif kind == "error":
                    messagebox.showerror("LanDesk", item[1])
        except queue.Empty:
            pass
        if dirty:
            self.render()
        if not self.app.quitting:
            self.root.after(250, self._poll)

    def shutdown(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
