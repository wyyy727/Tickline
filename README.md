# Pacelite

单行「档位式」性能叠加层（Windows）。按参考图复刻：一条半透明黑条，左侧帧率，中间一条
帧时间曲线，右侧按档位递增挂载 CPU / GPU / 温度 / 显存 / 内存。

## 运行

```powershell
# 常驻运行（托盘 + 全局热键 + 设置窗口）
D:\develop\Python313\python.exe main.py

# 四个档位叠在一起看（样式评审用）
D:\develop\Python313\python.exe main.py --demo --fake-fps

# 只截一张图，不常驻
D:\develop\Python313\python.exe main.py --demo --fake-fps --no-tray --shot assets/tiers.png
```

> 注意：`python` 在 WorkBuddy 的 shell 里指向自带的 3.13.12（没装 Qt），必须用
> `D:\develop\Python313\python.exe`。
>
> 需要 CPU 温度（精确传感器）时，先跑一次 `python fetch_deps.py` 把 PawnIO 安装包下载到位。

## 档位

| # | 名称 | 显示内容 |
|---|------|----------|
| 1 | 简洁 FPS | 当前帧率（开帧生成时附带帧生成帧率） |
| 2 | FPS 详情 | 帧生成帧率、实际帧率、最小/最大帧、帧时间曲线 |
| 3 | 加入 CPU/GPU | 再加处理器与显卡占用 |
| 4 | 全面监控 | 再加 CPU 最忙单核/温度/功耗/频率、GPU 温度与功耗、显存、内存、SSD |

第 4 档的分组顺序：`CPU 占用 → ↑最忙单核 → 温度 → 功耗 → 频率`，然后
`GPU 占用 → 温度 → 功耗 → 显存`，最后 `RAM → SSD`。显存属于显卡，跟 GPU 同组；内存单独一组。

`↑xx%` 是**当前最忙单核**的瞬时占用（不是历史峰值），配合 `CPU xx%`（全核平均）能看出负载是否集中在单核。

硬盘部分：只有一块盘时标签就是 `SSD`；多块盘时变成 `SSD1`/`SSD2`…，按组换行布局下每块盘各占一行。
`R`/`W` 分别是读/写速率（B/s、KB/s、MB/s、GB/s 自适应），没有 IO 时不显示该项。

## 操作

| 操作 | 方式 |
|------|------|
| 切换档位 | `Ctrl+Shift+1..4` 或托盘菜单（固定键位） |
| 切换显示器 | `Ctrl+Shift+M`（可改）／托盘"切换到下一块显示器"／设置里的"显示器"下拉 |
| 移动位置 | 鼠标直接拖动长条（松手记住；穿透模式下不可拖） |
| 退出 | 悬停长条时点右侧 `✕`、任务栏关闭、托盘"退出" |
| 鼠标穿透开关 | 托盘菜单或设置 |
| 设置窗口 | 托盘 → 设置 |

只有 **切换显示器** 一个全局热键，可在设置窗口的「快捷键」区改：点按钮 → 直接按组合键 →
立即生效。录键期间程序会先让出自己占用的热键，否则你要录的组合会被系统吃掉。
若新组合被别的程序占走，会自动回滚到原来的键并红字提示；启动时若配置里的键注册不上，
则回退到内置默认 `Ctrl+Shift+M`，不会出现"按了没反应"。

> 鼠标穿透和退出不设全局热键：它们都有更自然的入口（托盘菜单、✕、任务栏），
> 多一个全局热键就多一份和别的软件抢键位的风险。

**监视进程**下拉列出"正在出帧的进程 + 当前有可见窗口的进程"，也可以直接键入 exe 名。
手动指定后只统计该进程：它没在出帧时帧率显示 `—`，不会偷偷回落到"最忙的那个"。

## 设置项

显示器、档位、文字大小、背景透明度、颜色饱和度、布局（整行／按组换行）、
显示内容（帧率／CPU／GPU／显存／内存／硬盘）、监视进程、快捷键（切换显示器）、
只在游戏运行时显示、始终置顶、鼠标穿透、录制/截图里隐藏、在任务栏显示、开机自启（**默认关闭**）、
精确传感器（CPU/GPU 温度，需管理员）。
配置落在 `%APPDATA%\Pacelite\config.json`，所有改动即时生效并自动保存。

### 多显示器

- 选中的屏幕按**屏幕名**记录（如 `24G1WG4`、`\\.\DISPLAY1`），不是序号 —— 插拔显示器后 Qt
  给的顺序会变，序号会指错屏幕。
- 选"自动（跟随主屏）"时，叠加层贴主屏可用区域的左上角。
- 直接把长条拖到另一块屏幕，设置里的"显示器"会跟着更新。
- 只有一块显示器时，托盘里的"切换到下一块显示器"自动隐藏（菜单每次打开都会重新判断）。
- 拔掉外接屏后，原先保存的坐标会悬空，程序会自动回退到当前可用的主屏，不会消失到看不见的地方。

## 数据来源

| 指标 | 来源 |
|------|------|
| CPU 总/各核占用、频率 | `psutil` |
| CPU 温度（封装/各核） | 提权子进程 + LibreHardwareMonitor（MSR），需 PawnIO 驱动 |
| 内存 | `psutil.virtual_memory` |
| GPU 占用 | PDH `GPU Engine(*)\Utilization Percentage`（3D 引擎求和后截断） |
| 显存占用 | PDH `GPU Adapter Memory(*)\Dedicated Usage`，核显回落到 `Shared Usage` |
| 显存总量 | 注册表 `HardwareInformation.qwMemorySize`（PDH 无容量计数器） |
| GPU 温度 | N 卡走 NVML（免提权）；其它显卡走 LibreHardwareMonitor |
| CPU / GPU 功耗 | LibreHardwareMonitor（CPU 取封装功耗）；N 卡也可由 NVML 提供 |
| 内存温度 | LibreHardwareMonitor（只有 DDR5 内存条的 SPD Hub 带温度传感器，DDR4 一般没有） |
| SSD 温度与读写速度 | LibreHardwareMonitor（默认不显示，设置里勾「硬盘」；`R`/`W` = 读/写速率，空闲时只留温度） |
| 帧率 / 帧生成 | `tools\PresentMon\PresentMon.exe --output_stdout`，读 `MsBetweenPresents` 与 `MsBetweenDisplayChange` |
| DLSS / FSR 判定 | 枚举目标进程已加载模块（`nvngx_dlssg` → DLSS，`ffx_fsr` → FSR），识别不出显示中性 `FG` |
| "游戏在跑"判定 | 存在铺满显示器的可见窗口（排除桌面/输入法等系统窗口），或有非合成器进程在持续出帧 |

### 温度为什么需要一个提权子进程

读 CPU 温度要访问 MSR（ring0），必须借助内核驱动。这里的做法：

1. `tools\lhm\` 放 LibreHardwareMonitor 的 dll（约 5.2 MB，24 个文件），**内置随程序分发**；
2. `tools\pawnio\PawnIO_setup.exe`（3.1 MB，官方签名）负责装 PawnIO 驱动，**同样内置**；
3. 在设置里勾选「精确传感器」时**只弹一次 UAC**：提权拉起子进程，驱动由这个子进程静默安装
   （它已经是管理员，不需要第二次提权）；
4. 子进程在提权环境里读传感器，把结果写进 `%LOCALAPPDATA%\Pacelite\sensors.json`，
   主程序只读这个文件——**主程序始终保持普通权限**；
5. 设置里提供「卸载 PawnIO 驱动」，随时可以撤掉（部分系统需重启才彻底移除）。

**使用者不需要自己下载任何东西**：驱动安装包和 LHM 库都随程序分发，勾一个开关即可。
子进程会盯着父进程，主程序退出后 10 秒内自行结束，不留常驻的提权进程。
读数过期（超过 3 秒没更新）或子进程已死时，温度字段直接不显示，不会拿旧值糊弄。

> 为什么不用 WinRing0：它被大量杀软标记，而 PawnIO 是签名驱动、在开启"内存完整性"（HVCI）的
> 系统上也能正常加载。

两个帧率的含义：`MsBetweenPresents` 的倒数是应用真正 `Present()` 出帧的速率（游戏实际帧率），
`MsBetweenDisplayChange` 的倒数是屏幕实际变帧的速率（含 DLSS/FSR 插帧）。两者比值即帧生成倍率，
不需要任何 NVIDIA/AMD API，也不需要每帧的 FrameType。

## 打包

```powershell
D:\develop\Python313\python.exe build.py            # 目录版，启动快（推荐日常使用）
D:\develop\Python313\python.exe build.py --onefile  # 单文件 exe，约 60-80 MB，启动慢 2-4 秒
```

需要先 `pip install pyinstaller`（只有打包才要，日常运行不需要）。产物在 `dist\`，
PresentMon、LHM 库、PawnIO 安装包都会一起打进去。

## 依赖下载

构建和运行需要三个第三方组件，都在 `tools\` 下。缺了不用手动找，`build.py` 会自动调用下载：

```powershell
python fetch_deps.py              # 缺什么下什么
python fetch_deps.py --force      # 全部重新下载
python fetch_deps.py --no-pawnio  # 跳过 PawnIO（不需要 CPU 温度时可以省 3 MB）
```

| 组件 | 版本 | 体积 | 落地位置 |
|------|------|------|----------|
| PresentMon | 2.6.0 | 0.93 MB | `tools\PresentMon\PresentMon.exe` |
| LibreHardwareMonitor | 0.9.6 | 6.3 MB（解出 24 个 dll，5.13 MB） | `tools\lhm\` |
| PawnIO 安装包 | 官方 master | 3.1 MB | `tools\pawnio\PawnIO_setup.exe` |

- 版本固定在上面的 `fetch_deps.py` 常量里，升级时改常量即可，保证任何人打出来的包一致。
- 下载支持**断点续传**：中断后从已下载的字节继续（`Range` 请求），服务器不支持 Range 时自动从头重下。
- LHM 只解需要的 dll，跳过 exe、OxyPlot、WinForms 控件和语言包，解完删除 zip。
- TLS 默认严格校验证书；仅证书链异常的开发环境才设 `PACELITE_INSECURE_TLS=1` 跳过。
- 打包时想自己控制依赖，用 `build.py --no-fetch` 关掉自动下载。

## 还能扩展的指标

LHM 在本机（i5-1135G7 笔记本）实际暴露的传感器，按硬件分组：

| 硬件 | 可用传感器 | 已在叠加层显示 |
|------|------------|----------------|
| CPU | 各核温度、Core Max、Package 温度、各核占用、频率、封装功耗、电压 | 温度、功耗、总频率、占用 |
| GPU | 占用（13 项引擎细分）、功耗 | 占用、功耗（核显无温度） |
| 内存 | SPD 时序/容量 | 只用 psutil 的已用/总量 |
| 存储 | SSD 温度、读写速度、读写总量、剩余寿命 | 温度、读写速度（多块盘逐块显示） |
| 主板 | 无（这台笔记本没有暴露 SuperIO/EC） | — |

值得再加的：**最热单核温度**（比 Package 更直观）、**风扇转速**（有 SuperIO 的机器才有）。
不值得加：CPU 电压、内存时序。
风扇转速要台式机或支持 EC 的游戏本才有，本机读不到。

## 已知限制

- **PresentMon 的 stdout 是块缓冲**：游戏帧率正常时数据以约 4 KB 为块到达；空闲桌面（无
  DXGI 提交）时不会有任何行 —— 叠加层显示 `—` 表示"没有目标在出帧"，不是采集失败。
- **ETW 会话残留**：PresentMon 被强杀会留下同名 realtime session，之后的采集会失败。正常退出
  时本程序会执行 `--terminate_existing_session` 清理；若手工杀过进程，用 `logman query -ets`
  查看、`logman stop <名字> -ets` 清理。
- 核显上 `Dedicated Usage` 恒为 0，显存只能显示占用量，没有可比的分母。
- 全屏独占游戏不显示（与所有分层窗口叠加层一致），需无边框窗口化。
- "只在游戏运行时显示"是启发式判断：无边框全屏的浏览器/视频播放器也会被判为"游戏"。

## 目录

```
main.py            入口：命令行、托盘、热键、装配（也负责 --sensors-helper 分支）
overlay.py         Qt 窗口与绘制（一条 paintEvent 画完所有分段）、拖动、✕
tiers.py           四个档位的字段定义
metrics.py         硬件采样（psutil + PDH + 可选 NVML / 提权传感器）
sensors.py         提权子进程的启停、PawnIO 检测与静默安装
sensors_helper.py  提权进程本体：加载 LHM，把读数写成 JSON
presentmon.py      PresentMon 子进程与两条帧率
winprobe.py        全屏窗口探测、进程模块枚举（DLSS/FSR 判定）
settings.py        设置窗口
config.py          JSON 配置
hotkey.py          快捷键字符串 ↔ 键码（解析、格式化、Qt 录入转换）
theme.py           配色与饱和度
winutil.py         录制隐藏、全局热键
single_instance.py 单实例互斥体
sysintegrate.py    开机自启
build.py           PyInstaller 打包
fetch_deps.py      下载 PresentMon / LHM / PawnIO（构建前自动调用）
dev/               验证脚本（presenter 合成帧源、probe 分步探针、settings_shot、layout_shot）
assets/            截图
```
