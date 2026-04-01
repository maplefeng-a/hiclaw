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

AgentSpec 是 Controller 生成、Runtime 消费的完备配置描述。它比 Worker CRD 更底层：Worker CRD 是用户面向的声明（只写业务关心的字段），AgentSpec 是 Controller reconcile 后注入全部运行时信息（Matrix 凭证、Gateway Key 等）的完整描述。

```yaml
# AgentSpec 示例：写入 MinIO agents/{name}/agent-spec.yaml
name: alice
image: hiclaw/worker-agent:latest   # 由 runtime 字段决定，Controller 填入

# 模型：provider-id/model-id 复合键
model: higress/claude-sonnet-4-6

# Provider：Controller 从 Higress 获取 consumer key 后填入
provider:
  id: higress
  baseUrl: http://aigw-local.hiclaw.io:8080/v1
  apiKey: <higress-consumer-key>    # Controller reconcile 时从 Higress 申请后写入
  model:
    id: claude-sonnet-4-6
    name: claude-sonnet-4-6
    contextWindow: 1000000
    maxTokens: 128000
    reasoning: true
    input: [text, image]

# Matrix 频道：Controller 注册账号后填入凭证
matrix:
  homeserver: http://matrix-local.hiclaw.io:18080
  accessToken: <matrix-access-token>  # Controller 注册账号后写入
  encryption: false
  dm:
    allowFrom:
      - "@admin:matrix-local.hiclaw.io:18080"
  groupAllowFrom:
    - "@admin:matrix-local.hiclaw.io:18080"
    - "@manager:matrix-local.hiclaw.io:18080"
  historyLimit: 100

# Agent 行为
behavior:
  timeoutSeconds: 1800
  maxConcurrent: 4
  # heartbeat 仅 Manager / Team Leader 使用
  heartbeat:
    every: 1h
    prompt: "检查所有活跃任务的进展..."

# 身份文件（inline 内容或 MinIO URI，Controller 从 package 解析后写入）
soul: |
  你是 alice，一名全栈工程师...
agents: minio://agents/alice/AGENTS.md

# 技能与 MCP（名称列表，Controller 负责分发文件和配置 Higress 权限）
skills:
  - github-operations
  - file-sync
mcpServers:
  - github

# 自定义 package（Controller reconcile 时解压合并到 MinIO 空间）
package: file://./alice-worker.zip
```

与现有 Worker CRD 的层次关系：

| 字段来源 | Worker CRD（用户声明） | AgentSpec（Controller 生成） |
|---------|----------------------|---------------------------|
| 用户填写 | model, runtime, skills, mcpServers, package | ← 直接继承 |
| Controller 填入 | — | provider.apiKey、matrix.accessToken、soul/agents 内容、image |
| Runtime 消费 | — | 全部字段，转换为框架原生配置 |

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
| OpenClaw | `openclaw/src/adapter/` | Node.js | 读 AgentSpec → 生成 `openclaw.json` → 启动 OpenClaw |
| CoPaw | `copaw/src/copaw_worker/bridge.py`（重构） | Python | 读 AgentSpec → 生成 `config.json` + `providers.json` → 启动 CoPaw |
| 未来框架 | `<framework>/src/adapter/` | 框架原生语言 | 同上，只需实现本层逻辑 |

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

```
hiclaw-controller/
├── internal/
│   ├── runtime/              # 通用 Runtime 抽象（接口 + AgentSpec 定义 + Registry + 通用 Launcher）
│   │   └── launcher.go       # 只含通用逻辑：写 MinIO + 注册 Matrix/Higress + docker run
│   ├── controller/           # Reconciler（不变，改为通过 RuntimeRegistry 调用）
│   └── ...

openclaw/
└── src/adapter/              # ★ 新增，Node.js 实现
    └── index.js              # 读 agent-spec.yaml → openclaw.json → 启动 OpenClaw

copaw/src/copaw_worker/
└── bridge.py                 # ★ 重构：输入改为 agent-spec.yaml，消除 patch hack
```

Controller 侧 `internal/runtime/` 不再包含 openclaw/、copaw/ 子目录，各框架的配置生成逻辑归还给各框架自己。

---

## 6. 接入新运行时的成本

接入新的 Agent 框架只需两步：

1. 在框架目录用**框架原生语言**实现 entrypoint adapter：读取 `agent-spec.yaml` → 框架原生配置 → 启动框架
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
| 1 | 定义 AgentSpec 格式；Controller 生成 AgentSpec 写入 MinIO；OpenClaw entrypoint adapter（Node.js）读取并生成 openclaw.json；WorkerReconciler 切换为 RuntimeRegistry 调用 | 待开始 |
| 2 | 重构 CoPaw bridge.py：输入改为 AgentSpec，消除 patch_copaw_paths hack；注册 CoPaw Launcher | 待开始 |
| 3 | Manager skill 解耦：worker/team-management 改为生成 YAML + hiclaw apply | 待开始 |
| 4 | 清理旧代码（create-worker.sh runtime 分支、旧 bridge.py 逻辑）、更新文档 | 待开始 |
