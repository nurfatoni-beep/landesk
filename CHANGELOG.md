# Changelog

Semua perubahan penting LanDesk dicatat di sini. Format mengikuti
[Keep a Changelog](https://keepachangelog.com/id-ID/1.1.0/), versi mengikuti
[Semantic Versioning](https://semver.org/lang/id/).

## [Belum dirilis]

## [1.3.0] - 2026-11-23

### Ditambahkan
- Update otomatis dari GitHub Releases: cek 20 detik setelah start lalu tiap 6 jam, bar
  *Pasang & restart* / *Nanti*, paket update ±50 KB dengan verifikasi SHA-256, kembali otomatis ke
  versi bawaan kalau versi baru gagal start.
- Opsi `update_feed` untuk mirror update di server LAN.
- Rilis otomatis lewat GitHub Actions (zip & setup Windows, `.deb`, paket update).

## [1.2.0] dan sebelumnya

Versi sebelum repo ini dipublikasikan (dipasang manual, belum ada update otomatis):
- Dashboard ala aplikasi pemantau warnet: kartu semua PC dengan pratinjau layar live, status
  online/offline, ganti nama, filter, password bersama untuk PC baru.
- Satu aplikasi untuk client dan host (seperti RustDesk): ID 9 digit + password, pencarian
  otomatis di LAN, viewer di jendela sendiri.
- Transfer file (termasuk drag & drop), sinkron clipboard teks, pilih monitor, minta persetujuan
  koneksi masuk, mode hanya lihat, system tray, jalan otomatis saat login, satu instance per PC.
- Capture DXGI di Windows, tile JPEG yang hanya mengirim bagian layar yang berubah, flow control ACK.
- Installer Windows berbasis script yang lolos Smart App Control, dan paket `.deb`.

[Belum dirilis]: https://github.com/nurfatoni-beep/landesk/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/nurfatoni-beep/landesk/releases/tag/v1.3.0
