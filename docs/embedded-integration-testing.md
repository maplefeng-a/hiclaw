# Embedded 模式集成测试指南

## 1. 测试架构概览

### 1.1 测试框架结构

```
tests/
├── run-all-tests.sh              # 主测试编排器
├── lib/
│   ├── test-helpers.sh           # 通用测试工具（断言、等待、日志）
│   ├── matrix-client.sh          # Matrix API 客户端
│   ├── higress-client.sh         # Higress API 客户端
│   ├── minio-client.sh           # MinIO 客户端
│   └── agent-metrics.sh          # Agent 性能指标收集
├── test-01-manager-boot.sh       # 基础设施健康检查
├── test-02-create-worker.sh      # Worker 创建测试
├── test-18-team-config-verify.sh # Team 配置验证
├── test-19-human-and-team-admin.sh # Team Admin 测试
└── test-100-cleanup.sh           # 清理测试
```

### 1.2 Embedded 模式架构

```
┌─────────────────────────────────────────────────────────────┐
│ hiclaw-controller Container (独立容器)                      │
│ ├── hiclaw-controller (Go 进程)                            │
│ │   ├── Embedded kube-apiserver + kine (SQLite)           │
│ │   ├── CRD Reconcilers (Worker/Team/Human)               │
│ │   ├── Docker Backend (直接管理容器)                      │
│ │   └── HTTP API Server (:8090)                           │
│ ├── Tuwunel (Matrix Server, :6167)                         │
│ ├── MinIO (对象存储, :9000)                                │
│ ├── Higress (AI Gateway, :8080)                            │
│ └── Element Web (IM UI, :8088)                             │
└─────────────────────────────────────────────────────────────┘
                          ↓ 管理
┌─────────────────────────────────────────────────────────────┐
│ hiclaw-manager Container (可选，Manager Agent)              │
│ └── Manager Agent (OpenClaw/CoPaw)                          │
└─────────────────────────────────────────────────────────────┘
                          ↓ 管理
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ Worker A     │ │ Worker B     │ │ Team Leader  │
│ Container    │ │ Container    │ │ Container    │
└──────────────┘ └──────────────┘ └──────────────┘
```

---

## 2. 测试执行流程

### 2.1 完整测试流程（make test）

```bash
make test
```

**执行步骤**:
1. 卸载旧环境: `make uninstall`
2. 构建镜像: `make build` (如果未设置 SKIP_BUILD)
3. 安装 embedded 环境: `HICLAW_YOLO=1 make install`
4. 等待就绪: `make wait-ready`
5. 运行所有测试: `./tests/run-all-tests.sh --skip-build --use-existing`
6. 清理环境: trap cleanup (自动)

### 2.2 使用现有环境测试（推荐用于开发）

```bash
# 方式 1: 使用 Makefile
make test SKIP_INSTALL=1

# 方式 2: 直接运行测试脚本
./tests/run-all-tests.sh --use-existing

# 方式 3: 运行特定测试
make test SKIP_INSTALL=1 TEST_FILTER="01 02 18"
```

### 2.3 快速烟雾测试

```bash
# 只运行 test-01 (基础设施健康检查)
make test-quick
```

---

## 3. 测试用例详解

### 3.1 test-01-manager-boot.sh (基础设施健康检查)

**测试目标**: 验证 embedded 环境的所有基础设施正常运行

**检查项**:
```bash
# 1. 端口可访问性
- Higress Gateway (18080)
- Higress Console (18001)
- Element Web (18088)

# 2. 内部服务健康
- Tuwunel Matrix (6167)
- MinIO API (9000)

# 3. Matrix 登录
- Admin 用户登录成功
- 获取 access_token

# 4. Higress Console
- 登录成功
- Manager consumer 存在

# 5. MinIO 存储
- mc alias 配置成功
- Manager SOUL.md 存在
- Manager AGENTS.md 存在
- Manager HEARTBEAT.md 存在

# 6. Manager Runtime 检测
- 检测 HICLAW_MANAGER_RUNTIME 环境变量
- 验证 CoPaw/OpenClaw 配置文件
- 验证进程运行状态

# 7. Manager Agent 通信
- 尝试与 Manager 建立 DM
- 验证 Manager 响应
```

**关键代码**:
```bash
# 容器检测（自动识别 embedded 模式）
TEST_CONTROLLER_CONTAINER="hiclaw-controller"
TEST_AGENT_CONTAINER="hiclaw-manager"  # 如果存在

# 端口检测（从容器环境变量读取）
detect_manager_config()

# Runtime 检测
MANAGER_RUNTIME=$(docker exec "${_AGENT_CTR}" printenv HICLAW_MANAGER_RUNTIME)
case "${MANAGER_RUNTIME}" in
    copaw)
        # 验证 CoPaw 配置和进程
        ;;
    *)
        # 验证 OpenClaw 配置和进程
        ;;
esac
```

### 3.2 test-02-create-worker.sh (Worker 创建测试)

**测试目标**: 验证通过 Matrix 对话创建 Worker 的完整流程

**测试流程**:
```bash
# 1. 登录 Admin 用户
ADMIN_TOKEN=$(matrix_login "${TEST_ADMIN_USER}" "${TEST_ADMIN_PASSWORD}")

# 2. 找到或创建与 Manager 的 DM Room
DM_ROOM=$(matrix_find_dm_room "${ADMIN_TOKEN}" "@manager:...")

# 3. 等待 Manager Agent 就绪
wait_for_manager_agent_ready 300 "${DM_ROOM}" "${ADMIN_TOKEN}"

# 4. 等待 Manager 处理完之前的消息
wait_for_session_stable 5 60

# 5. 发送创建 Worker 请求
matrix_send_message "${ADMIN_TOKEN}" "${DM_ROOM}" \
    "Please create a new Worker for frontend development tasks.
     The worker's name (username) must be exactly 'alice'.
     She should have access to GitHub MCP."

# 6. 等待 Manager 回复（最多 5 分钟）
REPLY=$(matrix_wait_for_reply "${ADMIN_TOKEN}" "${DM_ROOM}" "@manager" 300)

# 7. 验证基础设施
- Matrix 用户 alice 已注册
- Higress consumer worker-alice 存在
- MinIO agents/alice/SOUL.md 存在
- MinIO agents/alice/openclaw.json 存在

# 8. 收集性能指标
METRICS=$(collect_delta_metrics "02-create-worker" "$METRICS_BASELINE")
```

**验证点**:
- Manager 回复包含 "alice"
- Matrix 用户创建成功
- Higress consumer 创建成功
- MinIO 配置文件存在
- Worker 容器启动（可选）

### 3.3 test-18-team-config-verify.sh (Team 配置验证)

**测试目标**: 验证 Team 创建后的配置正确性

**测试流程**:
```bash
# 1. 准备 SOUL.md 文件
mkdir -p /root/hiclaw-fs/agents/{leader,worker1,worker2}
# 写入各自的 SOUL.md

# 2. 调用 create-team.sh
bash /opt/hiclaw/agent/skills/team-management/scripts/create-team.sh \
  --name test-team \
  --leader test-leader \
  --workers test-worker1,test-worker2 \
  --team-admin admin

# 3. 验证 Team CR 状态
TEAM_STATUS=$(curl GET /api/v1/teams/test-team)
assert_eq "Active" "$(echo $TEAM_STATUS | jq -r '.phase')"

# 4. 验证 Team Room
TEAM_ROOM_ID=$(echo $TEAM_STATUS | jq -r '.teamRoomID')
assert_not_empty "${TEAM_ROOM_ID}"

# 5. 验证 Leader DM Room
LEADER_DM_ROOM_ID=$(echo $TEAM_STATUS | jq -r '.leaderDMRoomID')
assert_not_empty "${LEADER_DM_ROOM_ID}"

# 6. 验证 Worker CRs
for worker in test-leader test-worker1 test-worker2; do
    WORKER_STATUS=$(curl GET /api/v1/workers/${worker})
    assert_eq "Running" "$(echo $WORKER_STATUS | jq -r '.phase')"
done

# 7. 验证 ChannelPolicy
# Leader 应该能与所有 Workers 通信
# Workers 应该能与 Leader 通信
# Team Admin 应该在所有房间中

# 8. 验证 MinIO 配置
# Leader 的 AGENTS.md 应该包含 team-context
# Workers 的 AGENTS.md 应该包含 team-leader 信息
```

### 3.4 test-19-human-and-team-admin.sh (Team Admin 测试)

**测试目标**: 验证 Team Admin 的权限和通信

**测试流程**:
```bash
# 1. 创建 Human 用户作为 Team Admin
hiclaw create human --name team-admin-bob --display-name "Bob"

# 2. 创建 Team，指定 Team Admin
bash create-team.sh \
  --name admin-team \
  --leader admin-leader \
  --workers admin-worker1 \
  --team-admin team-admin-bob

# 3. 验证 Team Admin 在 Team Room 中
TEAM_ROOM_MEMBERS=$(matrix_get_room_members "${TEAM_ROOM_ID}")
assert_contains "${TEAM_ROOM_MEMBERS}" "@team-admin-bob:"

# 4. 验证 Team Admin 在 Leader DM Room 中
LEADER_DM_MEMBERS=$(matrix_get_room_members "${LEADER_DM_ROOM_ID}")
assert_contains "${LEADER_DM_MEMBERS}" "@team-admin-bob:"

# 5. 验证 Team Admin 的权限
# Team Admin 应该有 power level 100
POWER_LEVELS=$(matrix_get_power_levels "${TEAM_ROOM_ID}")
assert_eq "100" "$(echo $POWER_LEVELS | jq -r '.users["@team-admin-bob:..."]')"

# 6. 测试 Team Admin 与 Leader 通信
# Team Admin 发送消息到 Leader DM
# Leader 应该能收到并回复
```

---

## 4. 测试环境配置

### 4.1 环境变量

测试脚本从以下位置读取配置：

```bash
# 1. 从 ~/hiclaw-manager.env 加载（install 脚本生成）
HICLAW_ADMIN_USER=admin
HICLAW_ADMIN_PASSWORD=admin293cae24adb0
HICLAW_MATRIX_DOMAIN=matrix-local.hiclaw.io:18080
HICLAW_PORT_GATEWAY=18080
HICLAW_PORT_CONSOLE=18001
HICLAW_PORT_ELEMENT_WEB=18088
HICLAW_LLM_API_KEY=sk-xxx

# 2. 从容器环境变量检测（自动）
TEST_CONTROLLER_CONTAINER=hiclaw-controller
TEST_AGENT_CONTAINER=hiclaw-manager
TEST_GATEWAY_PORT=18080
TEST_CONSOLE_PORT=18001
TEST_ELEMENT_PORT=18088
```

### 4.2 容器检测逻辑

```bash
# test-helpers.sh 自动检测容器
if [ -z "${TEST_CONTROLLER_CONTAINER}" ]; then
    export TEST_CONTROLLER_CONTAINER="$(docker ps --format '{{.Names}}' | grep -E '^hiclaw-controller$')"
fi

if [ -z "${TEST_AGENT_CONTAINER}" ]; then
    export TEST_AGENT_CONTAINER="$(docker ps --format '{{.Names}}' | grep -E '^hiclaw-manager(-|$)')"
    export TEST_AGENT_CONTAINER="${TEST_AGENT_CONTAINER:-${TEST_CONTROLLER_CONTAINER}}"
fi
```

### 4.3 端口映射

Embedded 模式的端口映射：

| 服务 | 容器内端口 | 宿主机端口 | 说明 |
|------|-----------|-----------|------|
| Higress Gateway | 8080 | 18080 | AI Gateway + Matrix 路由 |
| Higress Console | 8001 | 18001 | Higress 管理控制台 |
| Element Web | 8088 | 18088 | Matrix Web UI |
| Manager API | 18799 | 18799 | Manager CoPaw Web Console |
| Tuwunel Matrix | 6167 | - | 内部访问（通过 docker exec） |
| MinIO API | 9000 | - | 内部访问（通过 docker exec） |

---

## 5. 针对 Team Management 的测试

### 5.1 测试 create-team.sh 新版本

创建测试脚本 `test-21-create-team-api.sh`:

```bash
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/test-helpers.sh"

test_setup "21-create-team-api"

log_section "Prepare SOUL.md files"

# 1. 创建 SOUL.md 文件
for agent in team-alpha-leader team-alpha-worker1 team-alpha-worker2; do
    AGENT_DIR="/root/hiclaw-fs/agents/${agent}"
    exec_in_agent mkdir -p "${AGENT_DIR}"

    SOUL_CONTENT="# ${agent}

## Role
- Name: ${agent}
- Team: team-alpha

## Behavior
- Follow team leader's instructions
- Report progress regularly
"
    echo "${SOUL_CONTENT}" | exec_in_agent tee "${AGENT_DIR}/SOUL.md" > /dev/null
done

log_section "Create Team via create-team.sh"

# 2. 调用 create-team.sh（新版本，使用 Controller API）
RESULT=$(exec_in_agent bash /opt/hiclaw/agent/skills/team-management/scripts/create-team.sh \
    --name team-alpha \
    --leader team-alpha-leader \
    --workers team-alpha-worker1,team-alpha-worker2 \
    --leader-model qwen3.5-plus \
    --worker-models qwen3.5-plus,qwen3.5-plus \
    --team-admin admin \
    --description "Test team for API validation" 2>&1)

log_info "create-team.sh output: ${RESULT}"

# 3. 解析结果
TEAM_ROOM_ID=$(echo "${RESULT}" | grep -A 20 "---RESULT---" | jq -r '.team_room_id // empty')
LEADER_DM_ROOM_ID=$(echo "${RESULT}" | grep -A 20 "---RESULT---" | jq -r '.leader_dm_room_id // empty')

assert_not_empty "${TEAM_ROOM_ID}" "Team Room ID returned"
assert_not_empty "${LEADER_DM_ROOM_ID}" "Leader DM Room ID returned"

log_section "Verify Team CR Status"

# 4. 验证 Team CR
CONTROLLER_URL="http://hiclaw-controller:8090"
GATEWAY_KEY=$(exec_in_agent printenv HICLAW_MANAGER_GATEWAY_KEY)

TEAM_STATUS=$(exec_in_agent curl -sf -X GET "${CONTROLLER_URL}/api/v1/teams/team-alpha" \
    -H "Authorization: Bearer ${GATEWAY_KEY}" 2>/dev/null)

PHASE=$(echo "${TEAM_STATUS}" | jq -r '.phase // "Unknown"')
assert_eq "Active" "${PHASE}" "Team phase is Active"

TEAM_ROOM_ID_FROM_API=$(echo "${TEAM_STATUS}" | jq -r '.teamRoomID // ""')
assert_eq "${TEAM_ROOM_ID}" "${TEAM_ROOM_ID_FROM_API}" "Team Room ID matches"

LEADER_DM_ROOM_ID_FROM_API=$(echo "${TEAM_STATUS}" | jq -r '.leaderDMRoomID // ""')
assert_eq "${LEADER_DM_ROOM_ID}" "${LEADER_DM_ROOM_ID_FROM_API}" "Leader DM Room ID matches"

log_section "Verify Worker CRs"

# 5. 验证 Worker CRs
for worker in team-alpha-leader team-alpha-worker1 team-alpha-worker2; do
    WORKER_STATUS=$(exec_in_agent curl -sf -X GET "${CONTROLLER_URL}/api/v1/workers/${worker}" \
        -H "Authorization: Bearer ${GATEWAY_KEY}" 2>/dev/null)

    WORKER_PHASE=$(echo "${WORKER_STATUS}" | jq -r '.phase // "Unknown"')
    log_info "Worker ${worker} phase: ${WORKER_PHASE}"

    # Worker 可能还在启动中，只要不是 Failed 就算通过
    if [ "${WORKER_PHASE}" != "Failed" ]; then
        log_pass "Worker ${worker} is not Failed (phase: ${WORKER_PHASE})"
    else
        log_fail "Worker ${worker} is Failed"
    fi
done

log_section "Verify Matrix Rooms"

# 6. 验证 Matrix Rooms 存在
ADMIN_LOGIN=$(matrix_login "${TEST_ADMIN_USER}" "${TEST_ADMIN_PASSWORD}")
ADMIN_TOKEN=$(echo "${ADMIN_LOGIN}" | jq -r '.access_token')

# 验证 Team Room
TEAM_ROOM_INFO=$(exec_in_manager curl -sf -X GET \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "http://127.0.0.1:6167/_matrix/client/v3/rooms/${TEAM_ROOM_ID}/state" 2>/dev/null)

assert_not_empty "${TEAM_ROOM_INFO}" "Team Room exists in Matrix"

# 验证 Leader DM Room
LEADER_DM_INFO=$(exec_in_manager curl -sf -X GET \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    "http://127.0.0.1:6167/_matrix/client/v3/rooms/${LEADER_DM_ROOM_ID}/state" 2>/dev/null)

assert_not_empty "${LEADER_DM_INFO}" "Leader DM Room exists in Matrix"

test_teardown "21-create-team-api"
test_summary
```

### 5.2 运行 Team 测试

```bash
# 1. 确保环境已安装
make install-embedded HICLAW_MANAGER_RUNTIME=copaw

# 2. 运行 Team 测试
./tests/test-21-create-team-api.sh

# 3. 或者通过 Makefile
make test SKIP_INSTALL=1 TEST_FILTER="21"
```

---

## 6. 调试技巧

### 6.1 查看容器日志

```bash
# Controller 日志
docker logs hiclaw-controller

# Manager 日志
docker logs hiclaw-manager

# Worker 日志
docker logs hiclaw-worker-alice

# 实时跟踪
docker logs -f hiclaw-controller
```

### 6.2 进入容器调试

```bash
# 进入 Controller 容器
docker exec -it hiclaw-controller bash

# 进入 Manager 容器
docker exec -it hiclaw-manager bash

# 查看 Matrix 消息
docker exec hiclaw-controller curl -sf http://127.0.0.1:6167/_matrix/client/versions
```

### 6.3 检查 API 状态

```bash
# 检查 Controller API
curl http://127.0.0.1:8090/healthz

# 查看 Team 状态
curl http://127.0.0.1:8090/api/v1/teams/team-alpha \
  -H "Authorization: Bearer ${GATEWAY_KEY}"

# 查看 Worker 状态
curl http://127.0.0.1:8090/api/v1/workers/alice \
  -H "Authorization: Bearer ${GATEWAY_KEY}"
```

### 6.4 查看 MinIO 文件

```bash
# 进入 Controller 容器
docker exec -it hiclaw-controller bash

# 列出 Team 配置
mc ls hiclaw/hiclaw-storage/teams/team-alpha/

# 查看 Leader AGENTS.md
mc cat hiclaw/hiclaw-storage/agents/team-alpha-leader/AGENTS.md
```

---

## 7. 常见问题

### 7.1 测试失败：Manager 不响应

**原因**: Manager Agent 可能还在初始化

**解决**:
```bash
# 检查 Manager 进程
docker exec hiclaw-manager ps aux | grep -E "(openclaw|copaw)"

# 检查 Manager 日志
docker logs hiclaw-manager | tail -50

# 等待更长时间
wait_for_manager_agent_ready 600  # 10 分钟
```

### 7.2 测试失败：Worker 容器未启动

**原因**: Docker Backend 可能遇到问题

**解决**:
```bash
# 检查 Controller 日志
docker logs hiclaw-controller | grep -i error

# 检查 Docker Socket 挂载
docker inspect hiclaw-controller | grep -A 5 Mounts

# 手动测试 Docker 访问
docker exec hiclaw-controller docker ps
```

### 7.3 测试失败：Team Room ID 为空

**原因**: TeamReconciler 可能还在处理

**解决**:
```bash
# 等待更长时间
sleep 30

# 检查 Team CR 状态
curl http://127.0.0.1:8090/api/v1/teams/team-alpha

# 检查 Controller 日志
docker logs hiclaw-controller | grep -i "team-alpha"
```

---

## 8. 持续集成

### 8.1 GitHub Actions 集成

```yaml
name: Integration Tests

on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Build images
        run: make build

      - name: Run integration tests
        run: make test
        env:
          HICLAW_LLM_API_KEY: ${{ secrets.LLM_API_KEY }}

      - name: Upload test results
        if: always()
        uses: actions/upload-artifact@v3
        with:
          name: test-results
          path: tests/output/
```

### 8.2 本地 CI 模拟

```bash
# 完整 CI 流程
make clean
make build
make test

# 快速验证
make build
make test SKIP_INSTALL=1 TEST_FILTER="01 02 21"
```

---

## 9. 总结

Embedded 模式的集成测试流程：

1. **环境准备**: `make install-embedded`
2. **运行测试**: `make test SKIP_INSTALL=1`
3. **验证结果**: 检查测试输出和日志
4. **调试问题**: 使用 docker logs 和 curl 检查状态
5. **清理环境**: `make uninstall-embedded`

关键测试点：
- ✅ 基础设施健康（test-01）
- ✅ Worker 创建（test-02）
- ✅ Team 创建（test-21，新增）
- ✅ Team 配置验证（test-18）
- ✅ Team Admin 权限（test-19）

新的 `create-team.sh` 脚本通过 Controller API 创建 Team，测试需要验证：
- Team CR 状态正确
- Team Room 和 Leader DM Room 创建成功
- Worker CRs 创建成功
- ChannelPolicy 配置正确
- MinIO 配置文件完整
