# Proposal: Logto IAM Manager — 让 IAM（组/M2M）在 Logto 部署上可用

> 状态：**已验证可实施**（2026-09-18 spike 通过，见 §5.1）
> 适用：AUTH_PROVIDER=logto 的部署（finddatatech 生产）
> 上游：agentic-community/mcp-gateway-registry（本方案设计为可回馈上游的通用实现）

## 1. 背景与线上证据

Settings → IAM 在生产上整体不可用，两个独立故障（2026-09-18 实测）：

### 故障 A：组列表 502

```
GET /api/management/iam/groups → 502 "Unable to list IAM groups"
registry 日志:
  Failed to list IAM groups: Failed to authenticate with Keycloak:
  [Errno -2] Name or service not known
```

根因：`registry/utils/iam_manager.py` 的 `get_iam_manager()` 工厂只认
keycloak/entra/okta/auth0/pingfederate/cognito。`AUTH_PROVIDER=logto`
落入 else 分支，静默 fallback 到 `KeycloakIAMManager`，去连不存在的主机名
`keycloak`。组列表失败进一步卡死 M2M 创建表单（表单强制选组：
"At least one group is required"，组下拉来自该接口）。

### 故障 B：M2M 列表 500

```
GET /api/iam/m2m-clients → 500
registry 日志:
  2 validation errors for IdPM2MClient
  name: Field required / provider: Field required
```

根因（非配置问题）：Mongo `idp_m2m_clients` 集合（库名 `mcp_registry`，
认证 admin）中有一条**遗留脏文档**：

```js
{ client_id: 'mcpgatewaym2m', enabled: true, groups: ['registry-admins'] }   // 缺 name/provider → 列表必炸
{ client_id: 'v08nef4o852kgl2ih5xsq', name: 'mcp-gateway-m2m',
  description: 'Logto M2M app for registry management', groups: ['registry-admins'],
  enabled: true, provider: 'logto', idp_app_id: 'v08nef4o852kgl2ih5xsq', ... }  // 完整
```

第二条同时是重要资产：**Logto 侧已存在一个 M2M 应用**
（client_id `v08nef4o852kgl2ih5xsq`，2026-09-09 创建）——正是 Management API
接入所需的凭证载体。

## 2. 架构事实：组 ≡ Logto 角色

本 fork 的 `auth_server/providers/logto.py:176-186` 已规定语义：

```python
roles = claims.get("roles", [])   # Logto OIDC claim（scope 'all' 补丁）
... "groups": roles               # 直接映射为 JWT groups
```

即整个权限链是：

```
Logto 用户 ──被赋予──> Logto 角色 (mcp-registry-admin / legal / ...)
                          │ OIDC claim "roles"
                          v
            fork auth-server:  groups := roles
                          │  签发 JWT {groups:[...]}
          ┌───────────────┴────────────────┐
          v                                v
   registry: 组→scope 映射          paas 平台：登录读
   （server 访问权 / UI 权限）      organizations/organization_roles
```

因此 IAMManager 的「组」操作应映射到 **Logto Management API 的 roles**，
与现有 JWT 链条天然一致——管理员在 IAM 界面建的组会出现在 Logto，
Logto 里赋了角色的用户，其 JWT 自动带对应 groups。

## 3. 设计

### 3.1 IAMManager 协议 → Logto Management API 映射

| IAMManager 方法 | Logto API | 备注 |
|---|---|---|
| `list_groups()` | `GET /api/roles` | 返回 [{id, name, path, attributes:{description}}]（对齐 Keycloak 形状） |
| `group_exists(name)` | `GET /api/roles`（按 name 过滤） | |
| `create_group(name, desc)` | `POST /api/roles {name, description}` | |
| `update_group(name, desc)` | `PATCH /api/roles/{id}` | |
| `delete_group(name)` | `DELETE /api/roles/{id}` | ⚠️ 级联：从所有用户移除该角色 |
| `list_users(search)` | `GET /api/users?search=`（+每人 `GET /api/users/{id}/roles`） | |
| `create_human_user(...)` | `POST /api/users` + 角色分配 | |
| `delete_user(username)` | `DELETE /api/users/{id}` | |
| `update_user_groups(user, groups)` | 差量 `POST/DELETE /api/users/{id}/roles` | |
| `create_service_account(client_id, groups, desc)` | `POST /api/applications {type:"machine-to-machine"}` + 角色分配（第 2 步，可选） | |

### 3.2 管理 API 认证（已实测通过）

- 凭证：Logto M2M 应用 `mcp-gateway-m2m`（client_id `v08nef4o852kgl2ih5xsq`，
  Logto v1.42.0，secret 在 Logto 管理后台该应用详情页，勿入库）。
- 已完成的授权（2026-09-18，spike 中操作）：管理后台已将默认角色
  **"Logto Management API access"**（含 `all` 权限）分配给该 M2M 应用。
- 令牌配方（两个坑都已踩平）：

```
POST https://auth.finddatatech.cloud/oidc/token
  grant_type=client_credentials
  client_id / client_secret
  resource=https://default.logto.app/api
  scope=all                      # ← 必须！缺了必 403（即使角色已授）
→ Bearer → GET https://auth.finddatatech.cloud/api/{roles|users|applications/...}
```

- Manager 内部缓存 access token（expires_in 3600s），过期重取。

### 3.3 代码落点

```
registry/utils/iam_manager.py
  + class LogtoIAMManager           # 照 KeycloakIAMManager 的结构与返回形状
  + get_iam_manager(): elif provider == "logto": return LogtoIAMManager()
registry/utils/logto_admin.py      #（可选拆分）HTTP 客户端 + token 管理
.env / docker-compose.prebuilt.yml
  + LOGTO_MANAGEMENT_M2M_CLIENT_ID
  + LOGTO_MANAGEMENT_M2M_CLIENT_SECRET
  （LOGTO_ENDPOINT 已存在）
```

新 env 只进 auth-server 与 registry 两个服务的 environment 段。

## 4. 实施步骤（按依赖序）

**第 0 步 · 数据修复（5 分钟，无代码，先行解堵 M2M 列表）**

```js
// mongosh -u admin -p … --authenticationDatabase admin mcp_registry
db.idp_m2m_clients.updateOne(
  { client_id: "mcpgatewaym2m" },
  { $set: { name: "mcpgatewaym2m (legacy)", provider: "manual" } }
)
// 若确认该旧账号已废弃，亦可 deleteOne
```

**第 1 步 · LogtoIAMManager（核心，约半天~一天）**

新类 + 工厂分支 + env 接线 + 单测（上游 `tests/unit/api/test_management_routes.py`
有 mock IAM manager 的现成模式）。验证：Settings → IAM → Groups
列表/新建/删除全通；M2M 创建表单组下拉出现 Logto 角色。

**第 2 步 ·（可选）service_account 真实对接**

`create_service_account` 调 Logto 创建 M2M 应用并返回 secret。
非必须：手工注册的 manual M2M 客户端由 auth-server 自签 JWT，
token 链路已可用。

## 5. 验证清单

1. ~~**Spike**：client_credentials 换管理 API token~~ **✅ 已通过（2026-09-18）**：
   角色已授予（见 §3.2）、`scope=all` 缺一不可；`/api/roles`、`/api/users`、
   `/api/applications` 三端点均 200。roles 返回形状已确认：
   `{id, name, description, type, usersCount, applicationsCount, ...}`。
2. 修 B 后：`GET /api/iam/m2m-clients` 200，两条文档均渲染。
3. 修 A 后：groups CRUD 通；新建组 `legal` 后
   `GET /api/management/iam/groups` 含该组，且 Logto 后台出现同名角色。
4. 端到端：Logto 给测试用户赋角色 `legal` → 登录 registry → 签发 JWT
   含 `"groups":["legal"]` → registry 组→scope 生效。
5. 回归：paas 侧 `/api/extensions/market` 条目数不变（148 skills / 15 MCP）。

## 6. 风险与开放问题

- ~~[Management API 授权链未实测]~~ → 已实测通过（§5.1）。
- [角色返回形状差异] → Logto roles 的字段是 `{id, name, description, ...}`，
  而 registry 期望 Keycloak 形状 `{id, name, path, attributes}`——
  LogtoIAMManager 需做一层适配（path 用 `/{name}` 合成，description 塞
  attributes）。
- [删组级联] → registry 删组 = 删 Logto 角色（全员移除）。UI 文案可后补提示。
- [Logto 角色 vs Organization] → registry 链用 roles；paas 登录读
  organizations/organization_roles。两套并存可行；若未来要
  「一处配组、两边生效」，统一到单一概念是独立变更。
- [部署形态] → registry 镜像为本地构建后按 compose 引用 tag 贴名
  （`public.ecr.aws/p3v1o3c6/registry:latest` 实为 fork 代码），
  auth-server 为 `mcp-auth-server:logto` 本地构建。部署 = fork 构建
  新镜像 → 贴同名 tag → `docker compose -f docker-compose.prebuilt.yml
  up -d --no-deps auth-server registry`（沿用既有流程，实施时以
  `build_and_run.sh` 实际参数为准）。
- [上游回馈] → 实现保持 provider-generic（不绑定 finddatatech 私有配置），
  可作为第 7 个 IdP 提 PR 回上游，降低长期合并负担。

## 7. 回滚

- 第 0 步：`$unset` 恢复原字段（或该文档本就废弃）。
- 第 1 步：镜像回退旧 tag + `up -d --no-deps`；`.env.bak-ttl-*` 同目录有
  先例备份惯例。Logto 侧新建的角色留在租户内不影响旧链路（fallback 的
  Keycloak manager 本来就不可用）。
