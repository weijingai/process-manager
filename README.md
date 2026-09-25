# 进程管理工具（潍鲸 - weijing.co）

跨平台进程 / 服务 / 端口 / 系统资源统一管理工具，提供 **Windows** 与 **Linux** 两个版本，
每个版本均支持「Web 界面」与「桌面程序」两种形态，界面采用 macOS 风格设计。

- 官方网站：https://weijing.co
- 品牌：潍鲸（weijing.co）

---

## 版本一览

| 版本 | 形态 | 交付物 | 说明 |
|---|---|---|---|
| Windows | Web 版 | `WindowsProcessManager.exe` | 本地 HTTP 服务（默认 `http://127.0.0.1:8765`），自动打开浏览器 |
| Windows | 桌面版 | `WindowsProcessManager-Desktop.exe` | 原生 tkinter 窗口，无需浏览器，双击即开 |
| Linux | Web 版（源码包） | `LinuxProcessManager.tar.gz` | 源码 + 一键打包脚本，需在目标 Linux 上打包为单文件 |

> 两个 Windows 可执行文件均为 PyInstaller 单文件打包，无需安装 Python，双击即可运行。
> Linux 版因 PyInstaller 不支持跨平台打包，提供源码包，在目标机上 `bash build-linux.sh` 生成单文件。

---

## 功能特性

| 模块 | 说明 |
|---|---|
| 进程管理 | 进程列表（CPU / 内存 / 端口 / 启动时间），结束进程、结束进程树，系统关键进程保护 |
| 服务管理 | Windows 服务 / Linux systemd 单元：启动、停止、重启，启动类型切换（自动 / 手动 / 禁用） |
| 端口管理 | TCP / UDP 监听与连接，端口与进程关联定位 |
| 系统监控 | CPU / 内存 / 磁盘 IO / 网络实时图表，负载（loadavg），Top 进程 |
| 磁盘清理 | 白名单清理项（临时文件 / 软件包缓存 / 日志 / 崩溃转储 / 浏览器缓存 / 回收站），默认 dry-run 预演 |
| 大文件扫描 | 全盘大文件定位 + 风险标签 + 送回收站 |
| 内存清理（Linux） | `sync` + `drop_caches` 释放内核页缓存（需 root） |

界面特性：

- macOS 风格视觉：系统蓝主色、毛玻璃面板、红绿灯窗口装饰、分段控件页签、Finder 侧栏树。
- 表格「显示列」以卡片形式勾选，按需显示 / 隐藏列。
- 操作列以红色字体标注「执行」，点击即弹出操作菜单（启停服务、结束进程等）。

---

## 下载与安装

从 GitHub Releases 下载对应版本（首个版本为 **v1.0.0**）：

- **Windows（Web 版）**：`WindowsProcessManager.exe`
- **Windows（桌面版）**：`WindowsProcessManager-Desktop.exe`
- **Linux（源码包）**：`LinuxProcessManager.tar.gz`

### Windows

直接双击对应的 `.exe` 即可：

- Web 版会自动启动本地服务并打开浏览器，访问 `http://127.0.0.1:8765`。
- 桌面版直接打开原生窗口。

> 部分安全软件可能误报单文件打包程序，加入白名单即可。
> 服务管理、内存清理等需要管理员 / root 权限的操作，请以相应权限运行。

命令行参数：

```bat
WindowsProcessManager-Desktop.exe --smoke-test    :: 无窗口自检（验证环境是否可用）
WindowsProcessManager.exe --port 9000             :: 指定端口
WindowsProcessManager.exe --no-browser            :: 不自动打开浏览器
```

### Linux

```bash
tar -xzf LinuxProcessManager.tar.gz
cd LinuxProcessManager
python3 -m pip install --user -r requirements.txt
python3 app.py                 # http://127.0.0.1:8765
sudo python3 app.py            # root 运行：服务管理 / 内存清理可用
```

或打包为单文件后运行：

```bash
bash build-linux.sh            # 产物 dist/LinuxProcessManager
sudo ./dist/LinuxProcessManager --port 8765
```

常用参数：

```
--port 8765        服务端口（被占用自动顺延）
--host 127.0.0.1   监听地址；0.0.0.0 允许局域网访问（请确保网络安全）
--no-browser       不自动打开浏览器（服务器环境建议加）
```

---

## Windows 详细说明

一个面向 Windows 的本地进程 / 服务可视化管理工具。自动扫描系统服务、运行进程与端口占用，
默认展示当前已启动的应用服务及其 PID、端口号，并支持一键启停服务、结束进程。

### 功能

| 模块 | 能力 |
|---|---|
| 运行中服务 | 默认视图。列出所有已启动 / 启动中的服务，展示显示名称、服务名、状态、PID、监听端口、启动类型、内存占用 |
| 全部服务 | 全部服务（含已停止、已禁用），可直接在表格里把启动类型切换为 自动 / 手动 / 禁用 |
| 进程 | 进程名、PID、父 PID、CPU、内存、占用端口、启动时间、运行账号、完整路径与命令行，支持结束进程或结束进程树；若该进程承载了服务，可直接停止 / 重启服务 |
| 端口占用 | 端口、协议、本地地址、连接状态、所属 PID 与进程名，可一键结束占用端口的进程，或停止 / 重启占用该端口的服务 |
| 系统监控 | 实时 CPU / 内存 / 磁盘 / 网络四张概览卡，CPU 与内存趋势曲线（最近 5 分钟），逐核心占用、磁盘分区用量、资源占用 Top 8、各网卡 IPv4 与流量 |
| 清理 | 一键内存清理、磁盘空间清理（12 类可清理项）、大文件扫描与删除（默认送入回收站，可还原） |

其他能力：关键字搜索（名称 / PID / 端口 / 路径 / 描述）、点击列头排序、状态筛选、
可配置间隔的自动刷新、管理员权限检测与一键提权、系统核心服务与关键进程保护。

服务操作统一为 **启动 / 停止 / 重启** 三档：服务视图里对运行中、启动中、停止中的服务都提供停止与重启；
进程与端口视图会按 PID 反查其承载的服务，同样提供停止服务与重启服务。

### 安装与运行

**方式一：直接用 exe（推荐，开箱即用）**

| 文件 | 界面 | 说明 |
|---|---|---|
| `dist\WindowsProcessManager-Desktop.exe` | 原生窗口 | 双击直接弹出程序窗口，不需要浏览器，所有功能内置 |
| `dist\WindowsProcessManager.exe` | 浏览器界面 | 双击后自动用默认浏览器打开管理界面，适合远程 / 多标签查看 |

启停系统服务请 **右键 → 以管理员身份运行**。

**方式二：源码运行**

```bash
python app.py --port 9000       # 浏览器界面版
python app.py --no-browser      # 不自动打开浏览器
python gui_app.py               # 原生桌面版（需 Python 自带 tkinter）
python gui_app.py --smoke-test  # 桌面版自检
```

启动后自动打开 `http://127.0.0.1:8765`，端口被占用时会自动顺延。

### 打包成 exe

```bat
pyinstaller build.spec --noconfirm        :: 浏览器版
pyinstaller build-gui.spec --noconfirm    :: 桌面版（需 tkinter 环境）
```

产物：`dist\WindowsProcessManager.exe`（约 8 MB，浏览器界面）、`dist\WindowsProcessManager-Desktop.exe`（约 12 MB，原生窗口，含 Tcl/Tk 运行时）。
两者都是单文件，可单独拷贝到任意 Windows 机器运行。

> 桌面版请使用含 tkinter 的 Python 环境打包（例如 gui314 环境，Tcl/Tk 9.0.4）。
> 支持 Windows 7 SP1 及以上（x64）。单文件 exe 首次启动会释放资源到临时目录，启动约需 1-2 秒，属正常现象。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/scan?force=1` | 全量扫描（6 秒内复用缓存，force 强制刷新） |
| GET | `/api/status` | 运行环境与权限信息 |
| POST | `/api/service/action` | `{name, action}` action = start / stop / restart |
| POST | `/api/service/config` | `{name, start_type}` = automatic / manual / disabled |
| POST | `/api/process/kill` | `{pid, tree}` tree=true 时结束进程树 |
| POST | `/api/elevate` | 申请管理员权限重新启动（触发 UAC） |

---

## Linux 详细说明

Windows 进程管理工具的 Linux 版本，Web 界面与操作方式完全一致。

### 功能

| 模块 | 说明 |
|---|---|
| 进程管理 | 进程列表（CPU / 内存 / 端口 / 启动时间），结束进程 / 进程树，系统关键进程保护 |
| 服务管理 | systemd 单元：启动 / 停止 / 重启，启动类型切换（自动=enable / 手动=disable / 禁用=mask） |
| 端口管理 | TCP / UDP 监听与连接，关联进程定位 |
| 系统监控 | CPU / 内存 / 磁盘 IO / 网络实时图表，负载（loadavg），Top 进程 |
| 磁盘清理 | 白名单清理项（临时文件 / 软件包缓存 / journald 日志 / 崩溃转储 / 用户与浏览器缓存 / 回收站），默认 dry-run 预演 |
| 大文件扫描 | 全盘大文件定位 + 风险标签 + 送回收站 |
| 内存清理 | sync + drop_caches 释放内核页缓存（需 root） |

### 运行方式

**方式一：源码运行**

```bash
cd linux
python3 -m pip install --user -r requirements.txt
python3 app.py                 # http://127.0.0.1:8765
sudo python3 app.py            # root 运行：服务管理 / 内存清理可用
```

**方式二：打包成单文件**

```bash
cd linux
bash build-linux.sh            # 产物 dist/LinuxProcessManager
sudo ./dist/LinuxProcessManager --port 8765
```

> 注意：PyInstaller 不支持跨平台打包，请在目标 Linux（x86_64 / aarch64）上执行打包脚本。

### 权限说明

| 操作 | 普通用户 | root |
|---|---|---|
| 查看进程 / 端口 / 监控 | ✅（他人进程部分字段受限） | ✅ 完整 |
| 结束自己的进程 | ✅ | ✅ |
| 结束他人 / 系统进程 | ❌ | ✅ |
| 服务启停 / 启动类型 | ❌ | ✅ |
| 内存清理 drop_caches | ❌ | ✅ |
| 清理 /tmp、~/.cache 等 | ✅（限当前用户目录） | ✅ |

### 目录结构（linux/）

```
linux/
├── app.py              # 服务入口（HTTP API，与 Windows 版接口一致）
├── core/
│   ├── scanner.py      # systemctl 服务 + psutil 进程/端口扫描
│   ├── control.py      # systemctl 服务控制 / 进程终止 / pkexec 提权
│   ├── cleanup.py      # 清理白名单 / 大文件 / drop_caches 内存清理
│   ├── monitor.py      # CPU/内存/磁盘/网络采样（含 loadavg）
│   └── textutil.py     # 输出解码与控制台编码
├── web/                # 前端（与 Windows 版同一套界面）
├── build-linux.sh      # 一键打包脚本
├── build-linux.spec    # PyInstaller 配置
├── requirements.txt
└── start.sh            # 源码启动脚本
```

### 环境要求

- Python ≥ 3.9（源码运行）；打包后无需 Python
- systemd（服务管理功能需要；无 systemd 时服务页为空，其余功能正常）
- `gio`（可选，回收站功能优先使用；缺失时自动回退手动移入 Trash）

---

## 安全设计

- **进程 / 服务保护**：systemd（PID 1）、dbus、journald、sshd、Windows 关键系统进程等被工具保护，禁止结束与停止。
- **清理白名单**：磁盘清理只接受预定义 id，不接受任意路径；`/usr`、`/etc`、`/var/lib` 等系统目录列为保护路径。
- **默认预演**：清理默认 dry-run，确认释放量后才真正删除。
- **删除走回收站**：优先系统回收站（Windows 回收站 / Linux `gio trash`），可还原。
- **仅本地监听**：Web 服务默认仅监听 `127.0.0.1`，不对外暴露。

---

## 目录结构（仓库根）

```
windows应用管理工具/
├── app.py                  # Windows Web 版服务入口
├── gui_app.py              # Windows 桌面版（tkinter）
├── web/                    # 前端（Web 版与 Linux 版共用同一套界面）
├── core/                   # Windows 后端（扫描 / 控制 / 清理 / 监控）
├── build.spec              # Web 版打包配置
├── build-gui.spec          # 桌面版打包配置
├── linux/                  # Linux 版源码（同构实现，含 build-linux.sh）
└── dist-linux/             # Linux 源码发布包 LinuxProcessManager.tar.gz
```

---

## 许可与品牌

© 潍鲸（weijing.co）。本工具供个人与企业在授权范围内使用。
