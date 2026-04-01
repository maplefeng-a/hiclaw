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
- **新增运行时成本高**：接入第三个框架（如 Manus、AutoGen）需要修改多处 shell 脚本
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
┌───────────────────────────────────────────────────────────────┐
│                    Controller Layer                           │
│                                                               │
│   WorkerReconciler   TeamReconciler   HumanReconciler         │
│                                                               │
│   ─ 期望状态 vs 实际状态                                       │
│   ─ 调用 RuntimeRegistry.Get(spec.runtime) 获取适配器         │
│   ─ 通过 AgentRuntime 接口执行 Create/Update/Delete           │
└───────────────────────┬───────────────────────────────────────┘
                        │ AgentRuntime 接口调用
                        ▼
┌───────────────────────────────────────────────────────────────┐
│                     Runtime Layer (新增)                      │
│                                                               │
│   AgentRuntime (interface)                                    │
│   ┌─────────────────┐  ┌─────────────────┐  ┌─────────────┐  │
│   │ OpenClawAdapter │  │  CoPawAdapter   │  │  Future...  │  │
│   └─────────────────┘  └─────────────────┘  └─────────────┘  │
│                                                               │
│   RuntimeRegistry  ─  注册 & 查找适配器                       │
│   AgentSpec        ─  运行时无关的统一配置格式                  │
└───────────────────────────────────────────────────────────────┘
```

---

## 3. 各层设计

### 3.1 Runtime Layer（新增核心抽象）

#### 3.1.1 AgentRuntime 接口

```go
// internal/runtime/interface.go

type AgentRuntime interface {
    // 创建并启动 Agent 容器/进程
    Create(ctx context.Context, spec AgentSpec) (*AgentInstance, error)

    // 更新 Agent 配置（热更新 skills/model，或重建容器）
    Update(ctx context.Context, name string, spec AgentSpec) error

    // 停止并销毁 Agent
    Delete(ctx context.Context, name string) error

    // 查询 Agent 运行状态
    GetStatus(ctx context.Context, name string) (*AgentStatus, error)

    // 运行时标识符（"openclaw" / "copaw" / ...）
    Name() string
}
```

#### 3.1.2 统一 AgentSpec（运行时无关配置）

现有 Worker CRD 的 `spec` 字段即是声明式层，AgentSpec 是 Runtime 层的内部表示，由 Controller 从 CRD 转换而来：

```go
// internal/runtime/spec.go

type AgentSpec struct {
    Name     string
    Model    string
    Image    string            // 可选，runtime 有默认值
    Identity AgentIdentity     // SOUL.md 内容 or URI
    Agents   string            // AGENTS.md 内容 or URI
    Skills   []SkillSpec
    MCP      []MCPServerSpec
    Channels []ChannelSpec     // Matrix, DingTalk, Feishu 等
    Memory   MemorySpec
    Env      map[string]string // 注入运行时环境变量
    Package  string            // 业务自定义包 URI（file/http/nacos）
}

type AgentIdentity struct {
    Source string  // "inline" | "minio" | "package"
    Content string // inline: 直接内容; minio/package: URI
}
```

#### 3.1.3 适配器实现

**OpenClawAdapter**：
- 职责：生成 `openclaw.json` → 推送到 MinIO → `docker run hiclaw/worker-agent`
- 当前 `create-worker.sh` 中 openclaw 分支的逻辑迁移至此

**CoPawAdapter**：
- 职责：生成 `openclaw.json`（CoPaw 兼容格式）→ 推送到 MinIO → `docker run hiclaw/copaw-worker`
- 消除 `bridge.py` 中的 hack（CoPaw 适配逻辑移入 Adapter，不再需要运行时 patch）
- 目标：CoPaw 容器直接读取适配后的配置，无需运行时转换

```go
// internal/runtime/openclaw/adapter.go
type OpenClawAdapter struct {
    dockerClient  ContainerClient
    minioClient   StorageClient
    matrixClient  MatrixClient
    higress       HigressClient
}

func (a *OpenClawAdapter) Create(ctx context.Context, spec AgentSpec) (*AgentInstance, error) {
    // 1. 生成 openclaw.json + SOUL.md + AGENTS.md
    // 2. 推送配置到 MinIO agents/{name}/
    // 3. 在 Higress 创建 Consumer + 分配 MCP 权限
    // 4. 注册 Matrix 账号 + 创建 Room
    // 5. docker run --name hiclaw-worker-{name} ...
    // 6. 返回 AgentInstance{MatrixUserID, RoomID, ContainerID}
}
```

#### 3.1.4 RuntimeRegistry

```go
// internal/runtime/registry.go

type RuntimeRegistry struct {
    adapters map[string]AgentRuntime
}

func (r *RuntimeRegistry) Register(runtime AgentRuntime) {
    r.adapters[runtime.Name()] = runtime
}

func (r *RuntimeRegistry) Get(name string) (AgentRuntime, error) {
    if rt, ok := r.adapters[name]; ok {
        return rt, nil
    }
    return nil, fmt.Errorf("unknown runtime: %s (registered: %v)", name, r.Names())
}
```

主程序初始化：

```go
// cmd/controller/main.go

registry := runtime.NewRegistry()
registry.Register(openclaw.NewAdapter(dockerClient, minioClient, matrixClient, higress))
registry.Register(copaw.NewAdapter(dockerClient, minioClient, matrixClient, higress))
// 未来：registry.Register(manus.NewAdapter(...))
```

---

### 3.2 Controller Layer（升级现有）

**现状**：Reconciler → `executor/shell.go` → `create-worker.sh`（内含 runtime 分支）

**升级后**：Reconciler → `RuntimeRegistry.Get(spec.runtime)` → `AgentRuntime.Create/Update/Delete`

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
- **SOUL.md / AGENTS.md**：无变化，继续通过 MinIO/package 分发
- **Skills**：无变化，通过 MinIO 分发
- **openclaw.json 格式**：作为统一的 AgentSpec 序列化格式，两种 runtime 都接受（CoPaw 适配器在写入前完成格式转换，不再运行时 patch）
- **启动流程**：由 Adapter 负责，容器内不需要 bridge.py

---

## 4. 项目结构调整

### 4.1 hiclaw-controller 目录结构（升级后）

```
hiclaw-controller/
├── go.mod
├── cmd/
│   ├── controller/main.go          # 注册 RuntimeRegistry + 启动 reconciler
│   └── hiclaw/main.go              # CLI 工具（apply/get/delete）
├── api/v1beta1/
│   ├── types.go                    # CRD 类型（不变）
│   └── register.go
├── internal/
│   ├── runtime/                    # ★ 新增 Runtime 抽象层
│   │   ├── interface.go            # AgentRuntime 接口
│   │   ├── spec.go                 # AgentSpec, AgentInstance, AgentStatus
│   │   ├── registry.go             # RuntimeRegistry
│   │   ├── openclaw/
│   │   │   ├── adapter.go          # OpenClawAdapter 实现
│   │   │   └── config.go           # openclaw.json 生成逻辑（从 create-worker.sh 迁移）
│   │   └── copaw/
│   │       ├── adapter.go          # CoPawAdapter 实现
│   │       └── config.go           # CoPaw 配置生成（替代 bridge.py）
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

### 4.2 copaw 目录（过渡期调整）

`bridge.py` 的 hack 逻辑迁移到 `internal/runtime/copaw/config.go` 后，`copaw/` 目录中的 bridge.py 可逐步废弃。CoPaw 容器只需要：
- `worker.py`：启动 CoPaw AgentRunner
- `matrix_channel.py`：Matrix 频道适配
- `sync.py`：MinIO 同步
- `config.py`：读取已经由 Adapter 生成好的 CoPaw 格式配置（不再需要运行时转换）

---

## 5. 迁移路径

### Phase 1：接口定义（不破坏现有功能）

1. 定义 `AgentRuntime` 接口 + `AgentSpec` 数据结构
2. 实现 `OpenClawAdapter`，将 `create-worker.sh` 中的 openclaw 逻辑迁移进来
3. `WorkerReconciler` 改为通过 `RuntimeRegistry` 调用（先只注册 OpenClaw）
4. 原有 shell 脚本保留，作为回退
5. **验收**：现有集成测试全部通过

### Phase 2：CoPaw 适配器

1. 实现 `CoPawAdapter`，在适配器内完成配置格式转换
2. 消除 `copaw/bridge.py` 中的 `patch_copaw_paths` hack
3. CoPaw 容器镜像去掉 bridge.py 依赖
4. 注册 `CoPawAdapter` 到 `RuntimeRegistry`
5. **验收**：CoPaw Worker 创建/删除/更新测试通过

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

假设未来接入 **Manus** 框架：

1. 实现 `internal/runtime/manus/adapter.go`（实现 `AgentRuntime` 接口）
2. 在 `cmd/controller/main.go` 注册：`registry.Register(manus.NewAdapter(...))`
3. Worker CRD 中 `spec.runtime: manus`
4. **Manager / TeamLeader / Worker 零修改**

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
