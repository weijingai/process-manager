# Linux进程管理工具（潍鲸 - weijing.co）

Windows 进程管理工具的 Linux 版本，Web 界面与操作方式完全一致。

## 功能

| 模块 | 说明 |
|---|---|
| 进程管理 | 进程列表（CPU / 内存 / 端口 / 启动时间），结束进程 / 进程树，系统关键进程保护 |
| 服务管理 | systemd 单元：启动 / 停止 / 重启，启动类型切换（自动=enable / 手动=disable / 禁用=mask） |
| 端口管理 | TCP / UDP 监听与连接，关联进程定位 |
| 系统监控 | CPU / 内存 / 磁盘 IO / 网络实时图表，负载（loadavg），Top 进程 |
| 磁盘清理 | 白名单清理项（临时文件 / 软件包缓存 / journald 日志 / 崩溃转储 / 用户与浏览器缓存 / 回收站），默认 dry-run 预演 |
| 大文件扫描 | 全盘大文件定位 + 风险标签 + 送回收站 |
| 内存清理 | sync + drop_caches 释放内核页缓存（需 root） |

## 运行方式

### 方式一：源码运行

```bash
cd linux
python3 -m pip install --user -r requirements.txt
python3 app.py                 # http://127.0.0.1:8765
sudo python3 app.py            # root 运行：服务管理 / 内存清理可用
```

### 方式二：打包成单文件

```bash
cd linux
bash build-linux.sh            # 产物 dist/LinuxProcessManager
sudo ./dist/LinuxProcessManager --port 8765
```

> 注意：PyInstaller 不支持跨平台打包，请在目标 Linux（x86_64 / aarch64）上执行打包脚本。

## 常用参数

```
--port 8765        服务端口（被占用自动顺延）
--host 127.0.0.1   监听地址；0.0.0.0 表示允许局域网访问（请确保网络安全）
--no-browser       不自动打开浏览器（服务器环境建议加）
```

## 权限说明

| 操作 | 普通用户 | root |
|---|---|---|
| 查看进程 / 端口 / 监控 | ✅（他人进程部分字段受限） | ✅ 完整 |
| 结束自己的进程 | ✅ | ✅ |
| 结束他人 / 系统进程 | ❌ | ✅ |
| 服务启停 / 启动类型 | ❌ | ✅ |
| 内存清理 drop_caches | ❌ | ✅ |
| 清理 /tmp、~/.cache 等 | ✅（限当前用户目录） | ✅ |

## 安全设计

- **服务保护**：dbus / journald / sshd 等核心服务被工具保护，禁止停止与重启
- **进程保护**：systemd（PID 1）等关键进程禁止结束
- **清理白名单**：磁盘清理只接受预定义 id，不接受任意路径；/usr、/etc、/var/lib 等系统目录列为保护路径
- **默认预演**：清理默认 dry-run，确认释放量后才真正删除
- **删除走回收站**：优先 `gio trash`，无 gio 时移入 `~/.local/share/Trash`，可还原

## 目录结构

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

## 环境要求

- Python ≥ 3.9（源码运行）；打包后无需 Python
- systemd（服务管理功能需要；无 systemd 时服务页为空，其余功能正常）
- `gio`（可选，回收站功能优先使用；缺失时自动回退手动移入 Trash）
