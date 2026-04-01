# HiClaw Agent Runtime 抽象层设计提案

## 1. 背景与动机

### 1.1 现状

HiClaw 当前架构中，Manager/TeamLeader/Worker 的运行时选择（OpenClaw vs CoPaw）以"硬编码"方式散布在多处：

| 位置 | 现状耦合 |
|------|---------|
| `create-worker.sh` | 直接判断 `runtime=openclaw|copaw`，走不同的容器启动路径 |
| `generate-worker-config.sh` | 生成 `openclaw.json`，CoPaw 靠 `bridge.py` 二次转换 |
| `copaw/bridge.py` | Hack 式补丁：直接修改 CoPaw 模块级常量（`patch_copaw_paths`） |
| `hiclaw-controller` Reconciler | 调用 shell 脚本，脚本内部分支 openclaw/copaw |
| Worker CRD `spec.runtime` | 已有 `openclaw | copaw` 字段，但只到 controller 层，没有向下传递到统一接口 |

这带来的问题：
- **新增运行时成本高**：接入新框架需要修改多处 shell 脚本
- **Manager 感知 Runtime 细节**：Manager 的 `worker-management` skill 包含运行时相关逻辑
- **bridge.py 脆弱**：强依赖 CoPaw 内部实现路径，CoPaw 版本升级极易破坏
- **测试困难**：无法 mock Runtime，集成测试强依赖容器环境

### 1.2 目标

```
Manager / Team Leader / Worker
    ↓  只关心"声明我需要什么能力"
Controller
    ↓  只关心"期望状态 → 实际状态"的 reconcile
AgentRuntime (新增抽象)
    ↓  隔离 OpenClaw / CoPaw / 未来框架的差异
具体实现：OpenClawAdapter / CoPawAdapter / ...
```

**核心原则：业务层（Manager/TeamLeader/Worker）对 Runtime 零感知。**

> 本文档中"第三方框架"指未来可能接入的其他 Agent 运行时，不预设具体实现。

---

## 2. 整体架构

```
┌───────────────────────────────────────────────────────────────┐
│                      Business Layer                           │
│                                                               │
│   Manager          Team Leader         Worker                 │
│   ─ 配置管理        ─ 团队任务分解       ─ 具体业务执行         │
│   ─ 声明式配置生成   ─ 子任务调度        ─ 技能调用            │
│   ─ 系统管理员职责                                            │
│                                                               │
│       只操作 YAML/CRD，不感知底层 Runtime                     │
└───────────────────────┬───────────────────────────────────────┘
                        │ 声明式 YAML (Worker/Team CRD)
                        ▼
┌───────────────────────────────────┐
│         Controller Layer (Go)     │
│                                   │
│  WorkerReconciler                 │
│  TeamReconciler     ─ 期望状态 vs │
│  HumanReconciler      实际状态    │
│                                   │
│  ─ 生成 AgentSpec                 │
│  ─ 写入 MinIO                     │
│  ─ 注册 Matrix/Higress            │
│  ─ 启动容器（docker run）         │
└───────────────┬───────────────────┘
                │ AgentSpec (JSON，写入 MinIO)
                │ + docker run <runtime-image>
                │
        ┌───────┴────────────────────────────┐
        │                                    │
        ▼                                    ▼
┌───────────────────┐              ┌─────────────────────┐
│  Runtime Layer    │              │   Runtime Layer     │
│  (Node.js)        │              │   (Python)          │
│                   │              │                     │
│  openclaw/        │              │  copaw/             │
│  ─ 读 AgentSpec   │              │  ─ 读 AgentSpec     │
│  ─ 生成           │              │  ─ 生成             │
│    openclaw.json  │              │    config.json      │
│  ─ 启动 OpenClaw  │              │    providers.json   │
│    框架           │              │  ─ 启动 CoPaw 框架  │
└───────────────────┘              └─────────────────────┘
```

**关键设计原则**：
- Controller 只负责通用基础设施操作（写 MinIO、注册 Matrix 账号、docker run），**不感知框架配置格式**
- Runtime Adapter 和框架同语言，作为容器 entrypoint 的一部分，读取 AgentSpec 自行完成框架初始化
- AgentSpec（JSON 格式）是 Controller 与 Runtime 之间的唯一契约，通过 MinIO 传递

---

## 3. 各层设计

### 3.1 Runtime Layer（新增核心抽象）

Runtime Layer 与 Controller Layer **并列**，不是 Controller 的内部组件。

- **Controller（Go）**：定义 AgentRuntime 接口和 AgentSpec 协议，负责通用基础设施操作
- **各框架 Adapter（原生语言）**：实现 AgentSpec → 框架原生配置的转换，作为容器 entrypoint 的一部分

#### 3.1.1 Controller 侧：AgentRuntime 接口（通用操作）

Controller 只关心容器生命周期，不感知框架内部格式：

```go
// hiclaw-controller/internal/runtime/interface.go

type AgentRuntime interface {
    // 写入 AgentSpec 到 MinIO，注册基础设施资源，启动容器
    Create(ctx context.Context, spec AgentSpec) (*AgentInstance, error)

    // 更新 AgentSpec 并重启容器（热更新或重建）
    Update(ctx context.Context, name string, spec AgentSpec) error

    // 停止容器，清理基础设施资源（Matrix Room、Higress Consumer）
    Delete(ctx context.Context, name string) error

    // 查询容器运行状态
    GetStatus(ctx context.Context, name string) (*AgentStatus, error)

    // 运行时标识符（"openclaw" / "copaw" / ...）
    Name() string
}
```

Controller 侧的实现是一个**通用 Launcher**，各运行时只需注册镜像名称和启动参数，无需实现框架特定的配置生成：

```go
// hiclaw-controller/internal/runtime/launcher.go

type Launcher struct {
    image         string            // 框架容器镜像，e.g. "hiclaw/worker-agent"
    name          string            // 运行时标识
    dockerClient  ContainerClient
    minioClient   StorageClient
    matrixClient  MatrixClient
    higress       HigressClient
}

func (l *Launcher) Create(ctx context.Context, spec AgentSpec) (*AgentInstance, error) {
    // 1. 在 Higress 注册 Consumer，获取 gateway key
    // 2. 在 Matrix 注册账号，创建 Room，获取 access token
    // 3. 将完整 AgentSpec（含 Matrix/Higress 凭证）序列化为 JSON
    //    写入 MinIO: agents/{name}/agent-spec.json
    // 4. docker run l.image --name hiclaw-worker-{name}
    //    （容器 entrypoint 从 MinIO 读取 agent-spec.json 完成框架初始化）
    // 5. 返回 AgentInstance{MatrixUserID, RoomID, ContainerID}
}

// RuntimeRegistry 保持不变，注册 Launcher 实例
registry.Register(&Launcher{name: "openclaw", image: "hiclaw/worker-agent", ...})
registry.Register(&Launcher{name: "copaw",    image: "hiclaw/copaw-worker", ...})
```

#### 3.1.2 统一 AgentSpec（运行时无关配置）

现有 Worker CRD 的 `spec` 字段即是声明式层，AgentSpec 是 Runtime 层的内部表示，由 Controller 从 CRD 转换而来。

AgentSpec 的字段设计**以 `openclaw.json` 的实际结构为基准**，CoPawAdapter 在此基础上做格式转换（camelCase→snake_case、拆分 providers.json 等）。

```go
// internal/runtime/spec.go

// AgentSpec 对应 openclaw.json 的核心字段，剥离基础设施相关配置
// （gateway.mode/port、plugins 由各 Adapter 自行注入）
type AgentSpec struct {
    Name  string
    Image string // 可选，Adapter 有默认镜像

    // 模型选择：对应 openclaw agents.defaults.model.primary
    // 格式："provider-id/model-id"，例如 "higress/claude-opus-4-6"
    Model string

    // Provider 配置：对应 openclaw models.providers[id]
    Provider ProviderSpec

    // Matrix 频道：对应 openclaw channels.matrix
    Matrix MatrixSpec

    // Agent 行为：对应 openclaw agents.defaults
    Behavior AgentBehavior

    // 身份文件：SOUL.md / AGENTS.md（inline 内容或 MinIO URI）
    Soul   string
    Agents string

    // Skill 名称列表（Controller 负责从 MinIO 解析为实际路径）
    Skills []string

    // MCP Server 名称列表（Controller 负责在 Higress 配置权限）
    McpServers []string

    // 业务自定义包 URI（file:// / http:// / nacos://）
    Package string
}

// ProviderSpec 对应 openclaw models.providers[id] 中的连接配置
// 模型元数据（contextWindow/maxTokens/reasoning/input）由 Controller
// 从 known-models 配置解析后注入，Adapter 可按需使用
type ProviderSpec struct {
    ID      string // provider 标识，也是 openclaw 中的 provider-id
    BaseURL string // models.providers[id].baseUrl
    APIKey  string // models.providers[id].apiKey（来自 Higress consumer key）
    Model   ModelMeta
}

type ModelMeta struct {
    ID            string   // model-id 部分
    Name          string
    ContextWindow int
    MaxTokens     int
    Reasoning     bool
    Input         []string // ["text"] or ["text", "image"]
}

// MatrixSpec 对应 openclaw channels.matrix
type MatrixSpec struct {
    Homeserver     string
    AccessToken    string
    Encryption     bool
    DMAllowFrom    []string // channels.matrix.dm.allowFrom
    GroupAllowFrom []string // channels.matrix.groupAllowFrom
    HistoryLimit   int      // 可选，0 表示使用默认值
}

// AgentBehavior 对应 openclaw agents.defaults
type AgentBehavior struct {
    TimeoutSeconds int
    MaxConcurrent  int
    Heartbeat      *HeartbeatSpec // 仅 Manager/TeamLeader 使用
}

type HeartbeatSpec struct {
    Every  string // e.g. "1h"
    Prompt string
}
```

**与 openclaw.json 的字段映射关系**：

| AgentSpec 字段 | openclaw.json 路径 |
|---|---|
| `Model` | `agents.defaults.model.primary` ("provider-id/model-id") |
| `Provider.BaseURL` | `models.providers[id].baseUrl` |
| `Provider.APIKey` | `models.providers[id].apiKey` |
| `Provider.Model.*` | `models.providers[id].models[0].*` |
| `Matrix.Homeserver` | `channels.matrix.homeserver` |
| `Matrix.AccessToken` | `channels.matrix.accessToken` |
| `Matrix.DMAllowFrom` | `channels.matrix.dm.allowFrom` |
| `Matrix.GroupAllowFrom` | `channels.matrix.groupAllowFrom` |
| `Behavior.TimeoutSeconds` | `agents.defaults.timeoutSeconds` |
| `Behavior.Heartbeat` | `agents.defaults.heartbeat` |

**CoPawAdapter 的额外转换职责**（消除 bridge.py）：
- camelCase → snake_case（`accessToken` → `access_token` 等）
- `Provider.Model.Input` 含 "image" → `vision_enabled: true`
- `Provider.Model.ContextWindow` → `agents.running.max_input_length`
- 将 Provider 配置拆分写入独立的 `providers.json`

#### 3.1.3 各框架 Adapter（原生语言实现）

Adapter 不在 Controller 中，而是各框架容器自己的 entrypoint 逻辑，用框架的原生语言实现，从 MinIO 读取 AgentSpec 并完成框架初始化。

**OpenClaw Adapter（Node.js）**，位于 `openclaw/src/adapter/`：

```javascript
// openclaw/src/adapter/index.js
// 容器启动时执行，替代 generate-worker-config.sh

const spec = await loadAgentSpec(process.env.MINIO_ENDPOINT, process.env.AGENT_NAME);

// AgentSpec → openclaw.json
const config = {
  gateway: buildGatewayConfig(spec),          // mode/port 由此处注入
  channels: { matrix: buildMatrixConfig(spec.matrix) },
  models: { mode: "merge", providers: buildProviders(spec.provider) },
  agents: { defaults: buildAgentDefaults(spec.behavior, spec.model) },
  session: buildSessionConfig(),
  plugins: { load: { paths: ["/opt/openclaw/extensions/matrix"] },
             entries: { matrix: { enabled: true } } }
};

await writeToMinIO(`agents/${spec.name}/openclaw.json`, config);
await startOpenClaw();
```

**CoPaw Adapter（Python）**，即重构后的 `copaw/src/copaw_worker/bridge.py`：

```python
# copaw/src/copaw_worker/bridge.py（重构：从 AgentSpec 转换，消除 patch_copaw_paths hack）

def bridge_agentspec_to_copaw(spec: AgentSpec, working_dir: str):
    """
    AgentSpec → CoPaw config.json + providers.json
    替代原来从 openclaw.json 二次转换的方式
    """
    write_config_json(spec, working_dir)      # channels（snake_case）+ agents.running
    write_providers_json(spec, working_dir)   # 独立 providers 文件
    # 不再需要 patch_copaw_paths：working_dir 通过正式启动参数传入 CoPaw
```

两种 Adapter 的**共同输入**是 `agent-spec.json`（Controller 写入 MinIO 的 AgentSpec），**各自负责**生成框架原生配置。

#### 3.1.4 RuntimeRegistry

```go
// hiclaw-controller/internal/runtime/registry.go

type RuntimeRegistry struct {
    runtimes map[string]AgentRuntime
}

func (r *RuntimeRegistry) Register(rt AgentRuntime) {
    r.runtimes[rt.Name()] = rt
}

func (r *RuntimeRegistry) Get(name string) (AgentRuntime, error) {
    if rt, ok := r.runtimes[name]; ok {
        return rt, nil
    }
    return nil, fmt.Errorf("unknown runtime: %s", name)
}
```

主程序初始化（Controller 只注册镜像信息，无框架特定逻辑）：

```go
// hiclaw-controller/cmd/controller/main.go

registry := runtime.NewRegistry()
registry.Register(&runtime.Launcher{Name: "openclaw", Image: "hiclaw/worker-agent",    /* clients */ })
registry.Register(&runtime.Launcher{Name: "copaw",    Image: "hiclaw/copaw-worker",    /* clients */ })
// 接入新框架：registry.Register(&runtime.Launcher{Name: "xxx", Image: "hiclaw/xxx-worker", ...})
```

---

### 3.2 Controller Layer（升级现有）

**现状**：Reconciler → `executor/shell.go` → `create-worker.sh`（内含 runtime 分支，感知 openclaw.json 格式）

**升级后**：Reconciler → `RuntimeRegistry.Get(spec.runtime)` → `Launcher.Create`（写 AgentSpec + docker run，不感知框架格式）

核心变化：

```go
// internal/controller/worker_controller.go（升级前）
func (r *WorkerReconciler) reconcileCreate(ctx context.Context, worker *v1beta1.Worker) error {
    result, err := r.shell.Run(ctx, "create-worker.sh",
        "--name", worker.Name,
        "--model", worker.Spec.Model,
        "--runtime", worker.Spec.Runtime,
        // ...
    )
    // ...
}

// internal/controller/worker_controller.go（升级后）
func (r *WorkerReconciler) reconcileCreate(ctx context.Context, worker *v1beta1.Worker) error {
    rt, err := r.runtimes.Get(worker.Spec.Runtime)
    if err != nil {
        return err
    }
    spec := r.toAgentSpec(worker)
    instance, err := rt.Create(ctx, spec)
    if err != nil {
        return err
    }
    worker.Status.MatrixUserID = instance.MatrixUserID
    worker.Status.RoomID = instance.RoomID
    worker.Status.Phase = v1beta1.WorkerPhaseRunning
    return r.Status().Update(ctx, worker)
}
```

> Controller 不再生成 `openclaw.json` 或 CoPaw 配置，这些格式细节由各框架自己的 Adapter 处理。

Controller 的其他职责**不变**：
- 仍使用 kine + controller-runtime informer
- 仍支持 embedded / incluster 两种模式
- 仍使用 finalizer 确保清理顺序
- HTTP API Server (`:8090`) 保持不变

---

### 3.3 Manager Layer（升级定位）

Manager 的核心职责重新定位为**系统配置管理员**：

#### 3.3.1 接收业务需求 → 生成声明式配置

Manager 不再直接调用 `create-worker.sh` 或感知 runtime 细节。其 `worker-management` 和 `team-management` skill 的产出物是**CRD YAML 文件**，通过 `hiclaw apply` 提交给 Controller。

```
业务方: "我需要一个前后端开发团队，3个人，擅长 React + Go"
    ↓
Manager 生成:
    alpha-team.yaml  (Team CRD: leader + frontend-dev + backend-dev)
    human-john.yaml  (Human CRD: L2 权限，关联 alpha-team)
    ↓
Manager 执行: hiclaw apply -f alpha-team.yaml -f human-john.yaml
    ↓
Controller Reconciler 驱动 AgentRuntime 完成实际创建
```

#### 3.3.2 Manager 保留的系统管理职责

| 职责 | 方式 |
|------|------|
| MCP Server 管理 | 直接调用 Higress Console API（不经过 Controller，非声明式资源） |
| 模型切换 | 生成新的 Worker CRD，`hiclaw apply` 触发 Update reconcile |
| 凭证/权限管理 | 直接调用 Higress API |
| Worker 生命周期（停止/恢复） | `hiclaw apply` 修改 Worker CRD `spec.replicas=0/1`（或通过容器 API） |
| 任务状态追踪 | 继续维护 `state.json`（Manager 内部，不经过 Controller） |

#### 3.3.3 Manager Skills 改造方向

| Skill | 现在 | 升级后 |
|-------|------|--------|
| `worker-management` | 调用 `create-worker.sh` | 生成 Worker YAML → `hiclaw apply` |
| `team-management` | 调用 `create-team.sh` | 生成 Team YAML → `hiclaw apply` |
| `human-management` | 调用 `create-human.sh` | 生成 Human YAML → `hiclaw apply` |
| `model-switch` | 修改 `openclaw.json` | 更新 Worker CRD spec.model → `hiclaw apply` |
| `mcp-server-management` | 直接 Higress API | 不变（MCP 非 K8s 资源） |

---

### 3.4 Team Leader / Worker Layer

Team Leader 和 Worker **不需要感知 Runtime 层**。它们的关注点：

- 接收任务（通过 Matrix @mention）
- 调用 Skills（file-sync、github-operations 等）
- 调用 MCP Tools（通过 mcporter）
- 汇报结果

**Runtime 抽象对 TeamLeader/Worker 的影响**：
- **SOUL.md / AGENTS.md / Skills**：无变化，继续通过 MinIO/package 分发
- **启动流程**：Controller 写入 `agent-spec.json` 后 docker run，容器内的框架 Adapter 完成初始化，TeamLeader/Worker 本身无感知
- **openclaw.json**：继续作为 OpenClaw 框架的原生配置，由 OpenClaw Adapter 生成；CoPaw 有自己的 config.json，由 CoPaw Adapter 生成；两者都以 AgentSpec 为统一输入

---

## 4. 项目结构调整

Runtime Layer 与 Controller Layer 并列，分别属于各自的目录（框架目录 vs controller 目录），通过 MinIO 中的 `agent-spec.json` 协议文件交互。

### 4.1 hiclaw-controller 目录结构（升级后）

Controller 侧只保留通用运行时抽象，**不含框架特定配置生成逻辑**：

```
hiclaw-controller/
├── go.mod
├── cmd/
│   ├── controller/main.go          # 注册 Launcher 实例 + 启动 reconciler
│   └── hiclaw/main.go              # CLI 工具（apply/get/delete）
├── api/v1beta1/
│   ├── types.go                    # CRD 类型（不变）
│   └── register.go
├── internal/
│   ├── runtime/                    # ★ 通用 Runtime 抽象（不含框架特定逻辑）
│   │   ├── interface.go            # AgentRuntime 接口
│   │   ├── spec.go                 # AgentSpec 定义（跨层协议）
│   │   ├── registry.go             # RuntimeRegistry
│   │   └── launcher.go             # 通用 Launcher（写 MinIO + docker run）
│   ├── controller/
│   │   ├── worker_controller.go    # 升级：使用 RuntimeRegistry
│   │   ├── team_controller.go      # 升级：使用 RuntimeRegistry
│   │   └── human_controller.go     # 基本不变（Matrix/email 操作）
│   ├── executor/                   # 保留，用于 Human 管理等非 Runtime 操作
│   │   ├── shell.go
│   │   └── package.go
│   ├── watcher/file_watcher.go
│   ├── store/kine.go
│   ├── server/http.go
│   └── mail/smtp.go
```

### 4.2 openclaw 目录（新增 Adapter，Node.js）

OpenClaw Adapter 作为容器 entrypoint，替代 `generate-worker-config.sh`：

```
openclaw/                           # 或 worker/ 目录下
├── src/
│   ├── adapter/
│   │   ├── index.js                # ★ 新增：读 AgentSpec → 生成 openclaw.json
│   │   └── spec.js                 # AgentSpec JSON 解析
│   └── ...                         # 现有 OpenClaw 运行时代码
```

### 4.3 copaw 目录（重构 Adapter，Python）

`bridge.py` 重构为正式的 CoPaw Adapter，输入从 `openclaw.json` 改为 `agent-spec.json`，消除 `patch_copaw_paths` hack：

```
copaw/src/copaw_worker/
├── bridge.py                       # ★ 重构：AgentSpec → CoPaw config，不再需要 patch_copaw_paths
├── worker.py                       # 启动 CoPaw AgentRunner（不变）
├── matrix_channel.py               # Matrix 频道（不变）
├── sync.py                         # MinIO 同步（不变）
└── config.py                       # 读取 bridge.py 生成好的配置（简化）
```

---

## 5. 迁移路径

### Phase 1：接口定义 + OpenClaw Adapter（不破坏现有功能）

1. 在 Controller 侧定义 `AgentRuntime` 接口、`AgentSpec` 数据结构、通用 `Launcher`
2. 在 `openclaw/src/adapter/` 实现 Node.js Adapter：读取 `agent-spec.json` → 生成 `openclaw.json`
3. Controller Launcher 改为：写 `agent-spec.json` 到 MinIO → docker run openclaw 镜像
4. `WorkerReconciler` 改为通过 `RuntimeRegistry` 调用（先只注册 openclaw Launcher）
5. 原有 shell 脚本保留，作为回退
6. **验收**：现有集成测试全部通过

### Phase 2：CoPaw Adapter 重构

1. 重构 `copaw/bridge.py`：输入改为 `agent-spec.json`（替代从 `openclaw.json` 二次转换）
2. 消除 `patch_copaw_paths` hack（working_dir 通过正式启动参数传入）
3. 注册 copaw Launcher 到 `RuntimeRegistry`
4. **验收**：CoPaw Worker 创建/删除/更新测试通过

### Phase 3：Manager 解耦

1. 升级 `worker-management` / `team-management` skill，改为生成 YAML + `hiclaw apply`
2. Manager SOUL.md 更新职责描述（配置管理员定位）
3. 保留旧 shell 脚本，但 Manager 不再直接调用
4. **验收**：Manager 可通过自然语言指令完成 Worker/Team 的创建，全程走声明式路径

### Phase 4：清理

1. 废弃 `create-worker.sh` 中的 runtime 分支逻辑（由 Adapter 替代）
2. 废弃 `copaw/bridge.py`
3. 更新文档
4. **验收**：代码库中无 openclaw/copaw 字符串散落在 Manager skill 脚本中

---

## 6. 接入新运行时的成本（设计目标验证）

假设未来接入一个新的 Agent 框架：

1. 在新框架的容器目录中，用**框架原生语言**实现 entrypoint adapter：读取 `agent-spec.json` → 框架原生配置 → 启动框架
2. 在 `hiclaw-controller/cmd/controller/main.go` 注册一行：`registry.Register(&runtime.Launcher{Name: "xxx", Image: "hiclaw/xxx-worker", ...})`
3. Worker CRD 中 `spec.runtime: xxx`
4. **Controller Go 代码零修改**，**Manager / TeamLeader / Worker 零修改**

---

## 7. 关键设计决策

### Q1：AgentSpec 是否应该完全替代 openclaw.json？

**决策**：`AgentSpec` 是 Controller→Runtime 的内部接口，`openclaw.json` 保留为 Runtime 实现细节（OpenClaw/CoPaw 都能读取的磁盘格式）。Manager 生成的声明式配置是 Worker CRD YAML，不是 openclaw.json。

### Q2：bridge.py 迁移到 Go 还是 Python？

**决策**：迁移到 Go（`internal/runtime/copaw/config.go`），与 Controller 同一进程，消除跨语言调用。CoPaw 容器运行时不再需要配置格式转换。

### Q3：Manager 生成 YAML 还是直接调用 Controller HTTP API？

**决策**：两种方式都支持。Manager 优先使用 `hiclaw apply -f` CLI（走 MinIO → file watcher → reconcile 链路），紧急情况可调用 Controller HTTP API（`:8090/api/v1/apply`）直接提交。保持声明式优先的原则。

### Q4：shell.go executor 是否完全废弃？

**决策**：不废弃。保留用于 Human 管理（Matrix 账号注册、邮件发送等非 Agent Runtime 操作）。废弃仅限于 worker/team 创建的 runtime 相关分支。

---

## 8. 与现有文档的关系

| 现有文档 | 关系 |
|---------|------|
| `docs/design/team-worker-proposal.md` | 本提案是其 Runtime 层的补充，Team/Worker CRD 格式不变 |
| `docs/architecture.md` | 需更新架构图，增加 Runtime Layer |
| `docs/declarative-resource-management.md` | Manager 工作流部分需更新，体现声明式配置生成路径 |

---

## 9. 实施状态

| Phase | 内容 | 状态 |
|-------|------|------|
| 1 | AgentRuntime 接口 + OpenClawAdapter + WorkerReconciler 接入 | 待开始 |
| 2 | CoPawAdapter + 消除 bridge.py hack | 待开始 |
| 3 | Manager skill 解耦，改为声明式 YAML 生成 | 待开始 |
| 4 | 清理旧代码、更新文档 | 待开始 |
