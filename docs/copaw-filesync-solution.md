# CoPaw FileSync 集成方案：FastAPI Lifespan

**目标**: 在保持 CoPaw FastAPI app 所有能力的前提下，集成 FileSync 配置同步功能

---

## 方案概述

使用 **FastAPI Lifespan Events** 机制，在 app 启动时启动 FileSync 后台任务，在 app 关闭时清理资源。

### 核心优势
1. ✅ **同一事件循环**: FileSync 任务与 FastAPI app 在同一个 asyncio 事件循环中运行
2. ✅ **生命周期管理**: 任务随 app 启动/关闭自动管理
3. ✅ **保持 FastAPI 能力**: 完全不影响 CoPaw 的 Web Console、API 等功能
4. ✅ **日志统一**: 所有日志在同一个进程中输出
5. ✅ **优雅关闭**: app 关闭时自动取消后台任务

---

## 实现方案

### 方案 A: 修改 copaw-worker 的 _run_copaw() (推荐)

**优点**: 不需要修改 CoPaw 核心代码，只修改 copaw-worker 包

#### 实现代码

**文件**: `copaw/src/copaw_worker/worker.py`

```python
async def _run_copaw(self) -> None:
    """Start CoPaw via FastAPI app with FileSync integration."""
    import uvicorn
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    from copaw.app.channels.registry import clear_builtin_channel_cache

    clear_builtin_channel_cache()

    # 导入 CoPaw 原生 app
    from copaw.app._app import app as copaw_app

    # 创建包装 app，注入 FileSync lifespan
    @asynccontextmanager
    async def lifespan_with_filesync(app: FastAPI):
        """Lifespan context manager that runs FileSync alongside CoPaw app."""
        console.print("[yellow]Starting FileSync background tasks...[/yellow]")

        # 启动 FileSync 后台任务
        sync_task = asyncio.create_task(
            sync_loop(
                self.sync,
                interval=self.config.sync_interval,
                on_pull=self._on_files_pulled,
            ),
            name="filesync-pull"
        )

        push_task = asyncio.create_task(
            push_loop(self.sync, check_interval=5),
            name="filesync-push"
        )

        console.print("[green]FileSync tasks started[/green]")

        try:
            # 如果 CoPaw app 有自己的 lifespan，也要执行
            if hasattr(copaw_app, 'router') and copaw_app.router.lifespan_context:
                async with copaw_app.router.lifespan_context(app):
                    yield
            else:
                yield
        finally:
            # 优雅关闭：取消后台任务
            console.print("[yellow]Stopping FileSync tasks...[/yellow]")
            sync_task.cancel()
            push_task.cancel()

            try:
                await asyncio.gather(sync_task, push_task, return_exceptions=True)
            except asyncio.CancelledError:
                pass

            console.print("[green]FileSync tasks stopped[/green]")

    # 将 lifespan 注入到 CoPaw app
    # 注意：这会覆盖原有的 lifespan，需要在上面的代码中保留原有 lifespan 的执行
    copaw_app.router.lifespan_context = lifespan_with_filesync

    # 启动 uvicorn server
    uv_config = uvicorn.Config(
        copaw_app,  # 直接传递 app 对象，而不是字符串
        host="0.0.0.0",
        port=self.config.console_port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)
    console.print(
        f"[bold green]CoPaw console available at "
        f"http://127.0.0.1:{self.config.console_port}/[/bold green]"
    )

    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise
```

#### 关键改动
1. **移除 Worker.start() 中的后台任务启动** (第 149-157 行)
2. **在 _run_copaw() 中通过 lifespan 启动后台任务**
3. **使用 asynccontextmanager 确保优雅关闭**

---

### 方案 B: 修改 CoPaw 核心 app (更彻底)

**优点**: 所有使用 CoPaw 的场景都能受益

#### 实现代码

**文件**: `copaw/app/_app.py`

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
import os

@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifespan: startup and shutdown events."""

    # 检测是否运行在 copaw-worker 模式
    filesync_enabled = os.environ.get("COPAW_FILESYNC_ENABLED") == "1"

    if filesync_enabled:
        # 从环境变量读取 FileSync 配置
        from copaw_worker.sync import FileSync, sync_loop, push_loop
        from pathlib import Path

        sync = FileSync(
            endpoint=os.environ["COPAW_FILESYNC_ENDPOINT"],
            access_key=os.environ["COPAW_FILESYNC_ACCESS_KEY"],
            secret_key=os.environ["COPAW_FILESYNC_SECRET_KEY"],
            bucket=os.environ.get("COPAW_FILESYNC_BUCKET", "hiclaw-storage"),
            worker_name=os.environ["COPAW_FILESYNC_WORKER_NAME"],
            secure=os.environ.get("COPAW_FILESYNC_SECURE", "false").lower() == "true",
            local_dir=Path(os.environ["COPAW_FILESYNC_LOCAL_DIR"]),
        )

        # 启动后台任务
        sync_task = asyncio.create_task(
            sync_loop(
                sync,
                interval=int(os.environ.get("COPAW_FILESYNC_INTERVAL", "300")),
                on_pull=lambda files: _on_filesync_pull(files, sync),
            ),
            name="filesync-pull"
        )

        push_task = asyncio.create_task(
            push_loop(sync, check_interval=5),
            name="filesync-push"
        )

        logger.info("FileSync background tasks started")

    # 原有的启动逻辑
    yield

    # 关闭时清理
    if filesync_enabled:
        sync_task.cancel()
        push_task.cancel()
        await asyncio.gather(sync_task, push_task, return_exceptions=True)
        logger.info("FileSync background tasks stopped")


async def _on_filesync_pull(files: list[str], sync: FileSync):
    """Handle files pulled from MinIO."""
    if "openclaw.json" in files:
        from copaw_worker.bridge import bridge_openclaw_to_copaw
        # 触发 re-bridge 和热更新
        # ... (需要访问 workspace 实例)


app = FastAPI(lifespan=lifespan)
```

**Worker.start() 修改**:
```python
async def start(self) -> bool:
    # ... 原有初始化逻辑 ...

    # 不再直接启动后台任务，而是通过环境变量传递配置
    os.environ["COPAW_FILESYNC_ENABLED"] = "1"
    os.environ["COPAW_FILESYNC_ENDPOINT"] = self.config.minio_endpoint
    os.environ["COPAW_FILESYNC_ACCESS_KEY"] = self.config.minio_access_key
    os.environ["COPAW_FILESYNC_SECRET_KEY"] = self.config.minio_secret_key
    os.environ["COPAW_FILESYNC_BUCKET"] = self.config.minio_bucket
    os.environ["COPAW_FILESYNC_WORKER_NAME"] = self.worker_name
    os.environ["COPAW_FILESYNC_LOCAL_DIR"] = str(self.config.install_dir / self.worker_name)
    os.environ["COPAW_FILESYNC_INTERVAL"] = str(self.config.sync_interval)

    return True
```

---

## 方案对比

| 特性 | 方案 A (修改 copaw-worker) | 方案 B (修改 CoPaw 核心) |
|------|---------------------------|-------------------------|
| 代码侵入性 | 低（只修改 copaw-worker） | 中（修改 CoPaw 核心） |
| 适用范围 | 仅 copaw-worker | 所有 CoPaw 使用场景 |
| 维护成本 | 低 | 中 |
| 灵活性 | 高（copaw-worker 独立控制） | 高（统一管理） |
| 实现难度 | 简单 | 中等（需要处理 workspace 访问） |
| 推荐度 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |

---

## 方案 C: 混合方案（最灵活）

结合方案 A 和 B 的优点：

1. **CoPaw 核心提供 FileSync 钩子接口**
2. **copaw-worker 实现具体的 FileSync 逻辑**

### CoPaw 核心 (_app.py)

```python
from typing import Optional, Callable, Awaitable

# 全局 FileSync 钩子
_filesync_startup_hook: Optional[Callable[[], Awaitable[list[asyncio.Task]]]] = None
_filesync_shutdown_hook: Optional[Callable[[list[asyncio.Task]], Awaitable[None]]] = None

def register_filesync_hooks(
    startup: Callable[[], Awaitable[list[asyncio.Task]]],
    shutdown: Callable[[list[asyncio.Task]], Awaitable[None]]
):
    """Register FileSync lifecycle hooks."""
    global _filesync_startup_hook, _filesync_shutdown_hook
    _filesync_startup_hook = startup
    _filesync_shutdown_hook = shutdown


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App lifespan with optional FileSync integration."""
    filesync_tasks = []

    # 如果注册了 FileSync 钩子，执行启动逻辑
    if _filesync_startup_hook:
        filesync_tasks = await _filesync_startup_hook()

    yield

    # 关闭时执行清理逻辑
    if _filesync_shutdown_hook and filesync_tasks:
        await _filesync_shutdown_hook(filesync_tasks)


app = FastAPI(lifespan=lifespan)
```

### copaw-worker (worker.py)

```python
async def _run_copaw(self) -> None:
    """Start CoPaw via FastAPI app with FileSync integration."""
    import uvicorn
    from copaw.app.channels.registry import clear_builtin_channel_cache
    from copaw.app._app import register_filesync_hooks

    clear_builtin_channel_cache()

    # 注册 FileSync 钩子
    async def filesync_startup() -> list[asyncio.Task]:
        console.print("[yellow]Starting FileSync background tasks...[/yellow]")

        sync_task = asyncio.create_task(
            sync_loop(self.sync, interval=self.config.sync_interval, on_pull=self._on_files_pulled),
            name="filesync-pull"
        )
        push_task = asyncio.create_task(
            push_loop(self.sync, check_interval=5),
            name="filesync-push"
        )

        console.print("[green]FileSync tasks started[/green]")
        return [sync_task, push_task]

    async def filesync_shutdown(tasks: list[asyncio.Task]) -> None:
        console.print("[yellow]Stopping FileSync tasks...[/yellow]")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        console.print("[green]FileSync tasks stopped[/green]")

    register_filesync_hooks(filesync_startup, filesync_shutdown)

    # 启动 uvicorn server
    uv_config = uvicorn.Config(
        "copaw.app._app:app",
        host="0.0.0.0",
        port=self.config.console_port,
        log_level="info",
    )
    server = uvicorn.Server(uv_config)
    console.print(
        f"[bold green]CoPaw console available at "
        f"http://127.0.0.1:{self.config.console_port}/[/bold green]"
    )

    try:
        await server.serve()
    except asyncio.CancelledError:
        server.should_exit = True
        raise
```

### 优点
- ✅ **松耦合**: CoPaw 核心不依赖 copaw-worker
- ✅ **可扩展**: 其他集成也可以使用钩子机制
- ✅ **清晰的职责**: CoPaw 提供接口，copaw-worker 提供实现

---

## 推荐实施步骤

### 阶段 1: 快速修复（方案 A）
1. 修改 `copaw/src/copaw_worker/worker.py` 的 `_run_copaw()` 方法
2. 移除 `Worker.start()` 中的后台任务启动代码
3. 测试验证 FileSync 功能

**预计工作量**: 2-3 小时

### 阶段 2: 架构优化（方案 C）
1. 在 CoPaw 核心添加 FileSync 钩子接口
2. 重构 copaw-worker 使用钩子机制
3. 更新文档和测试

**预计工作量**: 1-2 天

---

## 验证方法

### 1. 检查后台任务是否启动
```python
# 在 lifespan 启动后添加日志
import asyncio
tasks = [t for t in asyncio.all_tasks() if 'filesync' in t.get_name()]
print(f"FileSync tasks: {tasks}")
```

### 2. 测试配置同步
```bash
# 1. 启动 Worker
copaw-worker --name test --fs http://...

# 2. 修改 MinIO 中的 openclaw.json
mc cp updated-openclaw.json minio/hiclaw-storage/agents/test/openclaw.json

# 3. 等待 sync_interval (默认 300 秒) 或手动触发
# 4. 检查容器内的 agent.json 是否更新
docker exec hiclaw-worker-test cat /root/.hiclaw-worker/test/.copaw/workspaces/default/agent.json
```

### 3. 检查日志输出
```bash
# 应该看到 FileSync 相关日志
docker logs hiclaw-worker-test | grep -i filesync
```

预期输出：
```
Starting FileSync background tasks...
FileSync tasks started
FileSync: files changed: ['openclaw.json']
Config changed, re-bridging...
Synced Matrix allowFrom: [...]
```

---

## 总结

**推荐方案**: 先实施方案 A（快速修复），后续优化为方案 C（架构优化）

**核心原理**: 利用 FastAPI 的 lifespan 机制，确保 FileSync 后台任务与 FastAPI app 在同一个事件循环中运行，生命周期完全同步。

**关键优势**:
- 保持 CoPaw FastAPI app 的所有能力
- FileSync 任务与 app 生命周期绑定
- 优雅启动和关闭
- 日志统一管理
