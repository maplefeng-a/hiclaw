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

- Controller 只做通用基础设施操作（写 MinIO、注册 Matrix 账号、docker run），**不感知框架配置格式**
- Runtime Adapter 与框架同语言，作为容器 entrypoint 的一部分，读取 AgentSpec 后自行完成框架初始化
- AgentSpec 是 Controller 与 Runtime 之间唯一的数据契约，通过 MinIO 传递

---

## 3. AgentSpec：跨层数据契约

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
communication:
  channel: matrix
  dm:
    allowFrom:
      - "@admin:matrix.hiclaw.io"
  group:
    allowFrom:                              # 只响应以下用户的 @mention
      - "@admin:matrix.hiclaw.io"
      - "@manager:matrix.hiclaw.io"
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
  matrix:
    homeserver: http://matrix-local.hiclaw.io:18080
    accessToken: <Controller 注册 Matrix 账号后写入>
    userId: "@alice:matrix-local.hiclaw.io:18080"
    roomId: "!xxxxxx:matrix-local.hiclaw.io:18080"
```

与现有 Worker CRD 的层次关系：

| | Worker CRD（用户声明） | AgentSpec（Controller 生成） |
|---|---|---|
| 用户填写 | model, runtime, skills, mcpServers, package | ← 直接继承，映射到 identity / capabilities |
| Controller 注入 | — | infra.provider.apiKey、infra.matrix.accessToken、identity.instructions 内容 |
| Runtime 消费 | — | 全部字段，转换为框架原生配置（openclaw.json / copaw config 等） |

---

## 4. 各层职责

### 4.1 Controller Layer

职责范围：
- 监听 Worker/Team/Human CRD 变化，执行 reconcile
- 将 CRD 转换为完整的 AgentSpec（填入 Higress Key、Matrix Token 等运行时凭证）
- 将 AgentSpec 写入 MinIO，执行 `docker run` 启动框架容器
- 不感知任何框架的配置格式（openclaw.json、copaw config 等）

通过 `RuntimeRegistry` 按 `spec.runtime` 字段选择对应的容器镜像启动参数，各运行时只需注册镜像名称，无需在 Go 侧实现配置转换逻辑。

### 4.2 Runtime Layer

与 Controller 并列，以各框架目录为载体，用框架原生语言实现：

| 运行时 | 位置 | 语言 | 职责 |
|--------|------|------|------|
| OpenClaw | `runtime/openclaw/` | Node.js | 读 AgentSpec → 生成 `openclaw.json` → 启动 OpenClaw |
| CoPaw | `runtime/copaw/`（重构自 `copaw/bridge.py`） | Python | 读 AgentSpec → 生成 `config.json` + `providers.json` → 启动 CoPaw |
| 未来框架 | `runtime/<framework>/` | 框架原生语言 | 同上，只需实现本层逻辑 |

CoPaw 的 `bridge.py` 重构后输入从 `openclaw.json` 改为 `agent-spec.yaml`，消除 `patch_copaw_paths` hack——working_dir 通过正式启动参数传入，不再运行时修改模块常量。

### 4.3 Manager Layer

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

### 4.4 Team Leader / Worker Layer

对 Runtime 抽象完全透明：
- 启动流程由 Runtime Adapter 负责，TeamLeader/Worker 本身无感知
- SOUL.md / AGENTS.md / Skills 继续通过 MinIO/package 分发，格式不变
- 通信协议（Matrix @mention）不变

---

## 5. 项目结构调整

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

## 6. 接入新运行时的成本

接入新的 Agent 框架只需两步：

1. 在 `runtime/<framework>/` 用**框架原生语言**实现 adapter：读取 `agent-spec.yaml` → 框架原生配置 → 启动框架
2. 在 `hiclaw-controller` 注册一行：框架名称 + 容器镜像名称

**Manager / TeamLeader / Worker / Controller Reconciler 均零修改。**

---

## 7. 关键设计决策

**Q1：AgentSpec 通过 MinIO 传递还是环境变量？**

通过 MinIO（路径 `agents/{name}/agent-spec.yaml`）。原因：AgentSpec 含 SOUL.md 等大字段，不适合环境变量；MinIO 本已是系统内统一存储，容器启动后可直接 watch 变更实现热更新。

**Q2：bridge.py 是重构还是废弃？**

重构。bridge.py 的核心价值（CoPaw 原生语言做格式转换）是正确的，问题是当前输入是 openclaw.json 而不是 AgentSpec，且使用了 hack 方式修改模块常量。重构后 bridge.py 升格为正式的 CoPaw Runtime Adapter，不再依赖 openclaw.json 格式。

**Q3：shell.go executor 是否废弃？**

不废弃。保留用于 Human 管理（Matrix 账号注册、邮件发送等非 Agent Runtime 操作）。废弃仅限于 worker/team 创建的 runtime 分支逻辑。

---

## 8. 与现有文档的关系

| 现有文档 | 关系 |
|---------|------|
| `docs/design/team-worker-proposal.md` | Worker/Team/Human CRD 格式不变，本提案是其 Runtime 层的补充 |
| `docs/architecture.md` | 需更新架构图，增加 Runtime Layer |
| `docs/declarative-resource-management.md` | Manager 工作流部分需更新，体现声明式配置生成路径 |

---

## 9. 迁移路径

| Phase | 内容 | 状态 |
|-------|------|------|
| 1 | 定义 AgentSpec 格式；Controller 生成 AgentSpec 写入 MinIO；`runtime/openclaw/` Node.js adapter 读取并生成 openclaw.json；WorkerReconciler 切换为 RuntimeRegistry 调用 | 待开始 |
| 2 | 创建 `runtime/copaw/`：重构 bridge.py，输入改为 AgentSpec，消除 patch_copaw_paths hack；注册 CoPaw Launcher | 待开始 |
| 3 | Manager skill 解耦：worker/team-management 改为生成 YAML + hiclaw apply | 待开始 |
| 4 | 清理旧代码（create-worker.sh runtime 分支、旧 bridge.py 逻辑）、更新文档 | 待开始 |
