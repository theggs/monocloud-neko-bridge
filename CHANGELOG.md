# 版本记录

本文件记录本项目自身的发布历史，与上游 neko-agent 的版本号无关。

## 0.0.2 - 2026-11-02

### 升级提示

- **本项目的安装器不再安装或管理 Agent。** Agent 请按官方文档安装；从 0.0.1 升级时，按[升级指南](docs/upgrade.md#从-001-升级到-002)迁移并删除旧的 Agent 服务和文件。
- 旧版本安装：如果升级时提示找不到安装记录，加上 `--adopt` 重跑。

### 新增

- 切换到 0.0.2 版本后，运行 `python3 -B deploy/upgrade.py` 升级采集端；升级前自动备份，失败时自动回滚。详见[升级指南](docs/upgrade.md)。
- 安装采集端时加 `--install-agent`，会一并安装官方 Agent；不加则只显示官方安装步骤。

### 变更

- 本项目只负责采集端；Neko Master 和 neko-agent 都按官方文档安装和升级。顺序是：先安装 Neko Master，再安装采集端，最后安装 neko-agent。

### 修复

- macOS 上启动采集端时不再弹出“本地网络”授权提示（#1）；已在 macOS 与 Linux 验证。

## 0.0.1 - 2026-11-01

首个公开版本。

### 新增

- Linux bridge：把 Mihomo 的 Unix socket 转为仅监听回环地址、带 Bearer 鉴权的只读 HTTP 接口，保留累计字节计数，上游异常时显式返回 5xx。
- macOS adapter：订阅 MonoProxy 本地 socket，输出 Agent 可读的连接快照，暂存未就绪元数据、短暂保留关闭记录，并在缓存超限、分页未完成或计数回退时显式降级。
- Linux 安装器：发布专用程序与两份用户级 systemd service，安装前校验官方 Agent 发行包与校验和，失败时可 `--repair`。
- macOS 安装器：创建独立 backend，原子发布程序、配置与 LaunchAgent plist，支持管理 Bearer token 文件与受限权限安装记录。
- `agent.env.example` 配置模板与[部署运维指南](docs/operations.md)。
- 基于合成数据的单元测试，覆盖采集、鉴权、安装事务与启动器行为。

### 已知限制

- 代理名称不能保证区分实际节点；Mac 部分连接缺少目标 IP 与地区。
- 采集缓存与上报队列在内存中，不保证完整字节审计。
- 固定官方 Agent `agent-v1.4.8`，接口核对基于 Master `v1.4.3`；Mac IPC 非官方稳定 API。

详细边界见 [README](README.md#已知限制) 与[部署运维指南](docs/operations.md)。
