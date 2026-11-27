<p align="center">
  <img src="assets/banner.svg" alt="oa-git — 面向 Cadence OpenAccess 的 Git 版本控制工具" width="100%" />
</p>

<p align="center">
  <a href="README.md">English</a> &nbsp;|&nbsp; 简体中文
</p>

面向 **Cadence OpenAccess**（`sch.oa`、`symbol.oa`、`layout.oa`）的原生 Git 版本控制工具——**仅通过 Cadence 官方文档化的 SKILL API**，将 Virtuoso 二进制数据库无损转换为确定性、可读的 `YAML` / `JSONL` 文本快照，无缝接入标准 Git 协作流。

```bash
# 从 GitHub 安装（离线 EDA 服务器可从 Releases 下载 .whl 文件本地安装）
pip install git+https://github.com/wsunskyr/oa-git.git@v0.1.0

# 在包含 cds.lib 的任意 Virtuoso 工作目录下直接运行
oagit init                                                          # 自动 git init + 安全 .gitignore + 解析 cds.lib
oagit capture my_lib/opamp/schematic my_lib/opamp/symbol my_lib/opamp/layout
git add snapshots/ .gitignore .oagit/config.json && git commit -m "feat: initial baseline"
```

> **需要 Python >= 3.11。** 在 RHEL/Rocky 等 EDA 服务器上，如果 `PATH` 默认的 `python3` 版本较旧（如系统或 EDA 工具链自带的 3.8 / 3.9），建议使用 `python3.12 -m venv .venv` 或 [`uv`](https://docs.astral.sh/uv/)（`uv venv --python 3.12 --seed .venv`）创建独立虚拟环境并在其中安装，按需将 `.venv/bin/oagit` 软链到 `~/.local/bin/oagit`。

## 工作原理

<p align="center">
  <img src="assets/architecture.svg" alt="oa-git 工作原理" width="100%" />
</p>

- **极简 7 命令 CLI + 原生 Git**：仅新增 `init`、`status`、`capture`、`diff`、`merge`、`restore`、`daemon` 7 条 OA 专属命令；所有提交、分支、PR 审查与回退均直接使用原生 `git`。
- **分域语义 Diff 与三路合并**：将电气/连接真值与摆位/走线几何分离（拖动器件或整理连线不污染电气 Diff），支持耦合 CDF 参数协同合并，并拦截同层版图金属重叠冲突。
- **Fail-Closed 安全防护与极速体验**：自动拦截未保存缓冲区、活跃 `*.cdslck` 编辑锁与未授权目标库；还原过程采用“独立草稿重建 + 回读校验 + 原子发布”。本地脏索引可在 `< 0.2s` 内跳过未变更视图，懒加载无头守护进程默认保活 1 小时（`idle_timeout_s = 3600`，支持本机与 SSH 远程模式）。

## 常用命令

```bash
oagit status                         # 校验快照完整性、本地脏索引、OA 锁状态与 .gitignore 配置
oagit capture                        # 增量同步（默认 --changed，跳过未改动的视图）
oagit capture my_lib/top/schematic --hier --include-symbols  # 单会话递归抓取整棵层次树与符号
oagit diff HEAD~1..HEAD --stat       # 跨原理图、符号、版图的语义 Diff 概览
oagit diff --electrical-only         # 仅看电气变化，忽略纯摆位与整理走线
oagit diff --connectivity-only       # 版图仅看网络 / 引脚 / PCell / 通孔等连接变化
oagit restore my_lib/opamp/schematic --target my_lib/opamp/schematic --overwrite
oagit init --sync                    # 在 Virtuoso 新建 OA 库后同步更新配置与 .gitignore
```

*（如需通过 SSH 连接远程 Virtuoso 服务器，可在 `oagit init` 时传入 `--ssh-host <别名> --cds-lib /path/to/cds.lib`。如需本地开发或运行离线单测，克隆仓库后执行 `pip install -e ".[dev]" && pytest`。）*

## 参考文档

- [系统架构与 CLI 契约（英文）](docs/ARCHITECTURE.md)
- [符号视图说明 `symbol.yaml`（英文）](docs/SYMBOL.md)
- [物理版图说明 `objects.jsonl`（英文）](docs/LAYOUT.md)
- [无头守护进程、脏索引与批量协议（英文）](docs/DAEMON.md)

## 开源协议

MIT（[LICENSE](LICENSE)）—— 仅调用 Cadence Virtuoso 官方公开 SKILL API 的独立净室实现。
