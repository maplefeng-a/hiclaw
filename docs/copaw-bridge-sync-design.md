# CoPaw Bridge/Sync 标准化设计方案

## 1. 设计思路

三层各有明确边界，禁止跨层直接操作，跨层数据流**必须**经过 sync 或 bridge 组件，worker.py 不应直接做文件复制。：

```
┌─────────────────────────────────────────────────┐
│  Layer 1: MinIO/OSS Remote Storage              │
│  hiclaw-storage/agents/{name}/  
   hiclaw-storage/shared
│  controller管理唯一持久化真相源                                 │
└───────────────────┬─────────────────────────────┘
                    │ sync: pull (mirror_all / pull_all)
                    │ sync: push (push_local)
┌───────────────────▼─────────────────────────────┐
│  Layer 2: Local Workspace                       │
│  /root/.hiclaw-worker/{name}/                   │
│  controller管理agent文件在本地的缓存区     │
└───────────────────┬─────────────────────────────┘
                    │ bridge (openclaw.json → config/providers)
                    │ sync: propagate (L2 files → L3)
                    │ sync: Inner→Outer (Agent 修改 → L2)
┌───────────────────▼─────────────────────────────┐
│  Layer 3: CoPaw Runtime Space 
|   运行数据                                       │
│  {L2}/.copaw/                                   │
│  ├── config.json, providers.json  (bridge 生成)  │
│  ├── ......                                     │
│  └── workspaces/default/          (Agent 交互面) │
│       ├── SOUL.md, AGENTS.md                    │
│       ├── sessions/, memory/                    │
│       └── agent.json             (bridge 生成)   │
└─────────────────────────────────────────────────┘
```

### 远端存储: MinIO/OSS Remote Storage
- controller：worker创建
- controller：worker更新
- controller：Team创建

### 本地缓存: Local Workspace
- worker sync pull L1->L2
- worker sync push L2->L1
- worker sync Inner→Outer L3->L2

### 运行时数据: CoPaw Runtime Space
- worker：写入
- worker：propagate L2->L3
- worker：bridge L2->L3 

## 目录清单与职责

### /root/.hiclaw-worker/{name}/ L2目录
- AGENTS.md
- SOUL.md
- HEARTBEAT.md
- openclaw.json
- credentials/matrix/password
- mcporter.json
- skills/{builtin}/ 
- skills/{on-demand}/

### /root/.hiclaw-worker/{name}/.copaw L3目录



### 

## 共性服务与工具

### 同步服务: sync

### 配置转换器: bridge

### 地址计算器: layout


**设计原则**：。

#### 存在多归属文件清单

| 共享文件 | 当前共享机制 | 
|----------|-------------|
| AGENTS.md | controller + runtime |
| SOUL.md | Controller + runtime | 
| config.json、agent.json | Controller + runtime | 
| providers.json | Controller + runtime | 
| skills/ | Controller + runtime | 

##### AGENTS.md、SOUL.md → 拆分hiclaw定义文件和用户定义文件，

利用 CoPaw 的 `system_prompt_files` 列表机制（默认 `["AGENTS.md", "SOUL.md", "PROFILE.md"]`），CoPaw 在读取时会拼接列表中的所有文件。

```
拆分前（单文件 + markers）:
  AGENTS.md = [frontmatter + builtin 区] + [Agent 自定义区]

拆分后（两个文件，各自单一 owner）:
  AGENTS-BUILTIN.md  ← Controller 独占写入（角色定义、team-context、协作规则）
  AGENTS.md          ← Agent 独占写入（运行时发现、行为调整）
```

Bridge 在 config.json 中设置：
```json
{ "agents": { "system_prompt_files": ["AGENTS-BUILTIN.md", "AGENTS.md", "SOUL.md"] } }
```

- Controller 只写 AGENTS-BUILTIN.md，永不触碰 AGENTS.md
- Agent 只写 AGENTS.md，永不触碰 AGENTS-BUILTIN.md


##### SOUL.md → write-once 语义

| 规则 | 说明 |
|------|------|
| Controller 写入 | 仅在文件不存在时写入（create-only，不覆盖） |
| Agent 写入 | 运行时完全拥有，自由修改 |
| Spec 变更 | 通过显式 `force-reseed` 标记触发（而非隐式覆盖） |

这将 SOUL.md 从"分段共享"变为"接力传递"：Controller 播种 → Agent 接管，所有权在时间线上无重叠。

##### config.json → Bridge 输出隔离

```
拆分前（Bridge + 用户写同一文件）:
  config.json = bridge 字段 ∪ 用户字段（merge）

拆分后：
  config.bridge.json  ← Bridge 独占生成（channels.matrix, agents.running.max_input_length 等）
  config.json         ← 用户独占（其他所有配置）
```

这需要 CoPaw 支持 config overlay（config.json + config.bridge.json 合并读取）。如果 CoPaw 不支持：

**降级方案**：Bridge 生成 config.bridge.json，worker.py 启动时做一次性 merge 写入 config.json，但标记 bridge 写入的 key（如 `_bridge_managed_keys` 元数据），re-bridge 时只更新这些 key。

##### providers.json → 来源标记

```python
# Bridge 写入的 provider 带来源标记
custom_providers["higress-gateway"] = {
    ...,
    "_source": "bridge"   # 标记：这是 bridge 管理的
}

# Merge 时：只覆盖 _source="bridge" 的 providers，保留用户手动添加的
existing_providers = load_existing()
for pid, pcfg in bridge_providers.items():
    existing_providers[pid] = pcfg  # bridge 来源：覆盖
# 用户添加的（无 _source 或 _source!="bridge"）：保留
```

##### skills/ → 已隔离（现状保持）

Skills 的 4 层来源实际上已经是**不同目录**，天然隔离：

| 来源 | 目录 | Owner |
|------|------|-------|
| CoPaw 内置 | site-packages/copaw/agents/skills/ | CoPaw 包 |
| Manager 推送 | MinIO → L2 skills/ | Manager |
| Agent 安装 | L2 skills/（via ~/.agents/skills symlink） | Agent |
| 运行时自定义 | L3 customized_skills/ | Agent |

最终 merge 到 active_skills/ 是**只读组装**，不是双向写入。唯一问题是 dedup 逻辑（升级时删除旧 customized 副本），但这是一次性清理，非持续冲突。

#### 单一所有权文件（无需隔离）

| Owner | 文件 | 流向 |
|-------|------|------|
| Manager/Controller | openclaw.json, config/mcporter.json, shared/\*\* | L1 → L2 (pull) |
| Worker Agent | memory/\*\*, sessions/\*\* | L3 → L2 → L1 (push) |

特殊规则：`openclaw.json` 使用 merge-on-pull 语义（Manager 权威，Worker 保留 accessToken/plugins/channels）。这是可接受的，因为 openclaw.json 的 merge 方向是单向的（pull 时 merge），不存在双方同时写入同一文件的 race condition。

#### Controller/Manager 写入 MinIO 的完整文件清单

为准确判断每个文件的所有权，以下列出 Controller 和 Manager 写入 MinIO 的所有文件，按时机分类：

##### Worker 创建时（deployer.go + package.go）

```
agents/{name}/
├── openclaw.json              ← agentconfig.GenerateOpenClawConfig()（每次创建/更新重新生成）
├── SOUL.md                    ← 优先级：inline spec > package 文件 > 默认生成
├── AGENTS.md                  ← package 基础 + wrapWithBuiltinMarkers() + InjectCoordinationContext()
├── IDENTITY.md                ← inline spec（仅 OpenClaw runtime）
├── HEARTBEAT.md               ← 从 builtin 模板复制（可选）
├── credentials/
│   └── matrix/password        ← Matrix E2EE 密码
├── config/
│   └── mcporter.json          ← agentconfig.GenerateMcporterConfig()（仅有授权 MCP 时生成）
├── skills/                    ← 三个来源合并：package skills + builtin skills + on-demand skills
│   ├── file-sync/             ← builtin
│   ├── task-progress/         ← builtin
│   ├── project-participation/ ← builtin
│   ├── mcporter/              ← builtin
│   ├── find-skills/           ← builtin
│   └── {on-demand-skill}/     ← push-worker-skills.sh（Worker spec 指定）
└── .openclaw/cron/jobs.json   ← package 中的 crons/jobs.json（如有）
```

##### Worker 更新时（deployer.go, IsUpdate=true）

| 操作 | 文件 | 说明 |
|------|------|------|
| **重新生成** | openclaw.json | 完全重新生成 |
| **重新生成** | SOUL.md | 按优先级重新写入 |
| **Marker merge** | AGENTS.md | 替换 builtin 区段，保留 Agent 自定义内容 |
| **重新生成** | mcporter.json | 重新生成 MCP 配置 |
| **刷新** | credentials/matrix/password | 重新写入 |
| **刷新** | skills/\*\* | 重新同步 builtin + on-demand skills |
| **保留** | .openclaw/memory/\*\* | 不触碰（excludeMemory=true） |
| **保留** | MEMORY.md | 不触碰 |

##### Manager 升级时（upgrade-builtins.sh）

```
对每个已注册 Worker：
  agents/{name}/AGENTS.md       ← marker merge（保留用户内容）
  agents/{name}/HEARTBEAT.md    ← 覆盖
  agents/{name}/skills/{name}/  ← 同步 builtin + assigned worker-skills

全局发布：
  shared/builtins/worker/AGENTS.md
  shared/builtins/worker/skills/{skillName}/
```

##### 集群初始化时（initializer.go）

```
shared/knowledge/.gitkeep
shared/tasks/.gitkeep
workers/.gitkeep
hiclaw-config/workers/.gitkeep
hiclaw-config/teams/.gitkeep
hiclaw-config/humans/.gitkeep
agents/.gitkeep
```

##### Team 创建时（deployer.go:EnsureTeamStorage）

```
teams/{team}/shared/tasks/.keep
teams/{team}/shared/projects/.keep
teams/{team}/shared/knowledge/.keep
```

##### 运行时（Manager Agent skills）

| 脚本 | 目标 | 说明 |
|------|------|------|
| push-worker-skills.sh | `agents/{name}/skills/{skill}/` | Manager 按需推送 skill |
| setup-mcp-server.sh | `agents/{name}/config/mcporter.json` | 更新 MCP 配置（通过 Controller API） |

### P4: 声明式配置、幂等操作

- Bridge 是纯函数：相同 openclaw.json + layout 输入 → 相同 config.json/providers.json 输出
- Sync 每次操作幂等：重复执行不改变最终状态
- 所有路径通过 `WorkspaceLayout` 计算得出，零硬编码（P4）

### P5: Agent Workspace 是唯一交互面

Agent 只在 `workspaces/default/` 中读写。`COPAW_WORKING_DIR` 根级是基础设施层（config.json, providers.json, active_skills 等），Agent 不直接操作。

Inner→Outer sync 必须从 `workspaces/default/AGENTS.md` 开始，而非 `.copaw/AGENTS.md`。


### P2: Local Workspace 目录标准

每个 Worker 在本地有一个以 worker name 命名的目录，内部按层级和用途组织：

```
{INSTALL_DIR}/{worker_name}/              ← L2 根（local_dir）
├── openclaw.json                         ← Manager/Controller 管理
├── SOUL.md                               ← Controller 种子 → Agent 运行时独占
├── AGENTS.md                             ← Agent 独占
├── AGENTS-BUILTIN.md                     ← Controller 独占
├── config/
│   └── mcporter.json                     ← Manager/Controller 管理
├── skills/                               ← Manager 推送 + Agent 安装
│   ├── github-operations/
│   └── ...
├── shared/                               ← MinIO shared 的本地镜像（见下方说明）
│   ├── tasks/                            ← 任务工作区（Manager 分配，Worker 读写结果）
│   ├── projects/                         ← 项目资料
│   ├── knowledge/                        ← 共享知识库
│   └── builtins/worker/                  ← Worker 模板（AGENTS.md, skills/）
├── global-shared/                        ← 仅 Team Leader：全局 shared 只读副本
│
├── .copaw/                               ← L3 根（COPAW_WORKING_DIR）
│   ├── config.json                       ← Bridge 生成（或 config.bridge.json 隔离后）
│   ├── providers.json                    ← Bridge 生成
│   ├── active_skills/                    ← propagate 合并（只读组装）
│   │   ├── pdf/                          ← CoPaw 内置
│   │   ├── github-operations/            ← MinIO overlay
│   │   └── ...
│   ├── customized_skills/                ← Agent 运行时自定义（本地，不经 sync）
│   ├── config/
│   │   └── mcporter.json                 ← propagate 复制
│   ├── memory/                           ← Agent 独占
│   ├── models/                           ← CoPaw 内部
│   ├── custom_channels/                  ← CoPaw 内部
│   │
│   └── workspaces/default/               ← Agent 唯一交互面（P5）
│       ├── SOUL.md                       ← propagate 复制 → Agent 运行时修改
│       ├── AGENTS.md                     ← propagate 复制 → Agent 运行时修改
│       ├── AGENTS-BUILTIN.md             ← propagate 复制（Agent 不修改）
│       ├── agent.json                    ← CoPaw Agent 配置
│       ├── sessions/                     ← Agent 独占
│       └── memory/                       ← Agent 独占
│
└── .copaw.secret/                        ← L3 密钥目录
    └── providers.json                    ← Bridge 生成（密钥隔离）
```

**约定**：
- `INSTALL_DIR` 默认 `/root/.hiclaw-worker`，由 entrypoint 或 CLI 参数指定
- L2 根 = `INSTALL_DIR/{worker_name}`
- L3 根 = L2 根 / `.copaw`（即 `COPAW_WORKING_DIR`）
- 所有路径通过 `WorkspaceLayout` 类计算，零硬编码（P4）
- `/root/hiclaw-fs` symlink → L2 根（容器内便捷访问）

#### shared/ 目录详解

shared/ 是**跨 Worker 的协作文件空间**，MinIO 中有两级：

```
MinIO:
  hiclaw-storage/shared/                    ← 全局 shared（非 Team Worker 使用）
  │ ├── tasks/                              ← 任务工作区
  │ ├── projects/                           ← 项目资料
  │ ├── knowledge/                          ← 共享知识库
  │ └── builtins/worker/                    ← Worker 模板（AGENTS.md, skills/）
  │
  hiclaw-storage/teams/{team-id}/shared/    ← Team shared（Team 成员使用）
    ├── tasks/
    ├── projects/
    └── knowledge/
```

**路由逻辑**：Worker 根据自身 Team 归属决定 pull/push 哪个 shared：

| Worker 类型 | pull shared 来源 | push shared 目标 | global-shared |
|-------------|-----------------|-----------------|---------------|
| 非 Team Worker | `shared/` | `shared/` | 无 |
| Team Worker | `teams/{team}/shared/` | `teams/{team}/shared/` | 无 |
| Team Leader | `teams/{team}/shared/` | `teams/{team}/shared/` | `shared/` → `global-shared/`（只读） |

Team 检测方式：
1. 从 AGENTS.md 解析 `**Team**: {team-id}` 标记
2. Fallback：从 openclaw.json 读取 `team_id` 字段
3. Team Leader 检测：AGENTS.md 中包含 "Upstream coordinator" 文本

**shared/ 的特殊所有权**：

shared/ 不同于其他 Manager-managed 文件——它是**双向的**：
- **下行（Manager → Worker）**：Manager 通过 MinIO 分配任务 spec、项目资料、知识库
- **上行（Worker → shared）**：Worker 完成任务后通过 `push-shared.sh` 推送结果到 `tasks/{task-id}/`

但在 Worker 的自动 push_local() 循环中，shared/ 被排除（`_EXCLUDE_DIRS`），不会自动上推。Worker 必须通过显式的 `push-shared.sh` 脚本手动推送，避免半成品结果覆盖共享数据。

**builtins/ 分发**：

`upgrade-builtins.sh` 将 Manager 管理的 Worker 模板发布到 `shared/builtins/worker/`：
- AGENTS.md 模板 → `shared/builtins/worker/AGENTS.md`
- Worker 技能 → `shared/builtins/worker/skills/{skill-name}/`

然后逐个 Worker 从 builtins 同步到各自的 `agents/{worker-name}/` 工作空间。

**OSS 访问控制**：

Controller 为每个 Worker 生成 STS 策略，允许访问：
- `agents/{workerName}/*`（自身工作空间）
- `shared/*`（全局 shared）

Team Worker 额外获得 `teams/{team}/*` 访问权限。


## 3. 组件设计

### 3.1 WorkspaceLayout — 路径中心化

**文件**: `copaw/src/copaw_worker/layout.py`

单一来源的路径计算，消除所有模块中的硬编码路径。

```python
class WorkspaceLayout:
    def __init__(self, install_dir: Path, worker_name: str):
        ...

    # Layer 2 paths
    local_dir          # install_dir / worker_name
    l2_openclaw_json   # local_dir / "openclaw.json"
    l2_mcporter_json   # local_dir / "config" / "mcporter.json"
    l2_skills_dir      # local_dir / "skills"
    l2_shared_dir      # local_dir / "shared"

    # Layer 3 infrastructure paths
    working_dir        # local_dir / ".copaw"  (= COPAW_WORKING_DIR)
    secret_dir         # working_dir + ".secret"
    config_json        # working_dir / "config.json"
    providers_json     # working_dir / "providers.json"
    active_skills_dir  # working_dir / "active_skills"
    mcporter_config    # working_dir / "config" / "mcporter.json"

    # Layer 3 agent workspace paths
    agent_workspace    # working_dir / "workspaces" / "default"
    agent_soul_md      # agent_workspace / "SOUL.md"
    agent_agents_md    # agent_workspace / "AGENTS.md"

    def ensure_dirs(self) -> None:   # 幂等创建所有必要目录
```

### 3.2 Bridge — 纯配置转换

**文件**: `copaw/src/copaw_worker/bridge.py`

职责分离：bridge 只做文件生成，runtime patching 独立函数。

```
bridge_openclaw_to_copaw(openclaw_cfg, layout)    # 纯转换
  ├── _write_config_json(cfg, working_dir, in_container)
  └── _write_providers_json(cfg, working_dir, in_container)
      └── copy to secret_dir

patch_copaw_runtime(layout)                        # 独立调用
  ├── os.environ["COPAW_WORKING_DIR"] = ...
  ├── monkey-patch copaw.constant.*
  ├── monkey-patch copaw.providers.store.*
  └── monkey-patch copaw.envs.store.*
```

**关键变化**：
- `bridge_openclaw_to_copaw()` 接受 `WorkspaceLayout` 而非 `Path`
- 无环境变量副作用 — caller 自行调用 `patch_copaw_runtime()`
- Bridge 保持纯函数特性（P4）

#### Bridge 输出的 Merge 语义

> **注意**：以下描述当前实现。P2 隔离方案实施后，config.json 和 providers.json 的 merge 问题将被消除。

**config.json — 当前行为（部分 merge，待隔离）**：

```python
existing = json.load(config_path)           # 保留已有配置
existing["channels"]["matrix"] = bridge_cfg # 整体覆盖 matrix 配置
existing["agents"]["running"]["max_input_length"] = ...  # 追加字段
existing["agents"]["running"]["embedding_config"] = ...  # 追加字段
```

| 字段 | Bridge 行为 | 用户配置影响 |
|------|-------------|--------------|
| `channels.matrix` | **整体覆盖** | 用户在 CoPaw UI 修改的 matrix 配置会被覆盖 |
| `channels.console` | 仅设 enabled=false | 保留其他字段 |
| `channels.*`（其他 channel） | **不触碰** | 用户自行添加的 channel（如 discord）安全 |
| `agents.running.*` | 仅追加 bridge 字段 | 用户的 `agents.running` 其他设置保留 |
| 其他顶层 key | **不触碰** | 安全 |

**providers.json — 当前行为（全量覆盖）**：

```python
# ⚠️ 全量覆盖，不 merge
providers_data = {"providers": {}, "custom_providers": {...}, "active_llm": {...}}
json.dump(providers_data, f)
```

| 字段 | Bridge 行为 | 用户配置影响 |
|------|-------------|--------------|
| `custom_providers` | **全量覆盖** | 用户在 CoPaw UI 添加的 provider 会丢失 |
| `active_llm` | **全量覆盖** | 用户切换的模型会被 reset |
| `providers`（内置） | 清空为 {} | 不影响（CoPaw 使用 custom_providers） |

**设计原则**：Bridge 只写它「知道的」字段，保留它「不知道的」字段。

- config.json 已基本遵循（读 existing → 追加 bridge 字段 → 写回）
- providers.json 需要改为 merge 模式：bridge 只更新 openclaw 来源的 providers，保留用户在 CoPaw UI 手动添加的 providers

### 3.3 Sync — 统一文件同步

**文件**: `copaw/src/copaw_worker/sync.py`

#### FileSync 类核心方法

| 方法 | 方向 | 说明 |
|------|------|------|
| `mirror_all()` | L1 → L2 | 启动时全量镜像 |
| `pull_all()` | L1 → L2 | 运行时增量拉取（allowlist） |
| `propagate_to_runtime(layout)` | L2 → L3 | **新增**：统一的层间传播 |
| `push_local(sync, since)` | L2 → L1 | Worker-managed 内容推送 |

#### propagate_to_runtime() — 替代分散的文件复制

```python
def propagate_to_runtime(self, layout: WorkspaceLayout) -> None:
    """L2 → L3: 将 local_dir 内容传播到 runtime 空间。

    - SOUL.md, AGENTS.md → workspaces/default/（P5）
    - config/mcporter.json → .copaw/config/
    - skills → active_skills/（builtin seed + MinIO overlay）
    """
```

#### Inner→Outer Sync 修正

```python
# 修正前（错误）：从 .copaw/ 根目录读取
inner = copaw_dir / name   # .copaw/AGENTS.md — Agent 不在此读写

# 修正后：从 workspaces/default/ 读取（P5）
agent_ws = copaw_dir / "workspaces" / "default"
inner = agent_ws / name    # .copaw/workspaces/default/AGENTS.md
```

### 3.4 Worker — 简化的启动流

**文件**: `copaw/src/copaw_worker/worker.py`

重构后的 `start()` 流程清晰体现三层模型：

```python
async def start(self) -> bool:
    # 0. 构建 layout — 路径单一来源 (P4)
    self.layout = WorkspaceLayout(self.config.install_dir, self.worker_name)

    # 1. 初始化 FileSync
    self.sync = FileSync(..., local_dir=self.layout.local_dir)

    # 2. L1 → L2: 全量镜像
    self.sync.mirror_all()

    # 3. 解析配置 + Matrix 重登录
    openclaw_cfg = self.sync.get_config()
    openclaw_cfg = self._matrix_relogin(openclaw_cfg)

    # 4. Bridge: L2 → L3 配置转换 (P2 纯转换)
    bridge_openclaw_to_copaw(openclaw_cfg, self.layout)
    patch_copaw_runtime(self.layout)

    # 5. Propagate: L2 → L3 文件传播 (P1 跨层经 sync)
    self.sync.propagate_to_runtime(self.layout)

    # 6. 后台同步循环
    asyncio.create_task(sync_loop(...))
    asyncio.create_task(push_loop(...))
```

**删除的方法**（职责收归 sync.py）：
- `_sync_skills()` → `FileSync._propagate_skills()`
- `_dedup_customized_skills()` → `FileSync._dedup_customized_skills()`
- `_copy_mcporter_config()` → `FileSync.propagate_to_runtime()`
- 内联 SOUL.md/AGENTS.md 复制 → `FileSync.propagate_to_runtime()`

## 4. Sync Protocol

OpenClaw（bash）和 CoPaw（Python）共享同步协议，语义一致、实现独立。

### 4.1 L1↔L2 文件所有权

> 隔离后的目标状态（参见 P3）。每个文件仅有一个 writer。

| 所有者 | 文件 | 拉取策略 | 说明 |
|--------|------|----------|------|
| Manager/Controller | openclaw.json | merge-on-pull | Manager 权威，Worker 保留 token/plugins |
| Manager/Controller | config/mcporter.json | overwrite-on-pull | |
| Manager + Agent | skills/\*\* | mirror-on-pull | Manager 推送 + Agent 安装，独立目录 |
| Manager → Worker | shared/\*\* | mirror-on-pull | Team 感知路由（见 P2）；自动 push 排除，显式 push-shared.sh 上推 |
| Controller | AGENTS-BUILTIN.md | overwrite（Controller 独占） | 升级时直接覆盖，无 marker |
| Agent | AGENTS.md | push（Agent 独占） | Agent 运行时内容，Controller 不触碰 |
| Controller（种子） | SOUL.md | create-only | 仅文件不存在时写入，Agent 运行时独占 |
| Agent | memory/\*\*, sessions/\*\* | push | |

### 4.2 L2↔L3 传播协议

L2（Local Workspace）和 L3（CoPaw Runtime）之间**不是简单的文件复制**，而是有明确的方向和转换语义。

#### L2 → L3（外→内）：propagate + bridge

启动时和每次 pull_all 后触发。

| L2 源 | L3 目标 | 方式 | 说明 |
|--------|---------|------|------|
| openclaw.json | config.bridge.json | **bridge 转换** | 非复制，是格式转换（P3 隔离后） |
| openclaw.json | providers.json | **bridge 转换** | 格式转换 + 来源标记 |
| SOUL.md | workspaces/default/SOUL.md | **复制** | Agent 交互面（P5） |
| AGENTS.md | workspaces/default/AGENTS.md | **复制** | Agent 独占内容 |
| AGENTS-BUILTIN.md | workspaces/default/AGENTS-BUILTIN.md | **复制** | Controller 独占内容 |
| skills/\*\* | active\_skills/\*\* | **overlay 合并** | CoPaw 内置 → MinIO skills → 合并 |
| config/mcporter.json | .copaw/config/mcporter.json | **复制** | |

注意：Bridge 输出（config.bridge.json, providers.json）是 L2→L3 的**衍生产物**，不回写 L2。

#### L3 → L2（内→外）：Inner→Outer sync

Agent 在 `workspaces/default/` 中的修改回写到 L2，以便 push 到 MinIO（L1）。

| L3 源（workspaces/default/） | L2 目标 | 条件 | 说明 |
|-------------------------------|---------|------|------|
| SOUL.md | SOUL.md | content 变化 | Agent 运行时修改 |
| AGENTS.md | AGENTS.md | content 变化 | Agent 运行时修改 |
| memory/\*\* | — | 直接在 L2 | CoPaw memory 已在 L2（via symlink 或直接路径） |
| sessions/\*\* | — | 直接在 L2 | 同上 |

**关键**：Inner→Outer 只同步 **Agent 拥有的文件**。Bridge 输出（config.json, providers.json）、active_skills/、mcporter.json 等**永不回写**——它们是 L2→L3 单向衍生。

#### L2↔L3 所有权总览

```
L2 (Local Workspace)                    L3 (CoPaw Runtime)
─────────────────────                   ─────────────────────
openclaw.json ──── bridge ────────────→ config.bridge.json    (Bridge 独占)
                                        providers.json        (Bridge 独占)
SOUL.md ──────── copy ────────────────→ ws/default/SOUL.md    (Agent 运行时独占)
        ◄──────── Inner→Outer ────────  ws/default/SOUL.md
AGENTS.md ────── copy ────────────────→ ws/default/AGENTS.md  (Agent 独占)
          ◄────── Inner→Outer ────────  ws/default/AGENTS.md
AGENTS-BUILTIN.md── copy ────────────→ ws/default/AGENTS-BUILTIN.md (Controller 独占)
                                        (不回写 — Controller 管理)
skills/** ────── overlay 合并 ────────→ active_skills/**      (只读组装)
                                        (不回写 — 衍生产物)
config/mcporter.json── copy ──────────→ .copaw/config/mcporter.json
                                        (不回写 — Manager 管理)
```

**设计原则**：L2↔L3 之间回写（Inner→Outer）仅限 Agent 拥有的文件。所有衍生、转换、管理方文件都是**单向 L2→L3**。

### openclaw.json Merge 规则

| 字段 | 规则 |
|------|------|
| 顶层字段 | Remote wins |
| plugins.entries | Deep merge（remote wins shared keys） |
| plugins.load.paths | Union（sorted, deduplicated） |
| channels | Deep merge（remote wins shared types） |
| channels.matrix.accessToken | **Local wins**（Worker 重登录） |

### Push 排除规则

- Manager-managed: openclaw.json, config/mcporter.json
- Transient dirs: .agents, .cache, .npm, .local, .mc, \_\_pycache\_\_
- Derived dirs: custom\_channels, active\_skills, shared
- Bridge-derived (.copaw/): config.json, providers.json, root SOUL.md/AGENTS.md, mcporter.json
- Extensions: \*.lock

## 5. Entrypoint 一致性

`copaw-worker-entrypoint.sh` 中的路径与 `WorkspaceLayout` 保持一致：

| Entrypoint 变量 | 值 | Layout 属性 |
|-----------------|-----|-------------|
| `INSTALL_DIR` | `/root/.hiclaw-worker` | `install_dir` (CLI 参数) |
| `COPAW_WORKING_DIR` | `${INSTALL_DIR}/${WORKER_NAME}/.copaw` | `layout.working_dir` |
| `CONFIG_FILE` | `${INSTALL_DIR}/${WORKER_NAME}/.copaw/config.json` | `layout.config_json` |
| `WORKER_SKILLS_DIR` | `${INSTALL_DIR}/${WORKER_NAME}/skills` | `layout.l2_skills_dir` |
| `/root/hiclaw-fs` symlink | → `${INSTALL_DIR}/${WORKER_NAME}` | → `layout.local_dir` |

## 6. 数据流总览

```
                          L1 (MinIO)
                              │
            ┌─────────────────┼─────────────────┐
            │ mirror_all /    │                  │ push_local
            │ pull_all        │                  │ (mtime+content)
            ▼                 │                  │
    ┌───────────────┐         │         ┌────────┴──────┐
    │ L2 Local WS   │         │         │ L2 Local WS   │
    │ openclaw.json │         │         │ SOUL.md       │
    │ skills/       │         │         │ AGENTS.md     │
    │ mcporter.json │         │         │ sessions/     │
    └───────┬───────┘         │         └───────▲───────┘
            │                 │                  │
    bridge  │ propagate       │                  │ Inner→Outer
    (config)│ (files)         │                  │ (ws/default/→L2)
            ▼                 │                  │
    ┌───────────────────────────────────────────────────┐
    │                  L3 Runtime Space                  │
    │  config.json   providers.json   active_skills/    │
    │  config/mcporter.json                             │
    │                                                   │
    │  ┌─────────────────────────────────────────────┐  │
    │  │  workspaces/default/ (Agent 唯一交互面 P5)   │  │
    │  │  SOUL.md  AGENTS.md  sessions/  memory/     │  │
    │  └─────────────────────────────────────────────┘  │
    └───────────────────────────────────────────────────┘
```

## 7. 实现状态

分支: `refactor/copaw-bridge-sync-standardize`

| 变更 | 文件 | 状态 |
|------|------|------|
| 新建 WorkspaceLayout | `copaw/src/copaw_worker/layout.py` | ✅ 完成 |
| 重写 Bridge（纯函数 + patch 分离） | `copaw/src/copaw_worker/bridge.py` | ✅ 完成 |
| Sync: propagate_to_runtime + Inner→Outer 修正 | `copaw/src/copaw_worker/sync.py` | ✅ 完成 |
| Worker 简化（删除分散方法，统一流程） | `copaw/src/copaw_worker/worker.py` | ✅ 完成 |
| Sync Protocol 文档 | `docs/design/sync-protocol.md` | ✅ 完成（gitignored） |
| Entrypoint 路径验证 | `copaw/scripts/copaw-worker-entrypoint.sh` | ✅ 验证通过，无需修改 |

### 待办

- [ ] 端到端测试：部署到容器环境验证完整启动流程
- [ ] 验证 re-bridge callback 正确性（openclaw.json 变更触发重桥接+重传播）
- [ ] 确认 `_matrix_relogin` 中 `self.sync.local_dir` 引用与 layout 一致
