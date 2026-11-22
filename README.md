<details>
<summary><b>📖 点击展开简体中文介绍 (Click to expand Simplified Chinese)</b></summary>

# 作品鉴赏downloader V1.0.1

面向创作者与个人学习者的 Windows 本地公开作品归档桌面工具，支持 **抖音、小红书（国内版 `xiaohongshu.com` 与海外版 `rednote.com`）、微博** 三个平台的作品盘点、原画质保存与本地分类管理。

> **新手上手**：完整的安装、首次路径设置、盘点下载、常看作者管理与常见问题排查，请查看 [**《详细使用指南 (USAGE.md)》**](./USAGE.md)。

---

## 免责声明（使用前必读）

> **重要提示：**
> 1. **仅限合规用途**：本软件仅用于**个人学习、技术研究与创作者备份自有公开作品**，**绝不绕过任何平台的付费、会员或私密权限**。
> 2. **尊重原创版权**：请严格遵守所在地区法律法规与各平台用户协议，尊重原作者的著作权、肖像权与隐私权，严禁用于任何商业用途、公开二次分发或侵权行为。
> 3. **24 小时侵权删除**：如所保存的内容涉及侵权、违约或版权方提出异议，请使用者于 **24 小时内** 自行彻底删除相关文件。
> 4. **责任自负**：因使用本软件或所保存内容产生的一切法律责任与风险均由使用者自行承担，软件作者不承担任何连带责任。

软件每次启动均会弹出 5 秒强制阅读免责声明弹窗，须勾选同意后方可进入使用；选择不同意将立即安全退出。

---

## 核心功能

- **多平台原画质归档（抖音 / 小红书 / 微博）**
  - 支持作者主页批量盘点与单个作品链接解析；抖音与小红书公开内容支持「免登录盘点」。
  - 微博严格只盘点作者本人原创发布的图片、实况图（Live Photo）和视频，自动过滤点赞与非本人动态。
  - 小红书同时兼容国内域名 `www.xiaohongshu.com` 与海外域名 `www.rednote.com`。
  - 支持单作品「查看媒体」精细预览与按张勾选下载，内置原画质分辨率校验。
- **常看作者卡片管理**
  - 支持无限名额图钉置顶、长按卡片 / 拖拽手柄自定义排序、添加备注与一键检查更新。
  - 支持单字符起模糊搜索（按昵称 / ID / 账号 / 备注快速滚动定位）。
  - 微博等防盗链头像通过本地代理缓存正常展示。
- **作品检索与按日期跳转**
  - 作品列表支持按文案关键词实时搜索，点击条目自动滚动并黄光高亮定位。
  - 支持按年 / 月 / 日日历选择器精确跳转到对应发布日期的作品。
- **统一保存路径与跨重启历史记忆**
  - 默认保存路径自动探测当前电脑可用磁盘，设为 `<盘符>\WorksDownloader`（Windows 优先首个非系统固定盘如 `D:\WorksDownloader`，若仅有一个盘则回退 `C:\WorksDownloader`），**首次启动时可一键确认默认路径或自定义盘符与文件夹**。
  - 下载时自动建立二级目录：`<选定目录>\<平台名>\<作者昵称（账号）>`（平台名：`抖音` / `小红书` / `微博`）。
  - 更换目录或反复重启后，SQLite 本地下载历史仍完整记忆已下载作品及其归属目录。

---

## 安全与透明构建说明（关于 Windows SmartScreen）

由于个人开源项目未购买商业 EV 代码签名证书，首次在 Windows 上运行安装包或便携版 EXE 时，系统可能会弹出 **Windows Defender SmartScreen「未知发布者」** 提示。

为确保公开透明、可独立验证，本仓库采取以下措施：

1. **完整开源**：全部前后端源码与打包脚本均在本仓库公开，无任何闭源模块或个人隐私数据。
2. **GitHub Actions 自动构建**：所有 GitHub Releases 发布的 `Setup.exe` 安装包与 `Portable.exe` 便携版均由 GitHub 官方 `windows-latest` 托管环境通过 [`.github/workflows/release.yml`](./.github/workflows/release.yml) 自动构建并生成 `SHA256SUMS.txt` 校验文件。
3. **可本地自行复现**：你也可以直接克隆源码，在自己电脑上运行 `build_exe.bat` 本地构建。

下载后可通过 PowerShell 核对校验值：

```powershell
Get-FileHash .\作品鉴赏downloader-V1.0.1-Setup.exe -Algorithm SHA256
```

---

## 从源码运行与本地打包

### 环境要求

- Windows 10 / 11（64 位，系统已内置 Microsoft Edge WebView2）
- Python 3.11+
- Node.js 20+（用于准备小红书解析运行库）

### 开发模式运行

```powershell
python -m pip install -r requirements.txt
python scripts/prepare_runtime.py
python server.py
```

### 一键构建单文件 EXE

```powershell
.\build_exe.bat
```

构建完成后输出位于 `dist\作品鉴赏downloader-V1.0.1.exe`。若本地已安装 Inno Setup 6，可执行 `iscc installer\setup.iss` 生成 Windows 安装包。

### 运行测试与隐私审计

```powershell
python scripts/privacy_audit.py
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## 开源协议

本项目基于 **GNU General Public License v3.0 (GPL-3.0)** 协议开源，详见 [LICENSE](./LICENSE) 与 [THIRD_PARTY_LICENSES.md](./THIRD_PARTY_LICENSES.md)。

</details>

---

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