# HiClaw Embed 模式集成测试流程分析

## 概述

HiClaw 的 **embed 模式**（也称为 embedded mode 或 dual-container mode）是一种双容器架构，将基础设施和 Manager Agent 分离到两个独立容器中：

1. **hiclaw-controller** — 嵌入式控制器容器（基础设施 + Controller）
2. **hiclaw-manager** — Manager Agent 容器（由 controller 自动创建）

这种架构相比传统的 all-in-one 单容器模式，实现了更清晰的职责分离和更灵活的部署方式。

## 架构对比

### 传统模式（Legacy All-in-One）
```
┌─────────────────────────────────────┐
│     hiclaw-manager (单容器)          │
├─────────────────────────────────────┤
│ • Higress Gateway                   │
│ • Higress Console                   │
│ • Tuwunel (Matrix Server)           │
│ • MinIO (Object Storage)            │
│ • Element Web                       │
│ • Manager Agent (OpenClaw/CoPaw)    │
│ • mc-mirror (文件同步)               │
└─────────────────────────────────────┘
```

### Embed 模式（Dual-Container）
```
┌─────────────────────────────────────┐
│   hiclaw-controller (基础设施容器)   │
├─────────────────────────────────────┤
│ • Higress Gateway                   │
│ • Higress Console                   │
│ • Tuwunel (Matrix Server)           │
│ • MinIO (Object Storage)            │
│ • Element Web                       │
│ • nginx (Element Web 服务)          │
│ • hiclaw-controller (控制器)        │
│ • kube-apiserver (嵌入式 K8s API)   │
│ • kine (SQLite 后端)                │
└─────────────────────────────────────┘
          ↓ 通过 Docker API 创建
┌─────────────────────────────────────┐
│    hiclaw-manager (Agent 容器)       │
├─────────────────────────────────────┤
│ • Manager Agent (OpenClaw/CoPaw)    │
│ • mc-mirror (文件同步)               │
└─────────────────────────────────────┘
```

## 集成测试流程

### 1. CI/CD 触发入口

**GitHub Actions Workflow**: `.github/workflows/test-integration.yml`

```yaml
- name: Run embedded integration tests
  run: |
    make test-embedded \
      DOCKER_BUILD_ARGS="--build-arg APT_MIRROR=" \
      TEST_FILTER="$FILTER"
```

**触发条件**：
- PR 提交到 main 分支
- Push 到 main 分支
- 打 tag（`v*`）
- 手动触发（workflow_dispatch）

**测试范围**：
- 默认运行所有不需要 GitHub token 的测试：`01 02 03 04 05 06 14 15 17 18 19 20 100`
- 可通过 `test_filter` 参数指定特定测试

### 2. Makefile 测试目标

**`make test-embedded`** (Makefile:556-567)

```makefile
test-embedded: ## Run integration tests in embedded mode
ifdef SKIP_INSTALL
	@echo "==> Running tests against existing embedded installation"
	@docker exec hiclaw-manager touch /root/manager-workspace/yolo-mode 2>/dev/null || true
	./tests/run-all-tests.sh --skip-build --use-existing $(if $(TEST_FILTER),--test-filter "$(TEST_FILTER)")
else
	@echo "==> Installing embedded mode and running tests"
	$(MAKE) uninstall-embedded 2>/dev/null || true
	HICLAW_YOLO=1 $(MAKE) install-embedded
	$(MAKE) wait-ready-embedded
	./tests/run-all-tests.sh --skip-build --use-existing $(if $(TEST_FILTER),--test-filter "$(TEST_FILTER)")
endif
```

**执行步骤**：
1. 清理旧容器（`uninstall-embedded`）
2. 构建并安装 embed 模式（`install-embedded`）
3. 等待服务就绪（`wait-ready-embedded`）
4. 运行测试套件（`run-all-tests.sh`）

### 3. 构建镜像

**`make install-embedded`** (Makefile:520-531)

```makefile
install-embedded: ## Install in embedded mode (dual-container: controller + agent)
ifndef SKIP_BUILD
	$(MAKE) build-embedded build-manager build-manager-copaw build-worker build-copaw-worker
endif
	@echo "==> Installing HiClaw (embedded mode)..."
	HICLAW_NON_INTERACTIVE=1 \
		HICLAW_EMBEDDED_IMAGE=$(LOCAL_EMBEDDED) \
		HICLAW_MANAGER_IMAGE=$(if $(filter copaw,$(HICLAW_MANAGER_RUNTIME)),$(LOCAL_MANAGER_COPAW),$(LOCAL_MANAGER)) \
		HICLAW_WORKER_IMAGE=$(LOCAL_WORKER) \
		HICLAW_COPAW_WORKER_IMAGE=$(LOCAL_COPAW_WORKER) \
		HICLAW_MATRIX_E2EE=0 \
		bash ./install/hiclaw-install-embedded.sh
```

**构建的镜像**：
- `hiclaw/hiclaw-embedded:latest` — 嵌入式控制器（基于 `hiclaw-controller/Dockerfile.embedded`）
- `hiclaw/manager-agent:latest` — Manager Agent（OpenClaw runtime）
- `hiclaw/manager-copaw:latest` — Manager Agent（CoPaw runtime）
- `hiclaw/worker-agent:latest` — Worker Agent（OpenClaw runtime）
- `hiclaw/copaw-worker:latest` — Worker Agent（CoPaw runtime）

### 4. 安装脚本

**`install/hiclaw-install-embedded.sh`**

**核心流程**：

#### 4.1 环境变量配置
```bash
HICLAW_LLM_PROVIDER="${HICLAW_LLM_PROVIDER:-qwen}"
HICLAW_DEFAULT_MODEL="${HICLAW_DEFAULT_MODEL:-qwen3.5-plus}"
HICLAW_ADMIN_USER="${HICLAW_ADMIN_USER:-admin}"
HICLAW_ADMIN_PASSWORD="${HICLAW_ADMIN_PASSWORD:-admin123}"
HICLAW_PORT_GATEWAY="${HICLAW_PORT_GATEWAY:-18080}"
HICLAW_PORT_CONSOLE="${HICLAW_PORT_CONSOLE:-18001}"
HICLAW_PORT_ELEMENT_WEB="${HICLAW_PORT_ELEMENT_WEB:-18088}"
HICLAW_YOLO="${HICLAW_YOLO:-0}"  # 自动决策模式
```

#### 4.2 启动 hiclaw-controller 容器
```bash
docker run -d \
    --name hiclaw-controller \
    --network hiclaw-net \
    --network-alias matrix-local.hiclaw.io \
    --network-alias aigw-local.hiclaw.io \
    --network-alias fs-local.hiclaw.io \
    -e HICLAW_MANAGER_ENABLED=true \
    -e HICLAW_MANAGER_IMAGE=${MANAGER_IMAGE} \
    -e HICLAW_WORKER_IMAGE=${WORKER_IMAGE} \
    -e HICLAW_COPAW_WORKER_IMAGE=${COPAW_WORKER_IMAGE} \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v hiclaw-data:/data \
    -v ${HICLAW_WORKSPACE_DIR}:/root/hiclaw-fs/agents/manager \
    -p 127.0.0.1:18080:8080 \
    -p 127.0.0.1:18001:8001 \
    -p 127.0.0.1:18088:8088 \
    --restart unless-stopped \
    ${EMBEDDED_IMAGE}
```

**关键配置**：
- `HICLAW_MANAGER_ENABLED=true` — 启用 Manager 自动创建
- 挂载 Docker socket — 允许 controller 创建 Manager 容器
- 挂载 `hiclaw-data` volume — 持久化数据（MinIO、kine SQLite、证书）
- 挂载 workspace 目录 — Manager 工作空间文件同步

#### 4.3 等待基础设施就绪
```bash
wait_for_url "http://127.0.0.1:6167/_tuwunel/server_version" "${EMBEDDED_CTR}" 120 "Tuwunel (Matrix)"
wait_for_url "http://127.0.0.1:9000/minio/health/live" "${EMBEDDED_CTR}" 60 "MinIO"
wait_for_url "http://127.0.0.1:8080/status" "${EMBEDDED_CTR}" 120 "Higress Gateway"
```

#### 4.4 等待 Manager Agent 自动创建
```bash
MAX_WAIT=300
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
    if docker ps --format '{{.Names}}' | grep -q "^hiclaw-manager$"; then
        log "Manager Agent container detected: hiclaw-manager (${ELAPSED}s)"
        break
    fi
    sleep 3
    ELAPSED=$((ELAPSED + 3))
done
```

**Manager 创建机制**：
- hiclaw-controller 启动后，`ManagerReconciler` 检测到 `HICLAW_MANAGER_ENABLED=true`
- 自动创建 `Manager` CR（Custom Resource）
- `ManagerReconciler` 通过 `DockerBackend` 调用 Docker API 创建 `hiclaw-manager` 容器
- Manager 容器自动加入 `hiclaw-net` 网络，可访问基础设施服务

#### 4.5 启用 YOLO 模式（测试专用）
```bash
if [ "${HICLAW_YOLO}" = "1" ]; then
    docker exec hiclaw-manager touch /root/manager-workspace/yolo-mode
    log "Yolo mode enabled"
fi
```

**YOLO 模式**：Manager Agent 自动做决策，不等待人类确认（加速测试）

### 5. 等待服务就绪

**`make wait-ready-embedded`** (Makefile:533-554)

```bash
# 检查 4 个条件：
# 1. Matrix Server (HTTP 200)
# 2. MinIO (HTTP 200)
# 3. Higress Console (HTTP 200)
# 4. hiclaw-manager 容器存在且运行中

# 全部就绪后，额外等待 60s 让 Manager Agent 完成初始化
sleep 60
```

**为什么需要额外等待 60s？**
- Manager Agent 启动后需要：
  - 加载 SOUL.md、AGENTS.md、skills
  - 连接 Matrix Server 并登录
  - 初始化 mcporter（MCP Server 客户端）
  - 启动 heartbeat 定时任务
  - 处理欢迎消息（onboarding）

### 6. 运行测试套件

**`tests/run-all-tests.sh --skip-build --use-existing`**

#### 6.1 加载环境配置
```bash
load_env_file() {
    local env_file="${HICLAW_ENV_FILE:-${HOME}/hiclaw-manager.env}"
    # 从 env 文件加载：
    # - TEST_ADMIN_USER
    # - TEST_ADMIN_PASSWORD
    # - TEST_MATRIX_DOMAIN
    # - TEST_GATEWAY_PORT
    # - TEST_CONSOLE_PORT
}
```

#### 6.2 配置 Manager 身份（英文）
```bash
_setup_manager_identity() {
    # 发送身份配置消息到 Manager DM 房间
    matrix_send_message "${admin_token}" "${dm_room}" \
        "Here is my identity configuration for you:
- Name: Manager
- Language: English (always respond in English)
- Style: concise and professional
- No special constraints

Please update your SOUL.md with these preferences, then run: touch ~/soul-configured"

    # 等待 Manager 处理并创建 soul-configured 标记文件
    # 确保后续测试不会收到 onboarding 消息干扰
}
```

#### 6.3 运行测试用例
```bash
for test_file in "${TESTS[@]}"; do
    test_name=$(basename "${test_file}" .sh)
    log "Running: ${test_name}"

    # 等待 Manager 完成上一个测试的处理
    wait_for_session_stable 10 120

    if bash "${test_file}"; then
        RESULTS+=("PASS: ${test_name}")
        TOTAL_PASS=$((TOTAL_PASS + 1))
    else
        RESULTS+=("FAIL: ${test_name}")
        TOTAL_FAIL=$((TOTAL_FAIL + 1))
    fi
done
```

**测试用例示例**：
- `test-01-manager-boot.sh` — Manager 启动和健康检查
- `test-02-create-worker.sh` — 创建 Worker
- `test-03-worker-task.sh` — Worker 执行任务
- `test-04-team-creation.sh` — 创建 Team
- `test-05-team-task.sh` — Team 协作任务
- ...

### 7. 测试辅助工具

#### 7.1 Matrix 客户端库
**`tests/lib/matrix-client.sh`**

```bash
matrix_login()           # 登录获取 access_token
matrix_find_dm_room()    # 查找 DM 房间
matrix_send_message()    # 发送消息
matrix_read_messages()   # 读取消息
matrix_wait_for_reply()  # 等待 Agent 回复
```

#### 7.2 Agent 指标收集
**`tests/lib/agent-metrics.sh`**

```bash
wait_for_session_stable()        # 等待 session 稳定（无新消息）
wait_for_manager_agent_ready()   # 等待 Manager Agent 就绪
collect_agent_metrics()          # 收集 Agent 指标（token、工具调用）
generate_metrics_summary()       # 生成指标汇总
compare_metrics_with_baseline()  # 与 baseline 对比
```

**收集的指标**：
- `total_messages` — 总消息数
- `input_tokens` — 输入 token 数
- `output_tokens` — 输出 token 数
- `tool_calls` — 工具调用次数
- `thinking_blocks` — 思考块数量
- `session_duration_seconds` — 会话时长

#### 7.3 容器检测
**`tests/lib/test-helpers.sh`**

```bash
detect_infra_container()   # 自动检测基础设施容器（embedded 或 legacy）
detect_agent_container()   # 自动检测 Agent 容器（embedded 模式下是独立容器）
```

**兼容性设计**：测试脚本同时支持 legacy 和 embedded 模式，通过容器名称自动检测。

### 8. 测试结果处理

#### 8.1 生成测试报告
```bash
echo "========================================"
echo "  Integration Test Results"
echo "========================================"
echo "  Total:  $((TOTAL_PASS + TOTAL_FAIL))"
echo "  Passed: ${TOTAL_PASS}"
echo "  Failed: ${TOTAL_FAIL}"
echo "========================================"
```

#### 8.2 导出调试日志
```bash
python3 scripts/export-debug-log.py --range 2h --no-redact
```

**导出内容**：
- Manager Agent session 日志
- Worker Agent session 日志
- 容器日志（manager-agent.log, hiclaw-controller.log, tuwunel.log, etc.）
- 指标数据（metrics-*.json）

#### 8.3 上传 Artifacts
```yaml
- name: Upload test artifacts
  uses: actions/upload-artifact@v4
  with:
    name: test-artifacts-${{ github.sha }}
    path: test-artifacts/
    retention-days: 7
```

**Artifacts 包含**：
- `tests/output/result-*.txt` — 测试结果
- `tests/output/metrics-*.json` — 指标数据
- `debug-log/` — 完整调试日志

#### 8.4 PR 评论（仅 PR 触发）
```bash
# 下载最新 release 的 baseline
gh release download "$LATEST" --pattern "metrics-baseline.json"

# 对比当前指标与 baseline
COMPARISON=$(compare_metrics_with_baseline "$CURRENT" "$BASELINE")

# 生成 Markdown 报告并发布到 PR
gh api --method POST "repos/$REPO/issues/$PR_NUM/comments" -f body="$REPORT"
```

**报告内容**：
- 📊 指标对比（token 使用、工具调用、会话时长）
- ✅/❌ 测试通过/失败状态
- 📦 Artifacts 下载链接

### 9. Release Baseline 生成

**触发条件**：Push tag `v*`

```bash
# 运行完整测试套件
make test-embedded TEST_FILTER="$NON_GITHUB_TESTS"

# 生成 baseline
generate_metrics_summary $TEST_NAMES > metrics-baseline.json

# 上传到 release
gh release upload "${GITHUB_REF_NAME}" metrics-baseline.json --clobber
```

**Baseline 用途**：
- 作为后续 PR 的性能对比基准
- 检测性能回退（token 使用增加、工具调用增多）
- 跟踪版本间的性能变化趋势

## 关键技术点

### 1. 嵌入式 Kubernetes API Server

**`hiclaw-controller/internal/apiserver/embedded.go`**

- 使用 `kube-apiserver` 二进制 + `kine`（SQLite 后端）实现轻量级 K8s API
- 自签名证书 + static token 认证
- 自动注册 CRD（Worker, Team, Human, Manager）
- 无需外部 etcd 或 K8s 集群

**优势**：
- 零依赖部署（单个容器即可运行完整 K8s API）
- 数据持久化到 SQLite（`/data/kine.db`）
- 支持完整的 K8s 资源管理（CRUD、Watch、List）

### 2. Manager 自动创建机制

**`hiclaw-controller/internal/controller/manager_controller.go`**

```go
// ManagerReconciler watches Manager CR and ensures the container exists
func (r *ManagerReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
    var manager v1beta1.Manager
    if err := r.Get(ctx, req.NamespacedName, &manager); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }

    // Check if container exists
    exists, err := r.backend.ContainerExists(ctx, manager.Name)
    if err != nil {
        return ctrl.Result{}, err
    }

    if !exists {
        // Create container via Docker API
        if err := r.deployer.DeployManager(ctx, &manager); err != nil {
            return ctrl.Result{}, err
        }
    }

    // Update status
    return ctrl.Result{}, r.updateStatus(ctx, &manager)
}
```

**初始化流程**：
1. hiclaw-controller 启动时，`initializer` 检测 `HICLAW_MANAGER_ENABLED=true`
2. 创建默认 `Manager` CR（`name: manager`）
3. `ManagerReconciler` 监听到 CR 创建事件
4. 调用 `DockerBackend.CreateContainer()` 创建 `hiclaw-manager` 容器
5. 容器启动后，Manager Agent 自动连接基础设施服务

### 3. 网络通信

**容器网络拓扑**：
```
hiclaw-net (bridge network)
├── hiclaw-controller (aliases: matrix-local.hiclaw.io, aigw-local.hiclaw.io, fs-local.hiclaw.io)
├── hiclaw-manager (auto-created, joins hiclaw-net)
└── hiclaw-worker-* (created by Manager, joins hiclaw-net)
```

**域名解析**：
- `matrix-local.hiclaw.io:18080` → hiclaw-controller:8080 (Higress Gateway)
- `aigw-local.hiclaw.io:8080` → hiclaw-controller:8080 (AI Gateway)
- `fs-local.hiclaw.io:8080` → hiclaw-controller:8080 (MinIO via Higress)

**端口映射**（仅 controller 容器）：
- `127.0.0.1:18080` → 8080 (Higress Gateway)
- `127.0.0.1:18001` → 8001 (Higress Console)
- `127.0.0.1:18088` → 8088 (Element Web)

### 4. 文件同步

**Manager Workspace 同步**：
- Host: `~/hiclaw-manager/` (或 `HICLAW_WORKSPACE_DIR`)
- Controller: `/root/hiclaw-fs/agents/manager/` (volume mount)
- Manager: `/root/manager-workspace/` (mc-mirror 同步)

**同步机制**：
- Manager 容器内的 `mc-mirror` 进程监听 MinIO bucket
- 定期同步 `hiclaw-storage/agents/manager/` → `/root/manager-workspace/`
- 双向同步：Agent 写入的文件也会上传到 MinIO

### 5. YOLO 模式

**实现原理**：
```bash
# 在 Manager workspace 创建标记文件
touch /root/manager-workspace/yolo-mode

# Manager Agent 启动时检测此文件
if [ -f ~/yolo-mode ]; then
    export HICLAW_AUTO_DECISION=1
fi
```

**效果**：
- Manager 遇到需要人类确认的操作时，自动批准
- 跳过交互式提示（如创建 Worker、删除资源）
- 测试环境专用，生产环境禁用

## 常见问题排查

### 1. Manager 容器未创建

**症状**：`wait-ready-embedded` 超时，找不到 `hiclaw-manager` 容器

**排查步骤**：
```bash
# 查看 controller 日志
docker exec hiclaw-controller tail -100 /var/log/hiclaw/hiclaw-controller.log

# 检查 Manager CR 状态
docker exec hiclaw-controller hiclaw get managers

# 检查 Docker socket 权限
docker exec hiclaw-controller ls -l /var/run/docker.sock
```

**常见原因**：
- Docker socket 未正确挂载
- `HICLAW_MANAGER_ENABLED` 未设置或为 false
- Manager 镜像拉取失败（网络问题）
- 端口冲突（18080/18001/18088 已被占用）

### 2. 测试超时

**症状**：测试用例运行超过 2 分钟无响应

**排查步骤**：
```bash
# 查看 Manager session 日志
docker exec hiclaw-manager ls -lh /root/manager-workspace/.openclaw/agents/main/sessions/

# 查看最新 session
docker exec hiclaw-manager tail -100 /root/manager-workspace/.openclaw/agents/main/sessions/$(docker exec hiclaw-manager ls -t /root/manager-workspace/.openclaw/agents/main/sessions/ | head -1)

# 检查 LLM 调用
docker exec hiclaw-controller tail -100 /var/log/hiclaw/higress-gateway.log | grep -i error
```

**常见原因**：
- LLM API key 无效或过期
- LLM 服务限流或超时
- Manager 陷入思考循环（thinking block 过多）
- Matrix 消息未送达（Tuwunel 异常）

### 3. 指标收集失败

**症状**：`tests/output/metrics-*.json` 文件为空或缺失

**排查步骤**：
```bash
# 检查 session 文件是否存在
docker exec hiclaw-manager ls -lh /root/manager-workspace/.openclaw/agents/main/sessions/

# 手动运行指标收集
source tests/lib/agent-metrics.sh
collect_agent_metrics "test-02-create-worker"
```

**常见原因**：
- Session 文件路径错误（OpenClaw vs CoPaw 路径不同）
- Session 文件格式异常（JSON 解析失败）
- 容器名称检测错误（legacy vs embedded 模式混淆）

## 最佳实践

### 1. 本地调试

```bash
# 安装 embed 模式（不运行测试）
make install-embedded

# 手动运行单个测试
TEST_ADMIN_USER=admin \
TEST_ADMIN_PASSWORD=admin123 \
TEST_MATRIX_DOMAIN=matrix-local.hiclaw.io:18080 \
bash tests/test-02-create-worker.sh

# 查看实时日志
docker logs -f hiclaw-manager
docker exec hiclaw-controller tail -f /var/log/hiclaw/hiclaw-controller.log
```

### 2. 快速迭代

```bash
# 跳过构建，使用现有镜像
make test-embedded SKIP_BUILD=1

# 只运行特定测试
make test-embedded TEST_FILTER="01 02"

# 使用已安装的环境（跳过安装）
make test-embedded SKIP_INSTALL=1
```

### 3. 性能优化

```bash
# 使用国内镜像加速
make test-embedded DOCKER_BUILD_ARGS="--build-arg APT_MIRROR=mirrors.aliyun.com"

# 减少等待时间（风险：可能导致测试不稳定）
# 修改 wait-ready-embedded 中的 sleep 60 → sleep 30
```

### 4. CI/CD 优化

```yaml
# 缓存 Docker 层
- name: Cache Docker layers
  uses: actions/cache@v3
  with:
    path: /tmp/.buildx-cache
    key: ${{ runner.os }}-buildx-${{ github.sha }}
    restore-keys: |
      ${{ runner.os }}-buildx-

# 并行运行测试（需要修改测试脚本支持）
strategy:
  matrix:
    test_group: ["01 02 03", "04 05 06", "14 15 17"]
```

## 总结

HiClaw 的 embed 模式集成测试流程实现了：

1. **完全自动化** — 从构建镜像到运行测试，无需人工干预
2. **环境隔离** — 每次测试都是全新环境，避免状态污染
3. **指标跟踪** — 自动收集性能指标，对比 baseline 检测回退
4. **调试友好** — 导出完整日志，支持本地复现
5. **CI/CD 集成** — GitHub Actions 自动运行，PR 评论反馈

通过 embed 模式，HiClaw 实现了更清晰的架构分层和更灵活的部署方式，为未来的 Kubernetes 原生部署和多租户支持奠定了基础。
