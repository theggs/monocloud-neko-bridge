# 升级指南

本文说明如何升级**已安装的采集端**：Linux bridge 与 macOS adapter。Master 的升级、备份与数据库回滚按 [Neko Master 上游](https://github.com/foru17/neko-master)要求处理，不在本文范围。

初次安装请先阅读[部署与运维指南](operations.md)。

## 1. 版本与升级来源

- 版本以 Git 标签 `vX.Y.Z` 标识，源码本身即发布物，本仓库不发布二进制归档。
- 每个版本的变更与已知限制记录在 [CHANGELOG](../CHANGELOG.md)；升级前先阅读目标版本条目。
- 兼容基线：Python 3.9 或以上；Linux amd64 使用 `/usr/bin/python3`，macOS arm64 使用安装时指定的持久 Python。
- 安装器当前固定官方 Agent `agent-v1.4.8`，接口核对基于 Master `v1.4.3`；Mac IPC 针对 MonoProxy `1.3.0` 验证，不是上游稳定 API。
- 升级来源应是本仓库对应标签的代码。不要使用来源不明的压缩包或补丁。

## 2. 升级前的准备

1. 确认目标版本要求：Python 版本、官方 Agent 版本、配置项和协议是否变化（见 CHANGELOG 与[已知限制](../README.md#已知限制)）。
2. 选择低流量时段。bridge/adapter 与 Agent 的缓存、上报队列只在内存中，重启会丢失尚未上报的数据。
3. 备份专用安装目录、私有配置、service/plist 和安装记录。备份目录必须为 `0700`，因为其中包含令牌。

Linux：

```sh
stamp=$(date +%Y%m%d-%H%M%S)
bak="$HOME/.local/state/monocloud-neko-upgrade/$stamp"
install -d -m 700 "$bak"
cp -a "$HOME/.local/lib/monocloud-neko-bridge" "$bak/linux-lib"
cp -a "$HOME/.config/monocloud-neko-bridge" "$bak/linux-config"
cp -a "$HOME/.config/systemd/user/monocloud-neko-bridge.service" \
      "$HOME/.config/systemd/user/monocloud-neko-agent.service" "$bak/"
```

macOS：

```sh
stamp=$(date +%Y%m%d-%H%M%S)
bak="$HOME/.local/state/monocloud-neko-upgrade/$stamp"
install -d -m 700 "$bak"
cp -a "$HOME/.local/lib/monocloud-neko-bridge-mac" "$bak/mac-lib"
cp -a "$HOME/.config/monocloud-neko-bridge-mac" "$bak/mac-config"
cp -a "$HOME/Library/LaunchAgents/io.monocloud-neko.bridge.plist" \
      "$HOME/Library/LaunchAgents/io.monocloud-neko.agent.plist" "$bak/"
```

备份含 `agent.env` 与 `install-state.json`，两者都含令牌：不要提交、上传或粘贴到 issue。

## 3. 获取目标版本

```sh
git -C /path/to/monocloud-neko-bridge fetch --tags
git -C /path/to/monocloud-neko-bridge checkout v0.0.2   # 替换为目标版本
```

确认工作区干净、标签与发布页一致。若目标版本有对应 GitHub Release，以该标签为准。

## 4. 停服务

先停 Agent，再停采集端，避免停服期间产生无意义的上报失败。

Linux：

```sh
systemctl --user stop monocloud-neko-agent.service monocloud-neko-bridge.service
```

macOS：

```sh
launchctl bootout "gui/$(id -u)/io.monocloud-neko.agent"
launchctl bootout "gui/$(id -u)/io.monocloud-neko.bridge"
```

## 5. 替换专用文件

只替换本项目的专用安装目录。不要修改代理客户端、其他 Agent 或其他 backend，也不要手工替换 `neko-agent`（见第 9 节）。下面的文件清单对应当前版本；清单以目标版本安装器的发布列表为准。

Linux：替换程序文件和 `service` 文件（第 2 节已备份，覆盖是安全的；内容未变化时覆盖也没有影响）。

```sh
install -m 0644 linux/bridge.py linux/run_agent.py \
  "$HOME/.local/lib/monocloud-neko-bridge/"
install -m 0644 deploy/monocloud-neko-bridge.service \
  deploy/monocloud-neko-agent.service "$HOME/.config/systemd/user/"
```

macOS：安装器会把 `linux/bridge.py` 复制为 mac 端的 `bridge.py`，升级时同样需要复制。

```sh
install -m 0644 mac/adapter.py mac/protocol.py mac/launcher.py \
  "$HOME/.local/lib/monocloud-neko-bridge-mac/"
install -m 0644 linux/bridge.py \
  "$HOME/.local/lib/monocloud-neko-bridge-mac/bridge.py"
```

macOS 的 plist 由安装器按绝对路径与 Python 路径生成，仓库中不含模板。目标版本如果没有要求修改 plist，就保持原文件；如果要求修改，按当前 plist 的字段手工调整，或按[运维指南](operations.md#4-macos-安装与服务)重新安装到新目录并迁移配置。更换 Python 路径同样属于这种情况。

## 6. 重载并启动

Linux：先起 bridge，确认监听后再起 Agent。

```sh
systemctl --user daemon-reload
systemctl --user start monocloud-neko-bridge.service
ss -lnt | rg ':19092 '
systemctl --user start monocloud-neko-agent.service
```

macOS：

```sh
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.monocloud-neko.bridge.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.monocloud-neko.agent.plist"
```

macOS 的 launcher 会轮询本机 `/health`（最多约 30 秒）后才拉起 Agent，因此即使两条命令连续执行，也有启动顺序保证。刚 `bootout` 后立即 `bootstrap` 可能报错误 5，先确认状态并等待后重试，不要改用 root 加载。

## 7. 验证

```sh
systemctl --user is-active monocloud-neko-bridge.service monocloud-neko-agent.service
```

macOS 用 `launchctl print "gui/$(id -u)/io.monocloud-neko.bridge"` 与 `...agent` 查看状态，用 `lsof -nP -iTCP:19093 -sTCP:LISTEN` 确认监听。

本机接口检查（Linux 为 `19092`，macOS 为 `19093`）。令牌从私有配置读取，不打印到终端：

```sh
set -a
. "$HOME/.config/monocloud-neko-bridge/agent.env"   # macOS 改为 monocloud-neko-bridge-mac，URL 端口改为 19093
set +a
curl -sS -H "Authorization: Bearer $NEKO_GATEWAY_TOKEN" http://127.0.0.1:19092/health
```

令牌会像 Agent 一样通过命令行参数传给 `curl`，具备本机进程查看权限的账户可能读到；不要在其他人的终端或会话共享场景执行。

macOS 升级后还需要在 Master 管理页面确认目标 backend 有新数据。注意心跳正常不等于采集成功；检查采集接口状态和日志中的错误码。`401` 表示本机网关令牌错误，不能用管理令牌替代。

## 8. 回滚

服务异常时按备份恢复，再按第 6 节重启。以下命令沿用第 2 节同一 shell 会话中的 `$bak`，先用 `test -d` 确认备份存在。

Linux：

```sh
test -d "$bak/linux-lib" || { echo "backup missing"; exit 1; }
systemctl --user stop monocloud-neko-agent.service monocloud-neko-bridge.service
rm -rf "$HOME/.local/lib/monocloud-neko-bridge"
cp -a "$bak/linux-lib" "$HOME/.local/lib/monocloud-neko-bridge"
rm -rf "$HOME/.config/monocloud-neko-bridge"
cp -a "$bak/linux-config" "$HOME/.config/monocloud-neko-bridge"
cp -a "$bak"/monocloud-neko-*.service "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user start monocloud-neko-bridge.service
systemctl --user start monocloud-neko-agent.service
```

先删除再整目录恢复，可以避免新版本新增的文件残留。macOS：

```sh
test -d "$bak/mac-lib" || { echo "backup missing"; exit 1; }
launchctl bootout "gui/$(id -u)/io.monocloud-neko.agent"
launchctl bootout "gui/$(id -u)/io.monocloud-neko.bridge"
rm -rf "$HOME/.local/lib/monocloud-neko-bridge-mac"
cp -a "$bak/mac-lib" "$HOME/.local/lib/monocloud-neko-bridge-mac"
rm -rf "$HOME/.config/monocloud-neko-bridge-mac"
cp -a "$bak/mac-config" "$HOME/.config/monocloud-neko-bridge-mac"
cp -a "$bak"/io.monocloud-neko.*.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.monocloud-neko.bridge.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/io.monocloud-neko.agent.plist"
```

回滚不要删除 Master 上的 backend 或历史数据；这些与采集端版本无关。

## 9. 关于安装器、安装记录与 Agent

- **安装器不用于升级。** `deploy/install-linux.py` 与 `deploy/install-mac.py` 只负责新安装和中断恢复，遇到已存在且不匹配的文件会拒绝覆盖。
- **`--repair` 会拒绝已升级的安装。** 手动替换文件后，`install-state.json` 记录的文件校验值不再匹配，`--repair` 会以 `existing_installation_file_conflict` 停止；如果只更新仓库、安装目录仍是旧文件，而新版本改动了这些已发布文件，则会以 `installation_payload_changed` 停止。这是保护行为，不影响已发布程序运行。
- `install-state.json` 含 backend 与网关令牌，权限必须保持 `0600`，并与配置一起备份。
- **官方 Agent 的版本固定在安装器中。** 升级 Agent 需要修改 `deploy/install_support.py` 的 `VERSION`、两个安装器中的 `ASSET`，以及 `install-linux.py` / `install-mac.py` 中 `--version` 校验的硬编码版本字面量，再核对上游发布包的版本与 SHA256，并重新验证安装流程；不能只改文档版本号，也不建议手工替换 `neko-agent`（会绕过校验，并使 `release.txt` 与实际不符）。
- 如果目标版本固定了新的 Agent，现有安装无法只替换程序文件来升级 Agent，需要按[运维指南](operations.md)重新安装并迁移配置。macOS 会拒绝接管归属不同的同名 backend，可能需要新的 backend 名称；Master 上旧 backend 的历史数据保留与否由使用方决定。

## 10. 升级期间的数据

- 重启会丢弃内存中的缓存与未上报队列，Linux 短连接也可能漏采；对完整字节审计有要求的场景，应在维护窗口升级并记录缺口。
- Master 的历史数据、backend 和令牌不受采集端升级影响；升级也不需要重新创建 backend。
- 升级完成后检查 `release.txt`（Agent 版本与校验值）和当前 Git 标签，确认运行的是预期版本。`release.txt` 不记录 bridge/adapter 版本，建议在私有记录中同时记下本次升级的标签与时间。
