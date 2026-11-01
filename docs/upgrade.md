# 升级指南

采集端（Linux bridge / macOS adapter）升级只需要一条命令。配置、backend 和令牌都不需要改动。

## 前置条件

- 已按[部署与运维指南](operations.md)安装过。正常升级要求私有目录中存在 `install-state.json`；旧版本安装没有该文件时见下文的 `--adopt`。
- Python 3.9 或以上，与当前安装使用的解释器一致（macOS 以安装记录或现有 plist 中的路径为准）。
- 目标版本的代码已检出到本仓库。

## 升级

```sh
git -C /path/to/monocloud-neko-bridge fetch --tags
git -C /path/to/monocloud-neko-bridge checkout v0.0.2   # 替换为目标版本
cd /path/to/monocloud-neko-bridge
python3 -B deploy/upgrade.py --dry-run   # 预览会修改哪些文件、重启哪些服务
python3 -B deploy/upgrade.py
```

脚本按顺序完成：

1. 检查 `agent.env` 是否具备新版本所需的必填项；缺少时列出键名，并在改动任何文件前退出。
2. 核对已安装文件与安装记录一致，避免覆盖被手工改动的内容。
3. 备份将要修改的文件与 `install-state.json` 到 `~/.local/state/monocloud-neko-upgrade/<时间>/`。
4. 只替换内容确实变化的程序文件；Linux 的 service、macOS 的 plist 一并处理。
5. 目标版本固定了新的官方 Agent 时，按上游 `checksums.txt` 下载、校验版本后替换。
6. 只在服务原本运行时停止并重启它们，且先起 bridge、后起 Agent。
7. 检查本机 `/health`；失败时自动恢复备份并重启旧版本。

需要限制下载出口时加 `--download-proxy`，它只用于下载新 Agent。

如果提示 `installation_not_found`（旧版本安装没有 `install-state.json`），且确认升级的是本项目的专用目录，可以加 `--adopt` 升级。它能完成替换、备份与重启，但无法校验已装文件是否被改动；之后建议择机按运维指南重新安装，以恢复完整保护。

## 不需要做的事

- 不需要编辑 `agent.env`、令牌或 Master backend；脚本不会改写配置，也不会重新创建 backend。
- 不需要手工对比配置文件：必填项检查是自动的。可选项与行为变化见 [CHANGELOG](../CHANGELOG.md)。
- 不需要手工替换 `neko-agent` 或逐个复制文件。

## 失败处理

脚本失败时会自动回滚并打印备份路径；备份不会被删除，确认新版本可用后可自行清理。

| 提示 | 处理 |
|---|---|
| `private_config_not_ready` | 按列出的键补齐 `agent.env` 后重跑 |
| `insecure_or_missing_private_config` | `agent.env` 缺失，或权限不是 `0600` |
| `existing_installation_file_conflict` | 安装目录被手工改过；恢复该文件后重跑 |
| `installation_locked` | 另一个安装/升级进程正在运行；等待后重跑 |
| `installation_not_found` | 安装记录缺失；旧安装确认目录可信后可加 `--adopt`，否则按运维指南重新安装 |
| `incomplete_installation_journal` | 上次安装未完成；先用对应安装器 `--repair` 恢复，再重新升级 |
| `gateway_token_rejected` | 本机网关令牌与配置不一致；确认 `NEKO_GATEWAY_TOKEN` 后重跑 |
| `health_check_timeout` / `health_check_failed` | 已自动回滚；检查客户端 socket 与日志后重跑 |
| `persistent_python_is_not_executable` | macOS 记录的 Python 已被移除；按运维指南重新安装 |
| `agent_binary_missing` / `missing_recorded_backend` | 安装目录或安装记录不完整；按运维指南重新安装 |
| `unsupported_platform` | 只支持 Linux amd64 与 macOS arm64 |

## 需要手工处理的情况

- **macOS 需要重建 plist 或更换 Python 路径**：脚本会报错而不是猜测；按[运维指南](operations.md#4-macos-安装与服务)重新安装并迁移配置。
- **目标版本要求新 Agent，但下载受限**：使用 `--download-proxy`；仍不可行时按运维指南重新安装。
- **旧安装没有 `install-state.json`**（不是当前安装器创建的）：脚本不会接管，按运维指南重新安装并迁移配置。

## 关于安装器与回滚

- 安装器只用于新安装和中断修复，不用于升级。手动替换文件后，`--repair` 会因校验值不匹配而拒绝继续，这是保护行为，不影响已发布程序的运行。
- 自动回滚失败或需要手工恢复时，备份目录中的 `index.json` 记录了每个文件的原路径、备份文件名和权限，按记录覆盖回去再重启服务即可。

## 升级期间的数据

- 重启会丢弃内存中的缓存与未上报队列，Linux 短连接也可能漏采；请在低流量时段升级。
- Master 的历史数据、backend 和令牌不受采集端升级影响，也不需要重建 backend。
- `release.txt` 只记录 Agent 版本与校验值；本次升级的仓库版本记录在 `install-state.json` 的 `bridge_version` 中（用 `--adopt` 升级的旧安装没有安装记录，不会生成该字段）。

## 维护者：提升固定 Agent 版本

- 只需修改 `deploy/install_support.py` 的 `VERSION`：发行包名与期望的 `--version` 由 `agent_asset()` / `expected_agent_version()` 统一推导，两个安装器和升级脚本共用，不再需要分别修改硬编码字面量。
- 仍要核对上游 `checksums.txt`、架构与发布说明，并运行 `python3 -B -m unittest discover -s tests -v`。
