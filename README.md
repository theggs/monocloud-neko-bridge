# MonoCloud → Neko Bridge

把 Linux MonoCloud / Mihomo 和 macOS MonoProxy 的流量接入 [Neko Master](https://github.com/foru17/neko-master)，复用官方 neko-agent 完成增量计算、重试与上报。

## 组件

- **Linux bridge**：将现有 Mihomo Unix socket 转为带 Bearer 鉴权的本机只读 HTTP 接口。
- **macOS adapter**：订阅 MonoProxy 本地 socket，转换为 Agent 可读的连接快照，并短暂保留关闭记录。
- **官方 Agent**：每台设备独立安装、使用独立 backend/token，连接同一个 Master。

运行时只依赖 Python 标准库。当前安装脚本固定官方 Agent `agent-v1.4.8`，接口核对基于 Master `v1.4.3`；升级前核对上游兼容要求。Mac IPC 已针对 MonoProxy `1.3.0` 验证，不是官方稳定 API。

## 开始使用

先准备可访问的 Master、独立 Agent backend 和已运行的代理客户端，然后按[统一部署运维指南](docs/operations.md)安装对应采集端。

Linux 提供 [agent.env.example](agent.env.example) 配置模板。Mac 安装器要求显式传入 Master URL，可通过受限权限文件传入管理 Bearer token，不包含任何默认私有服务器地址。

## 数据范围

- 包含引擎可见的域名、目标 IP（若上游提供）、客户端源 IP、代理/策略别名、规则和流量；不保留进程身份。
- 累计字节原样传给 Agent，避免双重求差。
- 不采集绕过引擎的流量，不将代理服务器 IP 冒充目标 IP。
- Linux 快照可能漏采短连接与尾部字节；Mac 元数据可能延迟到关闭时才齐全。
- Mac 辅助规则/代理接口只是观察子集，不能当成客户端完整配置。
- 内存缓存与内存上报队列不保证断线或崩溃后的补传；独立 UDP 覆盖未完整验证。

## 开发与验证

```sh
python3 -B -m unittest discover -s tests -v
```

测试使用合成数据和临时 socket，不依赖真实账号、节点或 Master。私有配置、日志、数据库及本地调查记录不得提交。`.gitignore` 保留配置模板并忽略真实 `agent.env` / `*.env`。

这是一项非官方互操作项目，与 MonoCloud、MonoProxy、Chute 或 Neko 上游没有隶属关系。上游 Agent 由安装脚本从官方发布下载，不包含在仓库中。

## 许可证

[MIT](LICENSE)。上游客户端与 Agent 的权利及许可证归其各自项目所有。
