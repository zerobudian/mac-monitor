# Mac Monitor

轻量、开源的 macOS 本机硬件监控面板，同时提供浏览器页面与桌面常驻浮窗。实时展示 CPU、GPU、系统负载、风扇和内存状态，不需要账号、密码或管理员权限。

服务只监听 `127.0.0.1`，数据不会发送到外部服务。

## 下载常驻版

从 GitHub Releases 下载与你的 Mac 架构匹配的压缩包，解压后将 `Mac Monitor.app` 拖入“应用程序”即可。当前发布包使用临时签名、未经过 Apple 公证；首次启动可在 Finder 中右键应用并选择“打开”。

系统要求：macOS 26 或更高版本。发布包已经包含网页资源、采样服务和编译好的原生窗口，不要求安装 Python 或 Xcode。

## 启动

```bash
cd mac-monitor
python3 server.py
```

程序会自动选择 `8787` 起的可用端口并打开液态玻璃浮窗。首次启动会用系统自带的 Swift 编译器准备约 105 KB 的原生 WebKit 外壳，之后直接复用。关闭浮窗或在终端按 `Ctrl+C` 都会停止服务。

只使用网页版：

```bash
python3 server.py --browser
```

### 参数

| 参数 | 说明 |
| --- | --- |
| `--port N` | 指定起始端口，默认 8787（被占用时自动向后顺延） |
| `--browser` | 改用普通浏览器页面 |
| `--no-open` | 只启动服务，不自动打开任何界面 |

## 功能

- **桌面常驻**：无 Dock 图标、置于普通窗口上方，并可跨桌面显示
- **两种形态**：180×180 紧凑态用 2×2 环形指标展示 CPU、GPU、风扇和内存；520×390 展开态增加负载并在一页显示全部三类曲线
- **整面切换**：点击紧凑态主体展开，点击展开态内容区收起
- **HIG 材质层级**：Liquid Glass 仅用于原生顶层控制组，指标和图表使用标准内容材质，避免玻璃嵌套
- **语义状态色**：环形状态使用绿、橙、红，并始终同时显示数值，不只依赖颜色传达信息
- **即开即用**：无需登录，执行命令后直接进入监控面板
- **实时推送**：后端每秒采样一次，通过 SSE 推送，断线自动重连
- **滚动曲线**：展示最近 5 分钟 CPU/GPU、平均负载和风扇转速
- **导出日志**：一键将内存中最多 30 分钟的采样记录导出为 UTF-8 CSV
- **自适应主题**：默认跟随系统，也可固定浅色或深色
- **本地运行**：仅监听 `127.0.0.1`，数据不会发送到外部服务
- **优雅降级**：单项指标不可读取时不会影响其他指标

## 指标来源（macOS）

| 指标 | 方式 | 权限 |
| --- | --- | --- |
| CPU 占用 | `host_statistics64(HOST_CPU_LOAD_INFO)` 差值 | 普通用户 |
| 平均负载 | `os.getloadavg()` | 普通用户 |
| GPU 占用 | `ioreg` IOAccelerator 的 `Device Utilization %` | 普通用户 |
| 风扇转速/负载 | AppleSMC 用户客户端直读 `F#Ac/F#Mn/F#Mx` | 普通用户 |
| 内存占用 | `vm_stat` + `hw.memsize` | 普通用户 |

> 风扇读取直接与 AppleSMC 通信，无需 `sudo`。无风扇机型会在页面显示“此机型没有风扇”。

## 设计依据

界面遵循 Apple Human Interface Guidelines 的 Materials、Color、Accessibility 与 macOS window guidance：Liquid Glass 仅用于顶层原生控制组，内容层使用标准材质；状态始终同时提供颜色和数值；系统“降低透明度、提高对比度、减少动态效果”设置均有对应适配。

## 构建 macOS 应用

构建需要 macOS 26、Xcode Command Line Tools、Python 3，以及 PyInstaller：

```bash
python3 -m venv .build-venv
.build-venv/bin/python -m pip install --upgrade pip pyinstaller
bash scripts/build_macos.sh 1.0.0
```

产物位于 `release/`。构建脚本会编译 Swift 原生外壳、将服务和网页资源封装进 `.app`、执行临时签名，并生成 SHA-256 校验文件。

## 隐私与安全

- HTTP 服务只绑定本机回环地址 `127.0.0.1`。
- 所有采样记录只保存在内存中，退出后自动清除。
- 只有用户主动导出时才会生成 CSV 文件。
- 应用不包含遥测、分析或自动更新代码。

## 许可证

项目采用 [MIT License](LICENSE)。

## 文件结构

```text
mac-monitor/
├── server.py        # 采样器、HTTP、SSE、CSV 导出
├── desktop.swift    # macOS 原生透明 WebKit 常驻窗口
├── scripts/
│   └── build_macos.sh
├── web/
│   ├── index.html   # 面板结构
│   ├── style.css    # 响应式界面和主题
│   └── app.js       # 实时卡片、Canvas 曲线与交互
└── README.md
```
