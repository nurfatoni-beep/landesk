# LanDesk — Remote Desktop LAN (Windows & Linux)

Remote desktop ringan ala RustDesk, **khusus jaringan lokal** (tanpa server relay, tanpa
internet). Satu aplikasi yang sama dipasang di semua komputer: bisa **mengontrol komputer lain**
sekaligus **dikontrol** pakai ID + password.

```
 [LanDesk A]  -- UDP 8766: "siapa saja yang ada?" -->  [LanDesk B]   (pencarian otomatis)
 [LanDesk A]  -- POST /api/token (password) ------->   [LanDesk B]   (token sekali pakai)
 [Viewer A ]  == WebSocket TCP 8765 ================   [LanDesk B]   layar (JPEG tile) / input / clipboard
 [Viewer A ]  -- HTTP /api/files, upload/download -->  [LanDesk B]   transfer file
```

## Fitur
- **Dashboard** (gaya aplikasi pemantau warnet / lab komputer): semua PC dalam satu layar
  dengan **pratinjau layar live**. Isinya PC yang pernah terhubung, ditambahkan manual, atau
  ditemukan otomatis di LAN.
  - Tiap kartu: status online/offline, nama, ID, IP, dan kapan terakhir terlihat.
  - **Klik dua kali = langsung konek** dengan password tersimpan, tanpa perlu ingat ID.
  - PC dikenali lewat ID, jadi IP yang berubah (DHCP) diperbarui otomatis.
  - PC baru cukup diberi password sekali. Ada opsi *pakai password ini untuk semua PC baru*,
    cocok untuk warnet yang semua PC-nya memakai password sama.
  - Klik kanan kartu: ganti nama (mis. "PC-01", "Kasir"), ubah password tersimpan, salin ID,
    atau hapus. Ada juga kolom filter untuk mencari PC.
- **ID 9 digit + password** per komputer (seperti RustDesk). Password bisa diganti atau dibuat acak.
- **Pencarian otomatis di LAN**: komputer lain yang menjalankan LanDesk langsung muncul. Bisa
  juga konek pakai ID atau alamat IP; yang berhasil otomatis tersimpan di dashboard.
- **Viewer** terbuka di jendela sendiri (Edge/Chrome mode aplikasi, profil terpisah). Fitur viewer:
  - pilih monitor (termasuk gabungan semua monitor), kualitas, FPS, dan skala
  - fullscreen dengan Keyboard Lock (Alt+Tab, Win, Esc ikut terkirim)
  - menu tombol khusus: Win, Alt+Tab, Alt+F4, Ctrl+Shift+Esc, Win+D/E/R, Print Screen, dll
- **Sinkron clipboard teks** dua arah, otomatis.
- **Transfer file**: jelajah folder host, unduh file, kirim file (tombol atau **drag & drop ke
  layar remote**). Default ke folder Downloads host.
- **Keamanan**: token sekali pakai (password tidak lewat URL), opsi **minta persetujuan** di
  host untuk tiap koneksi masuk (otomatis ditolak dalam 40 detik), mode **hanya lihat**,
  transfer file dan clipboard bisa dimatikan, percobaan login gagal diperlambat dan tercatat.
  Pratinjau dashboard butuh password host, bisa dimatikan, dan **otomatis mati** kalau host
  memakai *minta persetujuan*.
- **System tray**, notifikasi koneksi masuk / file diterima, jalan otomatis saat login.
  LanDesk hanya berjalan satu kali per komputer; membuka shortcut lagi memunculkan jendela yang
  sudah ada.
- Tanpa aplikasi pun bisa: buka `http://<ip-host>:8765` di browser apa saja (HP/tablet juga).

## Cara pakai
1. Pasang dan buka **LanDesk** di semua komputer (satu LAN).
2. Di komputer yang mau dikontrol: tab **Komputer ini** menampilkan **ID** dan **password**.
3. Di komputer operator: tab **Dashboard**. PC lain muncul otomatis sebagai kartu "PC baru".
   Klik dua kali, isi password sekali, dan PC itu tersimpan dengan pratinjau live. Berikutnya
   cukup klik dua kali kartunya.
4. Viewer terbuka. Gerakkan mouse ke **tepi atas** untuk memunculkan toolbar (📌 untuk menempelkan
   toolbar).

**Contoh untuk warnet:** samakan password LanDesk di semua PC billing/client (tab *Komputer ini* →
ketik password → *Simpan*). Di PC operator, klik dua kali satu kartu "PC baru", isi password,
lalu centang *pakai password ini untuk semua PC baru*. Semua PC langsung tampil dengan pratinjau.
Ganti nama kartu sesuai nomor meja lewat klik kanan → *Ganti nama*.

## Kenapa cepat
- **Hanya bagian layar yang berubah** yang dikirim (tile 64×64, digabung jadi persegi panjang).
- Windows: capture lewat **DXGI Desktop Duplication** (`dxcam`), ±2–3 ms per frame dan
  langsung tahu kalau layar diam (0 byte terkirim). Linux: `mss` (XCB + shared memory).
- JPEG via libjpeg-turbo (`simplejpeg`) kalau tersedia, selain itu Pillow.
- **Flow control berbasis ACK**: maksimal 2 frame dalam perjalanan, jadi tidak ada antrian frame basi.
- Decode JPEG di browser memakai `createImageBitmap` (paralel, hardware).

Hasil tes di laptop pengembangan (1080p): 21–30 fps saat seluruh layar berubah terus, decode
1–5 ms, layar diam ±0 byte.

## Instalasi

File di folder `dist/`:

| File | Untuk |
|---|---|
| `LanDesk-1.3.0-win64-portable.zip` | Windows 10/11: ekstrak, lalu double-click `Install.cmd`. **Disarankan** (lolos Smart App Control) |
| `LanDesk-Setup-1.3.0-win64.exe` | Windows tanpa Smart App Control (installer biasa) |
| `landesk_1.3.0_all.deb` | Ubuntu 22.04+ / Debian 12+ / Mint 21+ |

**Windows (zip + Install.cmd)**: memasang ke `%LOCALAPPDATA%\Programs\LanDesk` tanpa admin,
membuat shortcut Start Menu/Desktop, dan menambahkan entri di *Settings → Apps* untuk
uninstall. Klik kanan → *Run as administrator* supaya rule firewall (TCP 8765 + UDP 8766) dibuat
otomatis; kalau tidak, klik *Allow* saat Windows Firewall bertanya.

**Windows (setup.exe)**: installer belum ditandatangani digital, jadi SmartScreen menampilkan
"Windows protected your PC" → *More info* → *Run anyway*. Di PC dengan **Smart App Control**,
setup.exe diblokir; pakai zip di atas. Solusi permanennya: tanda tangani dengan sertifikat code
signing tepercaya (mis. Azure Trusted Signing).

> Catatan teknis: paket Windows **tidak berisi exe buatan sendiri**. Aplikasi dijalankan oleh
> `runtime\pythonw.exe`, salinan Python resmi yang ditandatangani Python Software Foundation,
> dengan library dari PyPI di `lib\` dan kode di `app\`. Viewer memakai Microsoft Edge yang
> sudah ada di Windows.

**Ubuntu**:
```bash
sudo apt install ./landesk_1.3.0_all.deb
sudo ufw allow 8765/tcp && sudo ufw allow 8766/udp     # kalau ufw aktif
```
Buka **LanDesk** dari menu aplikasi. Viewer memakai Chromium/Chrome/Edge kalau terpasang
(fallback: browser default, tanpa sinkron clipboard otomatis). Sinkron clipboard butuh `xclip`
(otomatis terpasang sebagai *Recommends*). Uninstall: `sudo apt remove landesk`.
> Butuh sesi **Xorg** untuk dikontrol (pilih *Ubuntu on Xorg* di layar login); Wayland belum didukung.

### Config
- Windows: `%APPDATA%\LanDesk\config.json`, Linux: `~/.config/landesk/config.json`
- Berisi ID, password, pengaturan, riwayat, dan password tersimpan untuk komputer lain
  (simpan hanya di komputer pribadi; hapus lewat tombol *Hapus dari riwayat*).

## Mode tanpa GUI (server / headless)
```bash
landesk --cli                              # Linux
runtime\python.exe app\server.py --cli     # Windows, dari folder instalasi
systemctl --user enable --now landesk      # Linux: service otomatis (memakai --cli)
```

## Opsi command line
| Opsi | Default | Keterangan |
|---|---|---|
| `--cli` | – | Tanpa GUI, tampilkan ID/alamat/password di terminal |
| `--minimized` | – | GUI: mulai tersembunyi di system tray (dipakai autostart) |
| `--password` | dari config | Password akses (atau env `LANDESK_PASSWORD`) |
| `--save` | – | Simpan opsi yang diberikan ke config |
| `--reset-password` | – | Buat password acak baru dan simpan |
| `--port` | 8765 | Port HTTP/WebSocket |
| `--monitor` | primary | 1 = monitor ke-1, 2 = monitor ke-2, 0 = semua monitor jadi satu |
| `--quality` / `--fps` / `--scale` | 70 / 30 / 1.0 | Kualitas JPEG, batas FPS, skala resolusi |
| `--view-only` | – | Hanya lihat, tanpa kontrol |
| `--no-dxgi` | – | Windows: paksa capture GDI |

## Update otomatis

Sejak 1.3.0, LanDesk mengecek **GitHub Releases** repo ini 20 detik setelah start, lalu tiap
6 jam. Kalau ada versi baru:
- Muncul bar kuning *"Update LanDesk x.y.z tersedia"* dan notifikasi tray.
- **Pasang & restart**: mengunduh paket kecil (±50 KB, kode aplikasi saja), memverifikasi
  SHA-256, lalu memulai ulang. Tidak butuh admin/sudo; file disimpan di
  `%LOCALAPPDATA%\LanDesk\updates` (Windows) atau `~/.local/share/landesk/updates` (Linux).
- **Nanti**: versi itu tidak ditawarkan lagi sampai ada versi yang lebih baru.
- Kalau versi baru gagal start, LanDesk otomatis kembali ke versi bawaan installer.
- Kalau ada perubahan library (`requirements.txt`), bar akan meminta pasang ulang paket
  lengkap dari halaman rilis.

Bisa dimatikan di *Pengaturan → Aplikasi*. Untuk warnet tanpa akses GitHub, isi `update_feed`
di config dengan URL `latest.json` di server LAN (buat dengan
`python packaging/make_update.py --base-url http://server-lan/landesk`).

### Cara merilis versi baru
1. Ubah kode, lalu naikkan `__version__` di `server.py` (mis. `1.3.1`).
2. Commit dan push tag:
   ```bash
   git commit -am "Perbaikan ..."
   git tag v1.3.1
   git push origin main --tags
   ```
3. GitHub Actions (`.github/workflows/release.yml`) mem-build zip & setup Windows, `.deb`, paket
   update, dan `latest.json`, lalu membuat Release. Semua PC akan menawarkan update.

## Build installer sendiri
```powershell
# Windows -> dist\LanDesk-Setup-*.exe + portable zip
# (butuh Python 3.10+ 64-bit dari python.org, karena runtime-nya ikut dibundel, & Inno Setup 6)
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```
```bash
# .deb (bisa dijalankan di Windows maupun Linux, butuh Python + pip + Pillow)
python packaging/linux/build_deb.py
```

## Keamanan
- Didesain untuk **LAN tepercaya**. Lalu lintas **tidak dienkripsi** (HTTP/WebSocket biasa);
  jangan di-expose ke internet. Untuk akses dari luar, pakai VPN (WireGuard/Tailscale).
- Pencarian otomatis hanya menjawab info dasar (ID, nama komputer, OS, port), bukan password.
- Pakai password yang tidak mudah ditebak, atau aktifkan *Minta persetujuan saya* di Pengaturan.

## Batasan yang diketahui
- Windows: layar UAC / Ctrl+Alt+Del (secure desktop) tidak bisa di-capture/dikontrol.
- Pencarian otomatis memakai broadcast dan mengasumsikan subnet /24; kalau beda subnet/VLAN,
  konek pakai alamat IP.
- Belum ada audio, chat, dan transfer gambar lewat clipboard (hanya teks).
- Kursor host tidak digambar; yang terlihat kursor lokal (posisinya sama).
