# WEAutoTray（Wallpaper Engine 创意工坊自动换壁纸）

这是一个常驻托盘的小工具：按你的筛选条件从 Steam Workshop 拉取 Wallpaper Engine（AppID `431960`）作品，下载到本地后自动应用，并按配置清理历史下载。

当前工作目录示例：`D:\we`

## 功能概览

- 托盘常驻 + 实时控制台
- 自动抓取候选（优先走 Steam Web API；无 Key 时回退网页抓取）
- 维度过滤（**维度内 OR、维度间 AND**）：Genre/Type/Age/Resolution + exclude
- 支持按 **标题关键字** 与 **上传者 SteamID64** 排除
- SteamCMD 下载 → 镜像到 `workshop_root` → 同步到 WE `projects/backup` → 发送 WE 控制指令应用
- one-per-run 模式：每轮只应用 1 个，循环切换
- 清理历史：可配置删除上一张（或保留最近 N 张）

## 目录结构（重要文件）

- `WEAutoTray.exe`：主程序（托盘）
- `we_tray.py`：托盘源码入口
- `we_auto_fetch.py`：候选抓取/下载/应用逻辑（worker）
- `config`：配置文件（ini 格式）
- `WEAutoTray.spec`：PyInstaller 打包配置
- `we_auto_state.json`：状态/历史（自动生成）
- `we_downloads.log`：下载/应用记录（可选）

## 快速开始

### 1) 准备依赖

需要：
- Wallpaper Engine 已安装
- SteamCMD 可用

### 2) 配置 `config`

打开 `D:\we\config`，最少确保这些路径正确：

- `[paths].we_exe`：例如 `F:\SteamLibrary\steamapps\common\wallpaper_engine\wallpaper64.exe`
- `[paths].steamcmd`：例如 `D:\steamcmd\steamcmd.exe`
- `[paths].workshop_root`：例如 `D:\WE_script_workshop`

账号（建议至少填用户名）：

- `[auth].steam_username`
- `steam_password/steam_guard_code` 建议只用于首次登录，成功后清空，使用已缓存凭证

### 3) 运行

双击 `WEAutoTray.exe`，托盘出现图标即表示运行中。

## 托盘菜单说明

右键托盘图标：

- **登录账号...**：用弹窗输入账号/密码/2FA（成功后会重启 worker）
- **打开/隐藏 控制台**：查看实时日志
- **立即更换一次**：触发 worker 立刻执行一轮
- **排除当前壁纸上传者并立即切换**：把当前作品作者加入 `[filters].creator_exclude_ids` 后立刻切换
- **开启/关闭开机自启**
- **退出**

## 配置说明（按当前 `config`）

### 调度

- `[schedule].run_on_startup=true`：启动就跑一轮
- `[schedule].interval=2h`：每 2 小时执行一轮（留空/0 表示只跑一次）
- `[schedule].detect_interval=1m`：等待路径就绪时的检测间隔

### 候选来源与排序

- `[steam].api_key=...`：可选；有 Key 时走 Web API（更稳定）
- `[sort].method=Most Popular (Week)`：支持 `Top Rated / Most Popular(...) / Most Recent / Most Subscriptions / Most Up Votes`

### 过滤（核心）

`[filters]` 采用：**维度内 OR、维度间 AND**。

- `tags=Anime`：标签维度
- `types=Video`：类型维度（Scene/Video/Web/Application/Wallpaper/Preset）
- `age=G`：年龄分级（G/PG13/R → Everyone/Questionable/Mature）
- `resolution=`：可留空；例如 `3840 x 2160`
- `exclude=...`：排除标签（命中即剔除）
- `title_exclude_contains=vam`：标题包含关键字则剔除（不区分大小写）
- `creator_exclude_ids=7656...`：排除上传者 SteamID64（可填 17 位数字或 profiles URL）
- `min_candidates=100`：API 模式下，AND 过滤后达到最少候选才早停

### 清理

- `[cleanup].delete_previous=true`：应用新壁纸后清理旧项（会清 3 处目录）
- `[cleanup].keep_last_n=0`：>0 时改为保留最近 N 张（不会只删上一张）
- `[cleanup].use_recycle_bin=false`：true 时移动到 `Trash/`，false 直接删

## 常见问题

### 1) 为什么日志里会重复打印 `[config]` / `[filters]`？

程序会多次读取配置以支持热更新；新版本已做“配置未变化不重复打印摘要”，刷屏会明显减少。

### 2) SteamCMD 报 `Locking Failed` / `steam didn't shutdown cleanly`

一般是 SteamCMD 残留/并发导致锁冲突。新版本在重启 worker 时会尽量结束进程树，并对可重试错误做退避重试。

建议：
- 确保任务管理器里没有残留 `steamcmd.exe`
- 避免在下载过程中频繁“登录/重启 worker”

### 3) 应用壁纸失败（返回码 5）

`wallpaper64.exe -control ...` 返回码 5 常见于 **权限级别不一致**：
- Wallpaper Engine 与 WEAutoTray 需要同为“管理员”或同为“非管理员”运行。

## 重新打包（开发者）

在 `D:\we` 下使用 PyInstaller：

```powershell
py -m pip install -U pyinstaller requests
py -m PyInstaller --noconfirm --clean WEAutoTray.spec
```

输出在：
- `dist\WEAutoTray.exe`

