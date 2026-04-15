# CoPaw FileSync 配置同步问题调查报告

**日期**: 2026-04-14
**问题**: Controller 更新 MinIO 中的 openclaw.json 后，Manager 和 Worker 容器内的配置没有自动同步更新

---

## 问题现象

### 背景
当 Controller 通过 `UpdateManagerGroupAllowFrom` 更新 Manager 的 Matrix `groupAllowFrom` 配置（添加新 Worker 到白名单）时，发现：

1. MinIO 中的 `openclaw.json` 已正确更新（包含新 Worker 的 Matrix ID）
2. 但容器内的 `agent.json` 中 `group_allow_from` 字段仍为 `null`
3. Manager 无法接收新 Worker 房间的消息

### 预期行为
根据 `copaw-worker` 的设计，应该有双向配置同步机制：
- **Pull 方向**: MinIO → openclaw.json → agent.json (通过 `sync_loop`)
- **Push 方向**: agent.json → openclaw.json → MinIO (通过 `push_loop`)

---

## 调查过程

### 1. 验证配置文件状态

#### Manager 配置检查
```bash
# MinIO 中的配置（正确）
$ cat /root/manager-workspace/openclaw.json | jq '.channels.matrix.groupAllowFrom'
["@manager:...", "@admin:...", "@alice:...", "@host:..."]

# 容器内的配置（未同步）
$ cat /root/manager-workspace/.copaw/workspaces/default/agent.json | jq '.channels.matrix.group_allow_from'
null
```

#### Host Worker 配置检查
```bash
# MinIO 中的配置（正确）
$ docker exec hiclaw-worker-host cat /root/.hiclaw-worker/host/openclaw.json | jq '.channels.matrix.groupAllowFrom'
["@host:...", "@manager:..."]

# 容器内的配置（未同步）
$ docker exec hiclaw-worker-host cat /root/.hiclaw-worker/host/.copaw/workspaces/default/agent.json | jq '.channels.matrix.group_allow_from'
null
```

**结论**: openclaw.json → agent.json 的同步没有发生

---

### 2. 检查 FileSync 日志

#### Manager 日志分析
```bash
$ cat /root/manager-workspace/logs/*.log | grep -i "filesync\|sync_loop\|pull_all"
# 无任何输出
```

#### Host Worker 日志分析
```bash
$ docker exec hiclaw-worker-host cat /root/.hiclaw-worker/host/.copaw/copaw.log | grep -i "filesync\|sync_loop\|pull_all"
# 无任何输出
```

**结论**: FileSync 的 `sync_loop` 和 `push_loop` 没有运行

---

### 3. 验证启动方式

#### Manager 启动命令
```bash
$ docker top hiclaw-manager
python3 -m copaw app --host 0.0.0.0 --port 18799
```
- 使用 `copaw app` 启动（CoPaw 原生模式）
- **没有 FileSync 功能**

#### Host Worker 启动命令
```bash
$ docker top hiclaw-worker-host
/opt/venv/copaw/bin/copaw-worker --name host --fs http://... --fs-key host --fs-secret ... --install-dir /root/.hiclaw-worker --console-port 8088
```
- 使用 `copaw-worker` 启动
- **应该有 FileSync 功能，但日志显示没有运行**

---

### 4. 代码分析

#### copaw-worker 启动流程

**入口点**: `copaw/src/copaw_worker/cli.py:main()`
```python
def main() -> None:
    config = WorkerConfig(...)
    worker = Worker(config)
    asyncio.run(worker.run())
```

**Worker.run() 流程**: `copaw/src/copaw_worker/worker.py:45-53`
```python
async def run(self) -> None:
    if not await self.start():  # 初始化 + 启动 FileSync
        return
    try:
        await self._run_copaw()  # 启动 CoPaw 原生 app
    except asyncio.CancelledError:
        pass
    finally:
        await self.stop()
```

**Worker.start() - FileSync 初始化**: `copaw/src/copaw_worker/worker.py:149-157`
```python
# 9. Start background MinIO sync
asyncio.create_task(
    sync_loop(
        self.sync,
        interval=self.config.sync_interval,
        on_pull=self._on_files_pulled,
    )
)
# Local -> Remote: change-triggered push
asyncio.create_task(push_loop(self.sync, check_interval=5))
```

**Worker._run_copaw() - 启动 CoPaw app**: `copaw/src/copaw_worker/worker.py:169-190`
```python
async def _run_copaw(self) -> None:
    """Start CoPaw via FastAPI app (includes runner + channels + web console)."""
    import uvicorn
    from copaw.app.channels.registry import clear_builtin_channel_cache

    clear_builtin_channel_cache()

    uv_config = uvicorn.Config(
        "copaw.app._app:app",  # CoPaw 原生 FastAPI app
        host="0.0.0.0",
        port=self.config.console_port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)
    await server.serve()
```

---

## 根本原因

### Manager
- **启动方式**: `python3 -m copaw app`
- **问题**: 使用 CoPaw 原生模式启动，完全没有 `copaw-worker` 的 FileSync 功能
- **影响**: 无法从 MinIO 同步配置更新

### Worker
- **启动方式**: `copaw-worker` CLI
- **问题**: 虽然 `Worker.start()` 启动了 `sync_loop` 和 `push_loop`，但随后 `_run_copaw()` 启动了 CoPaw 原生 FastAPI app
- **可能原因**:
  1. **事件循环冲突**: `asyncio.create_task()` 创建的后台任务在 `uvicorn.Server.serve()` 启动后可能被中断或隔离
  2. **生命周期管理**: CoPaw app 有自己的生命周期管理（MultiAgentManager, Workspace），可能覆盖了 `copaw-worker` 的后台任务
  3. **日志级别**: FileSync 的日志可能被 CoPaw app 的日志配置覆盖（但验证后发现日志级别包含 INFO，排除此原因）

### 验证证据

**Host Worker 日志显示的是 CoPaw 原生启动流程**:
```
2026-04-14 12:54:24 | INFO | copaw/app/_app.py:190 | Checking for legacy config migration...
2026-04-14 12:54:24 | INFO | copaw/app/migration.py:109 | Migrating legacy config to multi-agent structure...
2026-04-14 12:54:24 | INFO | copaw/app/multi_agent_manager.py:431 | Starting 2 enabled agent(s)
```

**完全没有 FileSync 相关日志**:
- 没有 "FileSync: files changed" (sync.py:468)
- 没有 "FileSync push: uploaded" (sync.py:690)
- 没有 "mirror_all: full mirror completed" (sync.py:228)

---

## 架构问题

### 当前架构
```
copaw-worker CLI
  ↓
Worker.start()
  ↓ (启动后台任务)
  ├─ sync_loop (asyncio.create_task)
  └─ push_loop (asyncio.create_task)
  ↓
Worker._run_copaw()
  ↓
uvicorn.Server.serve()
  ↓
copaw.app._app:app (FastAPI)
  ↓
MultiAgentManager
  ↓
Workspace (独立的生命周期管理)
```

### 问题点
1. **后台任务与 FastAPI app 的生命周期隔离**: `asyncio.create_task()` 创建的任务在 `uvicorn.Server.serve()` 的事件循环中可能不可见
2. **CoPaw app 的独立初始化**: CoPaw app 有自己的配置加载和初始化流程，不依赖 `copaw-worker` 的 FileSync
3. **日志系统被覆盖**: CoPaw app 的日志配置可能覆盖了 `copaw-worker` 的日志设置

---

## 解决方案

### 方案 1: 将 FileSync 集成到 CoPaw app 生命周期（推荐）

**修改位置**: `copaw/app/_app.py` 或 `copaw/app/multi_agent_manager.py`

**实现思路**:
1. 在 CoPaw app 启动时检测是否运行在 `copaw-worker` 模式
2. 如果是，从环境变量或配置文件读取 MinIO 连接信息
3. 在 MultiAgentManager 或 Workspace 启动时初始化 FileSync
4. 将 `sync_loop` 和 `push_loop` 作为 app 的后台任务启动

**优点**:
- 与 CoPaw app 的生命周期完全集成
- 日志统一管理
- 不需要修改 `copaw-worker` 的架构

**缺点**:
- 需要修改 CoPaw 核心代码
- 增加 CoPaw app 的复杂度

---

### 方案 2: 修改 _run_copaw() 确保后台任务持续运行

**修改位置**: `copaw/src/copaw_worker/worker.py:_run_copaw()`

**实现思路**:
```python
async def _run_copaw(self) -> None:
    import uvicorn
    from copaw.app.channels.registry import clear_builtin_channel_cache

    clear_builtin_channel_cache()

    # 创建 uvicorn server
    uv_config = uvicorn.Config(
        "copaw.app._app:app",
        host="0.0.0.0",
        port=self.config.console_port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)

    # 确保后台任务在同一个事件循环中运行
    # 方法 1: 使用 asyncio.gather 同时运行
    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise
```

**问题**: `server.serve()` 是阻塞的，后台任务已经在 `start()` 中启动，理论上应该在同一个事件循环中运行

**需要验证**: 后台任务是否真的被启动了，还是在某个地方被取消了

---

### 方案 3: 使用独立进程运行 FileSync（不推荐）

**实现思路**:
1. 将 FileSync 作为独立的守护进程运行
2. 使用文件监听（inotify/watchdog）检测 openclaw.json 变化
3. 触发 re-bridge 和配置热更新

**优点**:
- 完全独立，不受 CoPaw app 影响

**缺点**:
- 增加系统复杂度
- 需要额外的进程管理
- 资源开销更大

---

## 临时解决方案

在修复架构问题之前，可以使用以下临时方案：

### 方案 A: 手动重启容器触发配置同步
```bash
# Controller 更新配置后，重启 Manager
docker restart hiclaw-manager

# 或重启 Worker
docker restart hiclaw-worker-<name>
```

### 方案 B: 使用 Controller 的 RestartAgent API
如果 Controller 提供了重启 Agent 的 API，可以在 `UpdateManagerGroupAllowFrom` 后自动触发重启。

---

## 下一步行动

### 优先级 P0
1. **验证后台任务启动**: 在 `Worker.start()` 的 `asyncio.create_task()` 后添加日志，确认任务是否真的被创建
2. **验证事件循环**: 检查 `sync_loop` 和 `push_loop` 是否在 `uvicorn.Server.serve()` 的事件循环中运行
3. **选择解决方案**: 根据验证结果选择方案 1 或方案 2

### 优先级 P1
4. **实现修复**: 根据选择的方案实现代码修改
5. **测试验证**: 重新运行 test-03，验证配置同步功能
6. **文档更新**: 更新 CoPaw Worker 的架构文档

---

## 附录

### 相关文件
- `copaw/src/copaw_worker/cli.py` - CLI 入口点
- `copaw/src/copaw_worker/worker.py` - Worker 主逻辑
- `copaw/src/copaw_worker/sync.py` - FileSync 实现
- `copaw/src/copaw_worker/bridge.py` - openclaw.json → CoPaw 配置转换
- `copaw/app/_app.py` - CoPaw 原生 FastAPI app
- `copaw/app/multi_agent_manager.py` - CoPaw 多 Agent 管理器

### 关键代码位置
- FileSync 初始化: `worker.py:78-86`
- sync_loop 启动: `worker.py:149-155`
- push_loop 启动: `worker.py:157`
- CoPaw app 启动: `worker.py:169-190`
- sync_loop 实现: `sync.py:455-474`
- pull_all 实现: `sync.py:335-378`
- _merge_openclaw_config: `sync.py:50-86`

### 配置文件路径
- openclaw.json: `<install_dir>/<worker_name>/openclaw.json`
- agent.json: `<install_dir>/<worker_name>/.copaw/workspaces/default/agent.json`
- config.json: `<install_dir>/<worker_name>/.copaw/config.json` (已废弃)

### 日志文件路径
- Manager: `/root/manager-workspace/logs/*.log` 和 `/root/manager-workspace/.copaw/copaw.log`
- Worker: `/root/.hiclaw-worker/<name>/logs/*.log` 和 `/root/.hiclaw-worker/<name>/.copaw/copaw.log`
