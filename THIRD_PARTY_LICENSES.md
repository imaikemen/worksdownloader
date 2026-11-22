# 第三方组件与开源许可说明 (Third-Party Licenses)

本项目（**作品鉴赏downloader**）以 **GNU General Public License v3.0 (GPL-3.0)** 开源发布，详见根目录 [LICENSE](./LICENSE)。

打包生成的 Windows 可执行文件与安装包包含以下第三方开源组件，完整许可文本保存在 `licenses/` 目录中：

| 组件 | 用途 | 开源协议 | 许可文件 / 上游来源 |
| --- | --- | --- | --- |
| **FFmpeg / FFprobe** | 音视频封装、画质探测、封面抽帧 | **GNU GPL v3.0**（`--enable-gpl --enable-version3` 构建） | [licenses/FFmpeg-LICENSE.txt](./licenses/FFmpeg-LICENSE.txt) / <https://ffmpeg.org/> |
| **Node.js** | 执行小红书公开/会话解析脚本 | **MIT License**（含捆绑子组件许可） | [licenses/Node.js-LICENSE.txt](./licenses/Node.js-LICENSE.txt) / <https://nodejs.org/> |
| **yt-dlp** | 透明媒体流信息解析与回退 | **The Unlicense** | [licenses/yt-dlp-LICENSE.txt](./licenses/yt-dlp-LICENSE.txt) / <https://github.com/yt-dlp/yt-dlp> |
| **@lucasygu/redbook** | 小红书 / RedNote 笔记与主页解析 | **MIT License** | [licenses/redbook-LICENSE.txt](./licenses/redbook-LICENSE.txt) / <https://www.npmjs.com/package/@lucasygu/redbook> |
| **pywebview** | Windows Edge WebView2 桌面容器 | **BSD 3-Clause License** | <https://github.com/r0x0r/pywebview> |
| **Pillow** | 封面缩略图与原图分辨率校验 | **MIT-CMU License** | <https://python-pillow.org/> |

## 关于 FFmpeg GPLv3 合规声明

由于 Windows 预编译打包版本内置的 FFmpeg 为启用了 `--enable-gpl --enable-version3` 的 **GPLv3** 构建，本项目遵循 GPLv3 义务：

1. 完整公开本项目全部源代码；
2. 在源码仓库、单文件可执行包及 Windows 安装包中均完整附带 `LICENSE` 与 `licenses/FFmpeg-LICENSE.txt`；
3. 不附带任何闭源专有二进制补丁，构建过程通过 GitHub Actions 工作流（`.github/workflows/release.yml`）与 `scripts/prepare_runtime.py` 完全透明可复现。
