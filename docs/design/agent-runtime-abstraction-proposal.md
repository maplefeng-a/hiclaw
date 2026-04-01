# HiClaw Agent Runtime 抽象层设计提案

## 1. 背景与动机

### 1.1 现状

HiClaw 当前架构中，运行时选择（OpenClaw vs CoPaw）以硬编码方式散布在多处：

| 位置 | 问题 |
|------|------|
| `create-worker.sh` | 直接判断 `runtime=openclaw\|copaw`，走不同容器启动路径 |
| `generate-worker-config.sh` | 只生成 `openclaw.json`，CoPaw 靠 `bridge.py` 二次转换 |
| `copaw/bridge.py` | Hack 式补丁：直接修改 CoPaw 模块级常量（`patch_copaw_paths`） |
| `hiclaw-controller` Reconciler | 调用 shell 脚本，脚本内部含 runtime 分支逻辑 |

### 1.2 目标

引入 Agent Runtime 抽象层，将现有架构分为四个清晰的职责层：

- **Manager**：系统配置管理员，接收业务方需求，生成声明式配置，管理系统资源（MCP Server、凭证、权限等）
- **Controller**：声明式生命周期管理，负责期望状态 vs 实际状态的 reconcile
- **Runtime**：隔离各智能体框架的差异，提供统一的 Agent 启动/停止/更新能力
- **Team Leader / Worker**：具体业务承载，对底层框架零感知

**核心原则：业务层（Manager/TeamLeader/Worker）只操作 YAML 声明，不感知底层运行时。**

---

## 2. 整体架构

```
┌────────────────────────────────────────────────────────────────┐
│                      Business Layer                            │
│                                                                │
│   Manager              Team Leader           Worker            │
│   ─ 接收业务需求         ─ 团队任务分解         ─ 具体业务执行   │
│   ─ 生成声明式 YAML      ─ 子任务分配调度       ─ 技能 / MCP 调用│
│   ─ 系统资源管理                                               │
│                                                                │
│             只操作 YAML/CRD，不感知底层运行时                   │
└───────────────────────┬────────────────────────────────────────┘
                        │ 声明式 YAML (Worker / Team / Human CRD)
                        ▼
┌───────────────────────────────────┐
│        Controller Layer (Go)      │
│                                   │
│  WorkerReconciler                 │
│  TeamReconciler      期望状态 vs  │
│  HumanReconciler     实际状态     │
│                                   │
│  将 CRD 转换为 AgentSpec，         │
│  写入 MinIO，注册 Matrix/Higress， │
│  通过 RuntimeRegistry 启动容器     │
└───────────────┬───────────────────┘
                │ AgentSpec（写入 MinIO）
                │ + docker run <runtime-image>
                │
        ┌───────┴──────────────────────────┐
        │                                  │
        ▼                                  ▼
┌───────────────────┐            ┌─────────────────────┐
│  Runtime Layer    │            │   Runtime Layer     │
│  OpenClaw         │            │   CoPaw             │
│  (Node.js)        │            │   (Python)          │
│                   │            │                     │
│  读 AgentSpec     │            │  读 AgentSpec       │
│  → openclaw.json  │            │  → config.json      │
│  → 启动框架       │            │  → providers.json   │
│                   │            │  → 启动框架         │
└───────────────────┘            └─────────────────────┘
```

**关键设计原则**：

- Controller 只做通用基础设施操作（写 MinIO、注册通信账号、docker run），**不感知框架配置格式**
- Runtime Adapter 与框架同语言，负责 Workspace 组织、Config 生成，以及**在框架内实现统一通信频道的接入**
- AgentSpec 是 Controller 与 Runtime 之间唯一的数据契约，通过 MinIO 传递
- 业务层（Manager/TeamLeader/Worker）使用统一通信抽象，不感知底层是 Matrix 还是其他 IM 系统

---

## 3. HiClaw Agent 接入要求

接入要求定义了一个 Agent 要成为合法的 HiClaw 成员必须满足哪些条件。这是 Runtime Adapter 的设计目标——不管底层用什么框架，最终都要达到这些要求。

### 3.1 核心能力要求

| 要求 | 说明 |
|------|------|
| **通信** | 通过统一通信频道收发消息，支持 @mention 唤醒，遵守 HiClaw 通信协议（完成标记、NO_REPLY 语义等）；当前实现为 Matrix，各 Runtime Adapter 负责在框架内接入该频道 |
| **模型** | 通过 Higress AI Gateway 调用 LLM，凭证以 consumer key 方式注入，Agent 不直接持有上游 API key |
| **技能** | 支持 HiClaw 标准技能格式：`skills/<name>/SKILL.md` + `skills/<name>/scripts/`，Manager 通过 MinIO 分发 |
| **MCP 工具** | 通过 `config/mcporter.json` 配置 MCP Server 访问，调用方式为 mcporter CLI |
| **记忆** | 维护 `memory/YYYY-MM-DD.md` 日志和 `memory/MEMORY.md` 长期知识，并同步回 MinIO |
| **文件同步** | 遵守 MinIO 同步边界：Manager 管理的路径只读（openclaw.json、skills/、config/）；Worker 自身产出可写回 |
| **身份** | 加载 SOUL.md 作为 system prompt，加载 AGENTS.md 作为工作指南 |

### 3.2 Workspace 标准布局（逻辑视图）

HiClaw 定义的逻辑 workspace 是框架无关的，各 Adapter 负责将其映射到框架的物理目录：

```
<workspace>/
├── SOUL.md                    # 身份 / 人格 / 行为约束
├── AGENTS.md                  # 工作指南 / 技能目录 / 通信规则
├── skills/                    # 技能集（Manager 通过 MinIO 分发）
│   └── <skill-name>/
│       ├── SKILL.md
│       └── scripts/
├── memory/                    # 记忆（Agent 读写，同步回 MinIO）
│   ├── YYYY-MM-DD.md
│   └── MEMORY.md
├── config/
│   └── mcporter.json          # MCP Server 配置（Manager 管理）
└── shared/                    # 协作空间（MinIO 共享，只读参考）
    ├── tasks/<task-id>/
    └── projects/<project-id>/
```

### 3.3 各框架与标准布局的映射

OpenClaw 和 CoPaw 对同一套逻辑文件有不同的物理布局期望，这正是 Adapter 的 Workspace 组织职责所在：

| 逻辑路径 | OpenClaw 物理路径 | CoPaw 物理路径 |
|---------|-----------------|----------------|
| workspace 根 | `~/hiclaw-fs/agents/<name>/` | `~/.copaw-worker/<name>/` |
| SOUL.md | `<root>/SOUL.md` | `<root>/SOUL.md` → 启动时复制到 `.copaw/SOUL.md` |
| AGENTS.md | `<root>/AGENTS.md` | `<root>/AGENTS.md` → 启动时复制到 `.copaw/AGENTS.md` |
| skills/ | `<root>/skills/` | `.copaw/active_skills/`（先播种框架内置，再覆盖 MinIO 技能） |
| memory/ | `<root>/memory/` | `.copaw/memory/` |
| config/mcporter.json | `<root>/config/mcporter.json` | `.copaw/config/mcporter.json` |
| 通信频道接入 | 内置 Matrix 插件，Adapter 无需额外安装 | Adapter 需将 `matrix_channel.py` 安装至 `.copaw/custom_channels/`；未来接入其他频道同理 |
| 模型凭证 | 内嵌 `openclaw.json` | 独立写入 `.copaw/.secret/providers.json` |
| 会话状态 | `~/.openclaw/agents/` | `.copaw/sessions/` |

---

## 4. AgentSpec：跨层数据契约

AgentSpec 是 Controller 生成、Runtime Adapter 消费的完备 Agent 描述。它比 Worker CRD 更底层：Worker CRD 是用户面向的声明（只写业务关心的字段），AgentSpec 是 Controller reconcile 后的完整结果——不仅继承用户声明的字段，还注入了所有运行时所需的凭证和基础设施信息。

AgentSpec 围绕"智能体"本身组织，分为四个 Agent 语义层和一个基础设施层：

```yaml
# AgentSpec 示例：写入 MinIO agents/{name}/agent-spec.yaml
name: alice
runtime: openclaw

# ── 身份 ────────────────────────────────────────────────────────
# Agent 是谁、遵循什么准则、携带什么知识
identity:
  soul: |
    你是 Alice，一名资深全栈工程师，擅长 React 和 Go。
    你只做被明确分配的任务，完成后主动汇报结果...
  instructions: minio://agents/alice/AGENTS.md   # 工作指南（AGENTS.md），inline 或 MinIO URI
  package: file://./alice-worker.zip             # 自定义技能包、领域知识库、自定义 AGENTS.md

# ── 能力 ────────────────────────────────────────────────────────
# Agent 能调用哪些工具和技能
capabilities:
  model: claude-sonnet-4-6
  skills:
    - github-operations    # 代码仓库操作
    - file-sync            # MinIO 文件同步
  mcpServers:
    - github               # GitHub MCP Server（通过 Higress 网关授权）

# ── 行为 ────────────────────────────────────────────────────────
# Agent 的运作规则和节律
behavior:
  timeout: 30m             # 单次任务超时
  maxConcurrent: 4         # 最大并发子任务数
  session:
    reset: daily           # 每日重置 session context，避免上下文污染
    resetAtHour: 4
  heartbeat:               # 仅 Manager / Team Leader 使用：主动周期检查
    interval: 1h
    prompt: "检查所有活跃任务的进展，向 Admin 汇报异常..."

# ── 通信 ────────────────────────────────────────────────────────
# Agent 与谁通信、通信边界
# 业务层只描述 channel 类型和访问控制策略，不感知底层 IM 实现
# 各 Runtime Adapter 负责在框架内接入对应频道
communication:
  channel: matrix                           # 当前：matrix；未来可扩展 dingtalk / feishu 等
  dm:
    allowFrom: [admin]                      # HiClaw 角色引用，Controller 解析为频道账号 ID
  group:
    allowFrom: [admin, manager]             # 只响应这些角色的 @mention
    requireMention: true
  historyLimit: 100                         # 启动时加载的历史消息条数

# ── 基础设施（由 Controller reconcile 时注入，对业务层不可见）──
infra:
  image: hiclaw/worker-agent:latest
  provider:
    id: higress
    baseUrl: http://aigw-local.hiclaw.io:8080/v1
    apiKey: <Controller 从 Higress 为该 Agent 申请的 consumer key>
    model:
      contextWindow: 1000000
      maxTokens: 128000
      reasoning: true
      input: [text, image]
  channel:                                  # 频道凭证，由 Controller 注册账号后写入
    type: matrix
    homeserver: http://matrix-local.hiclaw.io:18080
    accessToken: <Controller 注册 Matrix 账号后写入>
    userId: "@alice:matrix-local.hiclaw.io:18080"
    roomId: "!xxxxxx:matrix-local.hiclaw.io:18080"
    # communication.allowFrom 角色引用的实际账号 ID 解析结果
    resolvedUsers:
      admin: "@admin:matrix-local.hiclaw.io:18080"
      manager: "@manager:matrix-local.hiclaw.io:18080"
```

与现有 Worker CRD 的层次关系：

| | Worker CRD（用户声明） | AgentSpec（Controller 生成） |
|---|---|---|
| 用户填写 | model, runtime, skills, mcpServers, package | ← 直接继承，映射到 identity / capabilities |
| Controller 注入 | — | infra.provider.apiKey、infra.channel.accessToken、infra.channel.resolvedUsers、identity.instructions 内容 |
| Runtime 消费 | — | 全部字段，转换为框架原生配置（openclaw.json / copaw config 等） |

---

## 5. 各层职责

### 5.1 Controller Layer

职责范围：
- 监听 Worker/Team/Human CRD 变化，执行 reconcile
- 将 CRD 转换为完整的 AgentSpec（填入 Higress Key、Matrix Token 等运行时凭证）
- 将 AgentSpec 写入 MinIO，执行 `docker run` 启动框架容器
- 不感知任何框架的配置格式（openclaw.json、copaw config 等）

通过 `RuntimeRegistry` 按 `spec.runtime` 字段选择对应的容器镜像启动参数，各运行时只需注册镜像名称，无需在 Go 侧实现配置转换逻辑。

### 5.2 Runtime Layer

与 Controller 并列，位于根目录 `runtime/` 下，各框架用原生语言实现。每个 Adapter 的职责分为两部分：

**① 通信频道接入**：在框架内实现 HiClaw 统一通信频道，使框架能够收发 HiClaw 的业务消息
- OpenClaw：Matrix 为内置插件，Adapter 只需在 openclaw.json 中配置频道参数
- CoPaw：框架本身无 Matrix 支持，Adapter 需将 `matrix_channel.py` 安装到 `custom_channels/`
- 未来新框架：Adapter 同样负责将 HiClaw 的通信频道桥接进框架

**② Workspace 组织**：按框架约定建立目录结构，将 HiClaw 标准逻辑布局映射到框架物理路径
- 创建框架所需目录（`active_skills/`、`custom_channels/`、`.secret/` 等）
- 复制 SOUL.md、AGENTS.md 到框架期望的位置
- 播种框架内置技能（CoPaw 需先初始化 `active_skills/`，再用 MinIO 技能覆盖）

**③ Config 生成**：将 AgentSpec 转换为框架原生配置文件
- OpenClaw：生成 `openclaw.json`（channels、models、agents、session、plugins 各节）
- CoPaw：生成 `config.json`（channels snake_case）+ `.secret/providers.json`（拆分模型凭证）

| 运行时 | 位置 | 语言 |
|--------|------|------|
| OpenClaw | `runtime/openclaw/` | Node.js |
| CoPaw | `runtime/copaw/`（重构自 `copaw/bridge.py`） | Python |
| 未来框架 | `runtime/<framework>/` | 框架原生语言 |

CoPaw Adapter 重构后消除 `patch_copaw_paths` hack：working_dir 通过正式启动参数传入框架，不再运行时修改模块常量；bridge 输入从 `openclaw.json` 改为 `agent-spec.yaml`。

### 5.3 Manager Layer

Manager 职责重新定位为**系统配置管理员**：

- 接收业务方需求（自然语言），生成 Worker/Team/Human CRD YAML，通过 `hiclaw apply` 提交给 Controller
- 管理系统资源：MCP Server（直接调用 Higress API）、凭证、访问权限
- 不再直接调用 `create-worker.sh` 或感知运行时细节

```
业务方: "我需要一个前后端开发团队"
    ↓
Manager 生成 alpha-team.yaml (Team CRD)
    ↓
hiclaw apply -f alpha-team.yaml
    ↓
Controller Reconciler → AgentSpec → 各框架 Runtime Adapter → 容器启动
```

| Skill | 现在 | 升级后 |
|-------|------|--------|
| `worker-management` | 调用 `create-worker.sh` | 生成 Worker YAML → `hiclaw apply` |
| `team-management` | 调用 `create-team.sh` | 生成 Team YAML → `hiclaw apply` |
| `model-switch` | 直接修改 `openclaw.json` | 更新 Worker CRD → `hiclaw apply` |
| `mcp-server-management` | 直接 Higress API | 不变 |

### 5.4 Team Leader / Worker Layer

对 Runtime 抽象完全透明：
- 启动流程由 Runtime Adapter 负责，TeamLeader/Worker 本身无感知
- SOUL.md / AGENTS.md / Skills 继续通过 MinIO/package 分发，格式不变
- 通信协议（Matrix @mention）不变

---

## 6. 项目结构调整

`runtime/` 作为根目录下的独立模块，与 `hiclaw-controller/` 并列，包含所有框架的 Adapter 实现：

```
/
├── hiclaw-controller/        # Controller（Go）：只含通用 Launcher，不含框架适配逻辑
│   └── internal/
│       ├── runtime/          # AgentRuntime 接口 + RuntimeRegistry + 通用 Launcher 定义
│       └── controller/       # Reconciler（调用 RuntimeRegistry，不变）
│
├── runtime/                  # ★ 新增根目录模块：所有框架的 Runtime Adapter
│   ├── openclaw/             # OpenClaw Adapter（Node.js）
│   │   └── ...               # 读 agent-spec.yaml → openclaw.json → 启动 OpenClaw
│   └── copaw/                # CoPaw Adapter（Python，重构自 copaw/bridge.py）
│       └── ...               # 读 agent-spec.yaml → config.json + providers.json → 启动 CoPaw
│
├── manager/                  # Manager Agent（不变）
├── worker/                   # OpenClaw Worker 容器基础镜像（不变）
├── copaw/                    # CoPaw 容器（bridge.py 逻辑迁移到 runtime/copaw/ 后精简）
└── ...
```

各 Adapter 作为各自框架容器的 entrypoint，在容器启动时从 MinIO 读取 `agent-spec.yaml`，完成框架原生初始化。Controller 侧不再包含任何框架特定的配置生成代码。

---

## 7. 接入新运行时的成本

接入新的 Agent 框架只需两步：

1. 在 `runtime/<framework>/` 用**框架原生语言**实现 adapter：读取 `agent-spec.yaml` → 框架原生配置 → 启动框架
2. 在 `hiclaw-controller` 注册一行：框架名称 + 容器镜像名称

**Manager / TeamLeader / Worker / Controller Reconciler 均零修改。**

---

## 8. 关键设计决策

**Q1：AgentSpec 通过 MinIO 传递还是环境变量？**

通过 MinIO（路径 `agents/{name}/agent-spec.yaml`）。原因：AgentSpec 含 SOUL.md 等大字段，不适合环境变量；MinIO 本已是系统内统一存储，容器启动后可直接 watch 变更实现热更新。

**Q2：bridge.py 是重构还是废弃？**

重构。bridge.py 的核心价值（CoPaw 原生语言做格式转换）是正确的，问题是当前输入是 openclaw.json 而不是 AgentSpec，且使用了 hack 方式修改模块常量。重构后 bridge.py 升格为正式的 CoPaw Runtime Adapter，不再依赖 openclaw.json 格式。

**Q3：shell.go executor 是否废弃？**

不废弃。保留用于 Human 管理（Matrix 账号注册、邮件发送等非 Agent Runtime 操作）。废弃仅限于 worker/team 创建的 runtime 分支逻辑。

---

## 9. 与现有文档的关系

| 现有文档 | 关系 |
|---------|------|
| `docs/design/team-worker-proposal.md` | Worker/Team/Human CRD 格式不变，本提案是其 Runtime 层的补充 |
| `docs/architecture.md` | 需更新架构图，增加 Runtime Layer |
| `docs/declarative-resource-management.md` | Manager 工作流部分需更新，体现声明式配置生成路径 |

---

## 10. 迁移路径

| Phase | 内容 | 状态 |
|-------|------|------|
| 1 | 定义 AgentSpec 格式；Controller 生成 AgentSpec 写入 MinIO；`runtime/openclaw/` Node.js adapter 读取并生成 openclaw.json；WorkerReconciler 切换为 RuntimeRegistry 调用 | 待开始 |
| 2 | 创建 `runtime/copaw/`：重构 bridge.py，输入改为 AgentSpec，消除 patch_copaw_paths hack；注册 CoPaw Launcher | 待开始 |
| 3 | Manager skill 解耦：worker/team-management 改为生成 YAML + hiclaw apply | 待开始 |
| 4 | 清理旧代码（create-worker.sh runtime 分支、旧 bridge.py 逻辑）、更新文档 | 待开始 |
