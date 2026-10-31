<div align="center">

<img src="icon.png" width="96" alt="Media Downloader">

# Media Downloader

**Download video and audio from YouTube, VK, RuTube and hundreds of other sites — in one click. No ads, no limits, no sign-up.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)](#-requirements)
[![Powered by yt-dlp](https://img.shields.io/badge/powered%20by-yt--dlp-red)](https://github.com/yt-dlp/yt-dlp)
[![Release](https://img.shields.io/github/v/release/TTsanchez/media-downloader?label=release)](../../releases/latest)

[Download](#-download) · [Features](#-features) · [Sites](#-supported-sites) · [Usage](#-usage) · [Русский](README.md)

</div>

---

Media Downloader is a simple desktop app for Windows. It runs entirely locally: nothing is sent to third-party servers, there are no ads and no download limits. Powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp), so hundreds of sites are supported.

![Main window](docs/screenshots/01-format.png)

## ✨ Features

- 🎬 **Best quality** — video + audio merged into MP4
- 🎵 **Audio only** — mp3, m4a, opus, flac, wav with bitrate selection; cover art and tags embedded
- 🎼 **Music links** — YouTube Music, SoundCloud, Bandcamp, Mixcloud and Audiomack are saved as audio
- 🔗 **Clean links** — a video copied from a Mix/Radio downloads alone, not the whole playlist
- 📼 **Video only** — no sound track
- 📚 **Playlists** — full or by index range
- 🎯 **Format table** — pick the exact resolution, codec, FPS and size
- 📋 **Download queue** — many links at once, 1–4 threads, with thumbnails
- 🖼 **Preview** — cover art, channel and duration before downloading
- 🕘 **Download history** — recent files with previews and one-click repeat
- 🔔 **Windows notifications** — a message when a file is ready and the window is in the background
- 🎨 **Dark theme** — tidy cards, a single accent colour and clear states
- ⚡ **Shorts** and regular videos
- 📊 **Progress** — percent, speed, ETA, file size
- ⏸ **Pause and resume** — continues from where it stopped
- ⏹ **Cancel** in-flight downloads
- 🍪 **Cookies** — for age-restricted and private videos
- 🔁 **Auto-retry** — up to 3 attempts with different strategies
- 🗂 **Download archive** — skips files you already downloaded
- 🔄 **yt-dlp updater** — update from inside the app
- ⌨️ **Hotkeys** and clipboard link auto-fill
- 💾 **Remembers settings** between launches (including window size and position)
- 📦 **Portable `.exe`** — no Python required

## 🌍 Supported sites

Thanks to yt-dlp the app works beyond YouTube. Popular platforms:

| Video | Audio and social |
|---|---|
| YouTube, Shorts, VK, RuTube | SoundCloud, Bandcamp |
| Dailymotion, Vimeo, Twitch | TikTok, Instagram\*, Twitter/X |
| Odnoklassniki, RuTube Live | Reddit, Bilibili |

<sub>\* Some sites need cookies or may be unavailable in certain regions.</sub>

Full list: [yt-dlp supported sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) (1000+).

> **The project focuses on YouTube.** Cookies, PO Token and auto-retry are built for it. Other sites use yt-dlp's standard pipeline.

## 📥 Download

### Option 1. Ready-made .exe (easiest)

1. Open the [**Releases**](../../releases/latest) page
2. Download `Media-Downloader.exe`
3. Run it — no installation needed

> Windows SmartScreen may warn about an unknown publisher because the binary is not code-signed. Click **More info → Run anyway** — but first compare the checksum from the section below.

### Verifying the download

Every release ships `SHA256SUMS.txt` with the SHA-256 of the file built by GitHub Actions from this repository. Compare:

```powershell
Get-FileHash .\Media-Downloader.exe -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

The hashes must match. With [GitHub CLI](https://cli.github.com/) you can also verify build provenance:

```powershell
gh attestation verify .\Media-Downloader.exe --repo TTsanchez/media-downloader
```

Download the `.exe` only from this repository's [Releases page](../../releases/latest): copies elsewhere may have been modified.

### Option 2. From source

Requires [Python 3.10+](https://www.python.org/downloads/) (check **Add Python to PATH** during install).

```bat
git clone https://github.com/TTsanchez/media-downloader.git
cd media-downloader
start.bat
```

`start.bat` creates the virtual environment, installs dependencies and launches the app.

<details>
<summary>Manual launch</summary>

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```
</details>

## 🚀 Usage

1. **Paste a link** to a video, audio track or playlist.
2. Click **Get info** — title, cover art, channel and duration appear.
3. On the **Format** tab choose what to download: best quality, video only, or audio only. A **format table** lets you pick an exact resolution, codec and size; a **quality preset** (1080p, 720p, etc.) simply caps the resolution.
4. Click **Download**. Progress is shown at the bottom, and the **Log** button expands the detailed log; **Pause** stops the download so it can resume from the same point.
5. The file is saved to your download folder (**Folder** button). A completion window offers **Open folder**; if the app is in the background, Windows shows a notification instead.

<div align="center">
<img src="docs/screenshots/03-settings.png" width="49%" alt="Settings">
<img src="docs/screenshots/02-playlist.png" width="49%" alt="Playlist">
</div>

### 📋 Download queue

![Download queue](docs/screenshots/05-queue.png)

The **Queue** tab downloads many links at once:

1. Paste links (one per line) or use **From file...** / **Paste**. You can also drop a `.txt` with links onto the app icon — the queue picks it up on launch.
2. Pick the number of threads (1–4) and click **Add to queue**.
3. Click **Download all** — each link shows status and percent. **Pause** stops the queue so you can continue later; **Stop** cancels it. Queue controls always sit in the bottom bar and stay visible without scrolling.

The queue persists between launches: unfinished links come back the next time you open the app.

### 🕘 Download history

![Download history](docs/screenshots/04-history.png)

The **History** tab lists recent downloads with a cover, title, channel, duration, format and size. Each entry has **Repeat**, **Folder** and **Delete** buttons; the whole history can be cleared at once, while the files themselves stay on disk.

The history is stored locally in `%LOCALAPPDATA%\Media-Downloader\history.json` and keeps up to 300 entries.

### 🔔 Notifications

When a download finishes while the app window is inactive, Windows shows a notification with the file name. Turn it off in **Settings → Notifications and previews**.

### ⌨️ Hotkeys

| Keys | Action |
|---|---|
| `Enter` / `F5` | Get info for the link |
| `Ctrl+D` | Start download |
| `Esc` | Stop download |
| `Ctrl+O` | Open download folder |

If the link field is empty and your clipboard holds a link, the app fills it in automatically.

## 🗂 Download archive

Enable **Settings → Download archive → Skip already downloaded** — the app remembers what it downloaded and won't download the same links or playlist twice. The list is stored in a separate file (path is configurable).

## 🧩 Requirements

**The ready-to-run `.exe` is a compact build (~22 MB).** It installs everything it needs on first launch into `%LOCALAPPDATA%\Media-Downloader`; no administrator rights required:

- yt-dlp, yt-dlp-ejs and the PO Token plugin — from PyPI;
- FFmpeg and ffprobe — a static Windows build.

The first launch takes 1–2 minutes and shows a progress window. After that the app opens immediately. If something was removed, restore components via **Settings → Components**.

Heavier components (Deno, Node.js, Git and PO Token) are installed on demand with **"Install all"** — they are only needed for protected YouTube videos.

`build_exe.bat` produces the same compact build: FFmpeg and yt-dlp are not bundled into the `.exe`.

## 🍪 Cookies

Regular public videos do **not** need cookies — the app downloads them right away. yt-dlp picks the YouTube client itself: when logged out it uses the anonymous `visionos` client, which requires no cookies, no PO token and no JavaScript runtime.

Cookies are only needed when YouTube asks to "confirm you're not a bot", and for age-restricted, private or account-bound videos.

**Option 1 — read directly from the browser.** Settings → Cookies → pick a browser.

> **Chrome/Edge 127+ note.** Google switched to app-bound cookie encryption; the key is tied to the browser and cannot be read externally. Closing the browser does **not** help. Firefox cookies are readable.

**Option 2 — `cookies.txt` file (always works).**
1. Install the **Cookie-Editor** extension in your browser
2. Open youtube.com while signed in
3. Export cookies in Netscape format (`.txt`)
4. Settings → Cookies → **Load**

Cookies never leave your machine — they are read locally and used only for requests to the site.

## 🧱 YouTube components

Since 2025 YouTube requires a **PO Token** and a **JavaScript challenge**. Without them many videos fail (`The page needs to be reloaded`). The app installs everything itself: **Settings → Components**.

| Component | Purpose | How it installs |
|---|---|---|
| **yt-dlp** | Downloads video | Automatically on first launch |
| **FFmpeg** | Merges video with audio | Automatically on first launch |
| **Deno** | Runs YouTube's JS challenge | "Install all" button |
| **Node.js and Git** | Needed to build the PO Token server | "Install all" button |
| **PO Token server** | Generates access tokens | "Install all" button |

Everything installs portably under `%LOCALAPPDATA%\Media-Downloader\tools` — no installers, winget or administrator rights. The PO Token server is the local Node.js program [bgutil](https://github.com/Brainicism/bgutil-ytdlp-pot-provider). It is built once, starts automatically on `127.0.0.1:4416` and stops on exit. Nothing is uploaded.

## 🌐 Network and YouTube blocking (VPN / proxy)

In some countries (e.g. Russia) YouTube is blocked by the ISP **by domain name (SNI/DPI)**: the internet works, `google.com` opens, but YouTube does not. The connection is dropped before encryption, so **no** local downloader can work — it is not a bug in this app.

Check it via **Settings → Network and proxy → Check network**.

- YouTube reachable directly — the app downloads on its own.
- "domain-name blocking (SNI/DPI)" — you need a **VPN** or **proxy**.

To use a proxy, enter its address in the **Proxy** field (saved to config):

| Example | Description |
|---|---|
| `http://host:port` | HTTP/HTTPS proxy |
| `socks5://host:port` | SOCKS5 proxy |
| `http://user:pass@host:port` | Proxy with auth |

Empty means a direct connection. The local PO Token server (`127.0.0.1`) always bypasses the proxy.

## Diagnostic reports

Click **Save report** in the bottom toolbar and attach the ZIP to an issue or send it to support, together with the steps that caused the problem. Logs persist under `%LOCALAPPDATA%\Media-Downloader\logs`, including previous sessions. If the window cannot open, retrieve the log files from that directory.

Reports include application and component versions, job IDs, attempts and progress, Python tracebacks, Tk callback and background-thread errors, yt-dlp messages, and exit codes and recent output from child processes. Python-supported native faults are recorded through `faulthandler`; the updater script has its own log.

Regular logs rotate at 2 MiB with three backups. Up to 20 files are retained across sessions; exports include up to 12 recent files and the last 4 MiB of each. Cookies, configuration files, environment variables and downloaded media are excluded. Authorization headers, registered passwords and cookie values, URL parameters and the user's home path are redacted before writing regular logs and when exporting all logs, including native traces. Reports are never uploaded automatically.

Power loss, forced termination and failures before Python starts cannot be intercepted; only the last written events remain. If local application storage is unavailable, logs fall back to temporary storage, then to bounded memory which must be exported before closing.

## 🛠 Troubleshooting

| Message | Cause and fix |
|---|---|
| `Connection reset` / `Connection aborted` / TLS timeout | Your network blocks YouTube (SNI/DPI). Use a VPN or proxy |
| `Failed to decrypt with DPAPI` | Chrome/Edge 127+ cookie protection. Use `cookies.txt` from Cookie-Editor |
| `Could not copy cookie database` | Browser is open — close it completely |
| `Sign in to confirm you're not a bot` | Cookies needed. If it persists, your IP may be temporarily blocked by YouTube |
| `The page needs to be reloaded` | No PO Token — install Deno and PO Token on the Settings tab |
| `Confirm your age` | Age-restricted — cookies needed |
| `This video is private` | You need the invite link |
| `Unsupported URL` | Site not supported or bad link. Check yt-dlp's supported sites |
| `Video unavailable` | Removed or not available in your region |

## ⬆️ Updating

Sites change their protections occasionally, so it pays to keep yt-dlp fresh. The app has it built in: **Settings → yt-dlp update**.

- **Check for updates** — shows the installed and the latest version.
- **Update yt-dlp** — downloads a fresh version into the components folder. Works in the packaged `.exe` too; no need to reinstall the app. Changes apply after restart.

On startup the app quietly checks for a new version and logs it.

Manually (from source):

```bat
py -m pip install -U yt-dlp
```

## 👨‍💻 Development

```bat
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Build a single `.exe` (output in `dist\Media-Downloader.exe`):

```bat
build_exe.bat
```

The script then runs the `--selftest` check: it verifies the version, bundled files, module imports, component folder and Tk, and writes a report to `%LOCALAPPDATA%\Media-Downloader\selftest.txt`.

| File | Purpose |
|---|---|
| `main.py` | GUI (tkinter + ttkbootstrap) |
| `bootstrap.py` | Install order and the first-run window |
| `components.py` | Installs and checks yt-dlp, FFmpeg, Deno, Node.js, Git, PO Token |
| `selftest.py` | Network-free self-test of the built `.exe` |
| `net.py` | Downloads with resume, retries and SHA-256 checks |
| `downloader.py` | yt-dlp wrapper, formats, retries |
| `utils.py` | Paths, FFmpeg, browsers, cookies, network checks |
| `pot.py` | Builds and runs the PO Token server |
| `verify_encoding.py` | Source encoding check (guards against mojibake) |

## ⚖️ Legal notice

Downloading content without the rights holder's permission violates the sites' Terms of Service. Use this app only for material you have the right to: your own videos, Creative Commons, public domain, or content you have explicit permission to use.

## 📄 License

[MIT](LICENSE) © 2026 TTsanchez
