# 进程管理工具 v1.0.0（首个版本）

**发布方：潍鲸（weijing.co） · https://weijing.co**

跨平台进程 / 服务 / 端口 / 系统资源统一管理工具的首个正式版本，提供 Windows 与 Linux 两个版本，
每个版本均支持 Web 界面与桌面程序两种形态，界面采用 macOS 风格设计。

## 本次发布包含

| 资产 | 平台 / 形态 | 说明 |
|---|---|---|
| `WindowsProcessManager.exe` | Windows · Web 版 | 本地 HTTP 服务（默认 `http://127.0.0.1:8765`），自动打开浏览器 |
| `WindowsProcessManager-Desktop.exe` | Windows · 桌面版 | 原生 tkinter 窗口，无需浏览器，双击即开 |
| `LinuxProcessManager.tar.gz` | Linux · 源码包 | 源码 + 一键打包脚本，需在目标 Linux 上打包为单文件 |

## 主要功能

- **进程管理**：进程列表（CPU / 内存 / 端口 / 启动时间），结束进程、结束进程树，系统关键进程保护。
- **服务管理**：Windows 服务 / Linux systemd 单元 的启动、停止、重启，启动类型切换（自动 / 手动 / 禁用）。
- **端口管理**：TCP / UDP 监听与连接，端口与进程关联定位。
- **系统监控**：CPU / 内存 / 磁盘 IO / 网络实时图表，负载（loadavg），资源占用 Top 进程。
- **磁盘清理**：白名单清理项（临时文件 / 缓存 / 日志 / 回收站等），默认 dry-run 预演，删除走回收站可还原。
- **大文件扫描**：全盘大文件定位 + 风险标签 + 送回收站。
- **内存清理（Linux）**：`sync` + `drop_caches` 释放内核页缓存（需 root）。

## 界面特性

- macOS 风格视觉：系统蓝主色、毛玻璃面板、红绿灯窗口装饰、分段控件页签、Finder 侧栏树。
- 表格「显示列」以卡片形式勾选，按需显示 / 隐藏列。
- 操作列以红色字体标注「执行」，点击即弹出操作菜单。

## 使用说明

### Windows

直接双击对应 `.exe`：

- Web 版自动启动本地服务并打开浏览器（`http://127.0.0.1:8765`）。
- 桌面版直接打开原生窗口。
- 启停系统服务、内存清理等需要管理员权限的操作，请 **右键 → 以管理员身份运行**。
- 常见安全软件可能误报单文件打包程序，加入白名单即可。

### Linux

```bash
tar -xzf LinuxProcessManager.tar.gz
cd LinuxProcessManager
python3 -m pip install --user -r requirements.txt
python3 app.py                 # http://127.0.0.1:8765
sudo python3 app.py            # root 运行：服务管理 / 内存清理可用
```

如需打包为单文件：

```bash
bash build-linux.sh            # 产物 dist/LinuxProcessManager
sudo ./dist/LinuxProcessManager --port 8765
```

> PyInstaller 不支持跨平台打包，Linux 单文件需在目标机上生成。

## 安全设计

- 进程 / 服务保护：核心系统进程与关键服务被保护，禁止结束与停止。
- 清理白名单：只接受预定义 id，不接受任意路径；系统目录列为保护路径。
- 默认预演：清理默认 dry-run，确认释放量后才真正删除。
- 删除走回收站：优先系统回收站，可还原。
- Web 服务默认仅监听 `127.0.0.1`，不对外暴露。

## 已知说明

- Windows 桌面版需以包含 tkinter 的环境打包（如 gui314 环境，Tcl/Tk 9.0.4）。
- 单文件 exe 首次启动会释放资源到临时目录，启动约需 1-2 秒，属正常现象。
- Linux 版服务管理依赖 systemd；无 systemd 时服务页为空，其余功能正常。

---

© 潍鲸（weijing.co）。本工具供个人与企业在授权范围内使用。
