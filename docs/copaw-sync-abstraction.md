# CoPaw sync.py 功能抽象

**文件**: `copaw/src/copaw_worker/sync.py`

---

## 核心职责

**MinIO 文件同步引擎**：负责 Worker 与 MinIO 之间的双向文件同步，确保配置、技能、会话等数据在本地和远程保持一致。

---

## 设计原则

### 文件所有权模型

```
Manager-managed (Worker 只读，只 pull):
  - openclaw.json
  - mcporter-servers.json
  - skills/
  - shared/

Worker-managed (Worker 读写，push 到 MinIO):
  - AGENTS.md
  - SOUL.md
  - .copaw/sessions/
  - memory/
```

### 同步策略

- **Pull 方向** (Remote → Local): 定期拉取 Manager-managed 文件
- **Push 方向** (Local → Remote): 变更触发推送 Worker-managed 文件
- **通知机制**: 通过 Matrix @mention 触发按需同步

---

## 主要功能模块

### 1. MinIO 连接管理

#### `FileSync.__init__()`
- 初始化 MinIO 连接参数
- 设置本地工作目录
- 支持本地模式和云模式（阿里云 STS）

#### `_ensure_alias()`
- 设置 mc CLI 别名
- 云模式：动态刷新 STS 临时凭证
- 本地模式：使用静态 AccessKey/SecretKey

#### `_refresh_cloud_credentials()`
- 调用 shell 脚本刷新阿里云 STS 凭证
- 懒加载：仅在凭证即将过期时刷新

---

### 2. 基础 MinIO 操作

#### `_cat(key: str) -> Optional[str]`
- 下载单个对象内容（文本）
- 使用 `mc cat` 命令

#### `_ls(prefix: str) -> list[str]`
- 列出指定前缀下的所有对象
- 使用 `mc ls --recursive`

#### `_object_path(key: str) -> str`
- 构建完整的 mc 路径：`alias/bucket/key`

---

### 3. 配置文件操作

#### `get_config() -> dict`
- 从 MinIO 拉取 `openclaw.json`
- 解析为 Python 字典

#### `get_soul() -> Optional[str]`
- 获取 Worker 的 SOUL.md 内容

#### `get_agents_md() -> Optional[str]`
- 获取 Worker 的 AGENTS.md 内容

---

### 4. 技能管理

#### `list_skills() -> list[str]`
- 列出 MinIO 中可用的所有技能名称
- 扫描 `agents/{worker}/skills/` 目录

#### `get_skill_md(skill_name: str) -> Optional[str]`
- 获取指定技能的 SKILL.md 内容

---

### 5. 完整同步（启动时）

#### `mirror_all() -> None`
**用途**: Worker 启动时的全量同步

**操作**:
1. 镜像 Worker 的完整 MinIO 前缀到本地
   - `agents/{worker}/` → `{local_dir}/`
   - 排除 `credentials/**`
2. 镜像 shared 目录
   - Team Worker: `teams/{team}/shared/` → `{local_dir}/shared/`
   - 普通 Worker: `shared/` → `{local_dir}/shared/`
3. Team Leader 额外镜像全局 shared
   - `shared/` → `{local_dir}/global-shared/`

**特点**:
- 使用 `mc mirror --overwrite`
- 恢复所有状态：配置、会话、同步令牌等

---

### 6. 增量同步（运行时）

#### `pull_all() -> list[str]`
**用途**: 定期拉取 Manager-managed 文件

**拉取内容**:
1. **配置文件**
   - `openclaw.json` (智能合并)
   - `config/mcporter.json`

2. **技能目录**
   - `skills/{skill_name}/` (完整镜像)
   - 自动删除本地已移除的技能

3. **共享目录**
   - `shared/` 或 `teams/{team}/shared/`
   - Team Leader 额外同步 `global-shared/`

**智能合并 openclaw.json**:
- 使用 `_merge_openclaw_config()` 合并远程和本地配置
- **Remote wins**: 大部分配置以远程为准
- **Local wins**: `channels.matrix.accessToken` 保留本地值
- **Union**: `plugins.load.paths` 合并两边的路径

**返回值**: 变更的文件列表（用于触发 re-bridge）

---

#### `push_local(sync: FileSync, since: float) -> list[str]`
**用途**: 推送本地变更到 MinIO

**推送内容**:
- Worker-managed 文件（排除 Manager-managed）
- 仅推送 mtime > since 的文件
- 内容比对：仅推送真正变更的文件

**排除规则**:
- 文件名: `openclaw.json`, `mcporter-servers.json`
- 路径: `config/mcporter.json`
- 目录: `.agents`, `.cache`, `.npm`, `shared`, `__pycache__`
- 扩展名: `.lock`
- CoPaw 派生文件: `config.json`, `providers.json`, `SOUL.md`, `AGENTS.md`

**Inner → Outer 同步**:
- CoPaw 修改 `.copaw/AGENTS.md` 或 `.copaw/SOUL.md`
- 自动同步到外层 `AGENTS.md` / `SOUL.md`
- 然后推送到 MinIO

**返回值**: 推送的文件列表

---

### 7. 双向配置同步（新增功能）

#### `sync_agent_to_openclaw(sync: FileSync) -> bool`
**用途**: 反向同步 agent.json → openclaw.json

**同步内容**:
1. **Matrix allowlist**
   - `agent.json` 的 `allow_from` → `openclaw.json` 的 `allowFrom`
   - `agent.json` 的 `group_allow_from` → `openclaw.json` 的 `groupAllowFrom`

2. **模型配置**
   - `agent.json` 的 `llm_routing.local.model` → `openclaw.json` 的 `models.default`

**触发时机**: 在 `push_local()` 之前自动调用

**目的**: 确保运行时配置修改（如 `/model` 命令切换模型）持久化

---

### 8. 后台同步循环

#### `async sync_loop(sync, interval, on_pull)`
**用途**: 定期拉取 Manager-managed 文件

**流程**:
```python
while True:
    await asyncio.sleep(interval)  # 默认 300 秒
    changed = sync.pull_all()
    if changed:
        await on_pull(changed)  # 触发 re-bridge
```

**回调**: `on_pull(changed: list[str])`
- 检测到 `openclaw.json` 变化时触发 re-bridge
- 更新技能时重新同步到 CoPaw active_skills

---

#### `async push_loop(sync, check_interval)`
**用途**: 定期推送本地变更

**流程**:
```python
last_push_time = time.time()
while True:
    await asyncio.sleep(check_interval)  # 默认 5 秒

    # 1. 反向同步 agent.json → openclaw.json
    sync_agent_to_openclaw(sync)

    # 2. 推送变更的文件
    pushed = push_local(sync, last_push_time)
    last_push_time = time.time()
```

**特点**:
- 变更触发：仅推送 mtime > last_push_time 的文件
- 内容比对：避免推送未变更的文件

---

## 辅助功能

### Team 相关

#### `_get_team_id() -> Optional[str]`
- 从 AGENTS.md 或 openclaw.json 读取 team_id

#### `_is_team_leader() -> bool`
- 检查是否为 Team Leader
- 判断依据：AGENTS.md 中是否包含 "Upstream coordinator"

#### `_get_shared_remote() -> str`
- 返回 shared 目录的 MinIO 路径
- Team Worker: `teams/{team}/shared/`
- 普通 Worker: `shared/`

---

### 配置合并

#### `_deep_merge(base: dict, override: dict) -> dict`
- 深度合并两个字典
- override 优先（覆盖 base 的叶子节点）

#### `_merge_openclaw_config(remote_text: str, local_text: str) -> str`
- 智能合并远程和本地 openclaw.json
- 规则：
  - **plugins**: 深度合并，union `load.paths`
  - **channels**: 深度合并，remote wins，但 `accessToken` local wins
  - **其他**: remote as-is

---

## 功能分层

```
┌─────────────────────────────────────────────────────────┐
│ 后台循环层                                                │
│  - sync_loop (定期 pull)                                 │
│  - push_loop (定期 push)                                 │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 同步策略层                                                │
│  - pull_all (增量拉取 Manager-managed)                   │
│  - push_local (增量推送 Worker-managed)                  │
│  - sync_agent_to_openclaw (反向同步配置)                 │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ 文件操作层                                                │
│  - mirror_all (全量镜像)                                 │
│  - get_config, get_soul, get_agents_md (读取配置)        │
│  - list_skills, get_skill_md (技能管理)                  │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ MinIO 基础层                                             │
│  - _cat (下载对象)                                       │
│  - _ls (列出对象)                                        │
│  - _mc (执行 mc 命令)                                    │
│  - _ensure_alias (连接管理)                              │
└─────────────────────────────────────────────────────────┘
```

---

## 核心数据流

### 启动时（mirror_all）
```
MinIO (agents/{worker}/)
    ↓ mc mirror
Local (完整状态恢复)
    ↓ bridge
CoPaw (.copaw/workspaces/default/)
```

### 运行时 Pull（sync_loop）
```
MinIO (openclaw.json, skills/, shared/)
    ↓ pull_all (智能合并)
Local (openclaw.json)
    ↓ on_pull callback
bridge_openclaw_to_copaw()
    ↓
CoPaw (agent.json, config.json)
    ↓ hot-reload (~2s)
CoPaw Agent (配置生效)
```

### 运行时 Push（push_loop）
```
CoPaw Agent (修改配置/会话)
    ↓
CoPaw (agent.json, sessions/)
    ↓ sync_agent_to_openclaw
Local (openclaw.json 更新)
    ↓ push_local (变更检测)
MinIO (持久化)
```

---

## 关键设计特点

### 1. 智能合并策略
- **避免配置冲突**: openclaw.json 合并时保留本地关键字段
- **双向同步**: agent.json ↔ openclaw.json 互相同步

### 2. 变更检测优化
- **mtime 检查**: 仅处理修改过的文件
- **内容比对**: 避免推送未变更的文件
- **增量同步**: 运行时只同步必要的文件

### 3. 文件所有权清晰
- **Manager-managed**: 只读，定期 pull
- **Worker-managed**: 读写，变更触发 push
- **排除规则**: 明确哪些文件不同步

### 4. Team 支持
- **Team Worker**: 使用 team 专属 shared 目录
- **Team Leader**: 额外访问全局 shared（用于 Manager 任务）

### 5. 云原生支持
- **阿里云 STS**: 动态刷新临时凭证
- **本地模式**: 静态 AccessKey/SecretKey

---

## 使用场景

### 场景 1: Worker 启动
```python
sync = FileSync(...)
sync.mirror_all()  # 全量恢复状态
openclaw_cfg = sync.get_config()
bridge_openclaw_to_copaw(openclaw_cfg, working_dir)
```

### 场景 2: 配置热更新
```python
# Manager 更新 MinIO 中的 openclaw.json
# ↓
# sync_loop 检测到变化
changed = sync.pull_all()  # ['openclaw.json']
# ↓
# 触发 re-bridge
await on_pull(changed)
# ↓
# CoPaw 自动 hot-reload
```

### 场景 3: 会话持久化
```python
# CoPaw Agent 保存会话
# ↓
# push_loop 检测到 .copaw/sessions/ 变化
pushed = push_local(sync, last_push_time)
# ↓
# 会话推送到 MinIO
```

### 场景 4: 模型切换持久化
```python
# 用户执行 /model qwen3.5-plus
# ↓
# CoPaw 更新 agent.json 的 llm_routing.local.model
# ↓
# push_loop 触发
sync_agent_to_openclaw(sync)  # agent.json → openclaw.json
push_local(sync, last_push_time)  # openclaw.json → MinIO
# ↓
# 重启后配置保留
```

---

## 总结

`sync.py` 是一个**完整的 MinIO 文件同步引擎**，提供：

1. ✅ **双向同步**: Pull (Manager-managed) + Push (Worker-managed)
2. ✅ **智能合并**: 避免配置冲突，保留关键本地字段
3. ✅ **变更检测**: mtime + 内容比对，高效增量同步
4. ✅ **后台循环**: 定期自动同步，无需手动触发
5. ✅ **配置持久化**: 运行时修改自动保存到 MinIO
6. ✅ **Team 支持**: 多租户隔离，Team Leader 特殊权限
7. ✅ **云原生**: 支持阿里云 STS 动态凭证

**核心价值**: 让 Worker 的配置、会话、技能等数据在本地和 MinIO 之间保持一致，支持配置热更新和状态持久化。
