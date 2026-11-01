# 版本记录

本文件记录本项目自身的发布历史，与上游 neko-agent 的版本号无关。

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
