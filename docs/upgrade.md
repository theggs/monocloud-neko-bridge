# 升级指南

升级分两部分：**采集端**（bridge/adapter）用本仓库脚本，**Agent** 用上游 `nekoagent`。两者互不管理对方的文件或版本。

## 前置条件

- 已按[部署与运维指南](operations.md)部署过采集端。正常升级要求私有目录中存在 `install-state.json`；旧版本安装没有该文件时见下文的 `--adopt`。
- Python 3.9 或以上，与当前安装使用的解释器一致（macOS 以安装记录或现有 plist 中的路径为准）。
- 目标版本的代码已检出到本仓库。

## 升级采集端

```sh
git -C /path/to/monocloud-neko-bridge fetch --tags
git -C /path/to/monocloud-neko-bridge checkout v0.0.2   # 替换为目标版本
cd /path/to/monocloud-neko-bridge
python3 -B deploy/upgrade.py --dry-run   # 预览会修改哪些文件、重启哪些作业
python3 -B deploy/upgrade.py
```

脚本按顺序完成：

1. 检查 `agent.env` 是否具备新版本所需的必填项；缺少时列出键名，并在改动任何文件前退出。
2. 核对已安装的采集端文件与安装记录一致，避免覆盖被手工改动的内容。
3. 备份将要修改的文件与 `install-state.json` 到 `~/.local/state/monocloud-neko-upgrade/<时间>/`。
4. 只替换内容确实变化的采集端文件；Linux 的 service、macOS 的 plist 一并处理。
5. 只在采集端作业原本运行时停止并重启它。
6. 检查本机 `/health`；失败时自动恢复备份并重启旧版本。

脚本不下载、不替换、不启动 `neko-agent`，也不读写 Agent 的实例配置。

## 不需要做的事

- 不需要编辑 `agent.env`、令牌或 Master backend；脚本不会改写配置，也不会重新创建 backend。
- 不需要手工对比配置文件：必填项检查是自动的。可选项与行为变化见 [CHANGELOG](../CHANGELOG.md)。
- 不需要处理 Agent 二进制或它的 systemd/launchd 定义。

## 失败处理

脚本失败时会自动回滚并打印备份路径；备份不会被删除，确认新版本可用后可自行清理。

| 提示 | 处理 |
|---|---|
| `private_config_not_ready` | 按列出的键补齐 `agent.env` 后重跑 |
| `insecure_or_missing_private_config` | `agent.env` 缺失，或权限不是 `0600` |
| `existing_installation_file_conflict` | 采集端文件被手工改过；恢复该文件后重跑 |
| `installation_locked` | 另一个安装/升级进程正在运行；等待后重跑 |
| `installation_not_found` | 安装记录缺失；旧安装确认目录可信后可加 `--adopt`，否则按运维指南重新安装 |
| `legacy_installation_incomplete` | `--adopt` 找不到现有 bridge 程序；目录不是完整安装，按运维指南重新安装 |
| `incomplete_installation_journal` | 上次安装未完成；先用对应安装器 `--repair` 恢复，再重新升级 |
| `gateway_token_rejected` | 本机网关令牌与配置不一致；确认 `NEKO_GATEWAY_TOKEN` 后重跑 |
| `health_check_timeout` / `health_check_failed` | 已自动回滚；检查客户端 socket 与日志后重跑 |
| `persistent_python_is_not_executable` | macOS 安装记录或 plist 指向的 Python 不可用；按运维指南重新安装 |
| `persistent_python_not_recoverable` | macOS 现有 plist 缺失或无法解析；按运维指南重新安装 |
| `unsupported_platform` | 只支持 Linux amd64 与 macOS arm64 |

## 需要手工处理的情况

- **macOS 需要重建 plist 或更换 Python 路径**：脚本会报错而不是猜测；按[运维指南](operations.md#4-macos-安装与服务)重新安装并迁移配置。
- **旧安装没有 `install-state.json`**：默认拒绝升级以保证安全。确认升级的是本项目的专用目录后，可按上文加 `--adopt`（不校验已装文件），否则按运维指南重新安装。

## 从 0.0.1 升级到 0.0.2

0.0.1 及更早版本会同时安装并管理 `neko-agent`、Agent 的 systemd unit / LaunchAgent、`run_agent.py` 和 `release.txt`。本节把 Agent 交还官方，并把采集端升级到 0.0.2。

1. 切换到 0.0.2，并在仓库根目录升级采集端：

   ```sh
   git fetch --tags
   git checkout v0.0.2
   python3 -B deploy/upgrade.py
   ```

   0.0.1 安装器创建的安装带 `install-state.json`，通常可以直接升级；如果提示找不到安装记录，按上文加 `--adopt` 重跑。升级脚本不会删除旧 Agent 文件。
2. 停用并移除旧 Agent 作业：

   Linux：

   ```sh
   systemctl --user disable --now monocloud-neko-agent.service
   rm -f "$HOME/.config/systemd/user/monocloud-neko-agent.service"
   systemctl --user daemon-reload
   ```

   macOS：

   ```sh
   launchctl bootout "gui/$(id -u)/io.monocloud-neko.agent"
   rm -f "$HOME/Library/LaunchAgents/io.monocloud-neko.agent.plist"
   ```

3. 按上游命令重新安装 Agent。安装器会打印同样的命令，可加 `--repair` 重新运行以读取现有配置（不会重复发布文件）：

   ```sh
   python3 -B deploy/install-linux.py --repair
   ```

   macOS 使用相同安装参数加 `--repair`。也可以直接使用[上游安装指南](https://github.com/foru17/neko-master/blob/main/docs/agent/install.md)中的一条命令，填入本机 `agent.env` 中的网关地址与令牌。

   命令中的 `NEKO_AGENT_CONFIG_DIR` 与 `NEKO_AGENT_STATE_DIR` 需要加入 shell profile；`nekoagent` 的 `start/status/logs/upgrade` 每次运行都会读取这两个变量，不会从安装结果中恢复。

4. 确认上报恢复后，删除旧文件：

   ```sh
   rm -f "$HOME/.local/lib/monocloud-neko-bridge/run_agent.py" \
         "$HOME/.local/lib/monocloud-neko-bridge/neko-agent" \
         "$HOME/.local/lib/monocloud-neko-bridge/release.txt"
   ```

   macOS 将路径换成 `monocloud-neko-bridge-mac`。这些文件已不再被安装器或升级脚本使用。

5. Linux 的开机自启：上游 `nekoagent enable-autostart` 使用系统级 systemd，需要 root；不使用 root 时可用 `nekoagent start <instance>` 运行，但不会随开机启动。macOS 使用用户级 LaunchAgents，保持非 root。

## Agent 的升级与回滚

- 升级：`nekoagent upgrade`（全部实例）或 `nekoagent upgrade agent-vX.Y.Z`（指定版本）。
- 回滚：用同一命令指定旧版本。
- 查看状态与日志：`nekoagent status <instance>`、`nekoagent logs <instance>`。

本仓库不再校验或记录 Agent 版本；升级前请核对上游的版本与兼容说明。

## 关于安装器与回滚

- 安装器只用于新安装和中断修复，不用于升级。手动替换文件后，`--repair` 会因校验值不匹配而拒绝继续，这是保护行为，不影响已发布程序的运行。
- 自动回滚失败或需要手工恢复时，备份目录中的 `index.json` 记录了每个文件的原路径、备份文件名和权限；`exists` 为 `false` 表示升级前该文件不存在，手工恢复时删除它即可。

## 升级期间的数据

- 重启会丢弃内存中的缓存与未上报队列，Linux 短连接也可能漏采；请在低流量时段升级。
- Master 的历史数据、backend 和令牌不受采集端升级影响，也不需要重建 backend。
- `install-state.json` 的 `bridge_version` 记录本次升级的仓库版本（`--adopt` 的旧安装没有该字段）。

## 维护者：调整建议的 Agent 版本

修改 `deploy/install_support.py` 的 `PINNED_AGENT_VERSION`，它只影响安装器打印/执行的官方命令中的 `NEKO_AGENT_VERSION`，不再参与下载或校验。仍需核对上游发布说明与架构，并运行 `python3 -B -m unittest discover -s tests -v`。
