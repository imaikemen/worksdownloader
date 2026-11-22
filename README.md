<p align="center">
  <b>English</b> | <a href="./README_CN.md">简体中文</a>
</p>

# worksdownloader V1.0.1

A Windows desktop tool for locally archiving public works, built for creators and individual learners. It supports cataloging, original-quality saving, and local categorized management of works from **Douyin, Xiaohongshu (both `xiaohongshu.com` and `rednote.com`), and Weibo**.

> **Getting Started**: For complete installation, initial path setup, cataloging & downloading, favorite creator management, and troubleshooting, please refer to the [**Detailed Usage Guide (USAGE.md)**](./USAGE.md).

---

## Disclaimer (Must Read Before Use)

> **Important Notice:**
> 1. **Compliant Use Only**: This software is strictly for **personal learning, technical research, and creators backing up their own public works**. It **never bypasses any platform's paywalls, memberships, or privacy permissions**.
> 2. **Respect Copyright**: Please strictly comply with local laws, regulations, and each platform's Terms of Service. Respect the original authors' copyrights, portrait rights, and privacy rights. Commercial use, public redistribution, or any infringing behavior is strictly prohibited.
> 3. **24-Hour Infringement Deletion**: If any saved content involves infringement, breach of contract, or an objection from the copyright holder, the user must permanently delete the relevant files within **24 hours**.
> 4. **Use at Your Own Risk**: Users assume all legal responsibilities and risks arising from the use of this software or the saved content. The software author bears no joint or several liability.

Every time the software launches, a mandatory 5-second disclaimer pop-up will appear. You must check "Agree" to proceed; selecting "Disagree" will immediately and safely exit the program.

---

## Core Features

- **Multi-Platform Original-Quality Archiving (Douyin / Xiaohongshu / Weibo)**
  - Supports batch cataloging of creator profiles and single-work link parsing; public content on Douyin and Xiaohongshu supports **login-free cataloging**.
  - Weibo strictly catalogs only original images, Live Photos, and videos posted by the author, automatically filtering out likes and non-original feeds.
  - Xiaohongshu supports both the domestic domain `www.xiaohongshu.com` and the international domain `www.rednote.com`.
  - Supports detailed previewing via "View Media" for single works and selective downloading by individual file, with built-in original resolution verification.
- **Favorite Creator Card Management**
  - Supports unlimited pinning, custom sorting via long-press / drag handles, adding notes, and one-click update checks.
  - Supports fuzzy search starting from a single character (quick scroll-to-locate by nickname / ID / account / notes).
  - Properly displays hotlink-protected avatars (such as Weibo) via local proxy caching.
- **Work Search & Jump by Date**
  - Supports real-time keyword search by caption/text in the work list; clicking an item automatically scrolls to it with a yellow highlight.
  - Supports a Year / Month / Day calendar picker to jump directly to works published on a specific date.
- **Unified Save Path & Persistent History Across Restarts**
  - Automatically detects available drives on your PC and sets the default save path to `<Drive>:\WorksDownloader` (prioritizing the first non-system fixed drive on Windows, e.g., `D:\WorksDownloader`, or falling back to `C:\WorksDownloader` if only one drive exists). **On first launch, you can confirm the default path with one click or customize the drive and folder.**
  - Automatically creates subdirectories when downloading: `<Selected Directory>\<Platform>\<Creator Nickname (Account)>` (Platform names: `抖音` / `小红书` / `微博`).
  - Even after changing directories or restarting repeatedly, the local SQLite download history fully remembers downloaded works and their corresponding folders.

---

## Security & Transparent Build (Regarding Windows SmartScreen)

Because this is an independent open-source project without a commercial EV code signing certificate, Windows may display a **Windows Defender SmartScreen "Unknown Publisher"** warning the first time you run the installer or portable EXE.

To ensure transparency and independent verification, this repository takes the following measures:

1. **Fully Open Source**: All frontend/backend source code and packaging scripts are publicly available in this repository, with no closed-source modules or personal privacy data.
2. **Automated GitHub Actions Builds**: All `Setup.exe` installers and `Portable.exe` builds published in GitHub Releases are automatically compiled on GitHub's official `windows-latest` runner via [`.github/workflows/release.yml`](./.github/workflows/release.yml), which also generates the `SHA256SUMS.txt` checksum file.
3. **Reproducible Local Builds**: You can also clone the repository and run `build_exe.bat` on your own machine to build it locally.

After downloading, you can verify the file hash via PowerShell:

```powershell
Get-FileHash .\作品鉴赏downloader-V1.0.1-Setup.exe -Algorithm SHA256
```

---

## Running from Source & Local Packaging

### Requirements

- Windows 10 / 11 (64-bit, with built-in Microsoft Edge WebView2)
- Python 3.11+
- Node.js 20+ (required to prepare the Xiaohongshu parsing runtime)

### Development Mode

```powershell
python -m pip install -r requirements.txt
python scripts/prepare_runtime.py
python server.py
```

### One-Click Single-File EXE Build

```powershell
.\build_exe.bat
```

Once built, the output will be located at `dist\作品鉴赏downloader-V1.0.1.exe`. If you have Inno Setup 6 installed locally, you can run `iscc installer\setup.iss` to generate the Windows installer.

### Running Tests & Privacy Audit

```powershell
python scripts/privacy_audit.py
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## License

This project is open-sourced under the **GNU General Public License v3.0 (GPL-3.0)**. See [LICENSE](./LICENSE) and [THIRD_PARTY_LICENSES.md](./THIRD_PARTY_LICENSES.md) for details.
