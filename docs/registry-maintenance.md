# Registry 运维台账

单一事实源：当前生产运行什么、哪条谱系出的、历史怎么走到这里。
任何发布/换版前，先核本文件 §1，再核 `git log origin/main`（D4 教训：台账与远端都要看）。

## §1 当前生产运行（2026-10-13 fd-1.3.0 起）

| 组件 | 镜像 tag | 谱系 | 状态 |
|---|---|---|---|
| registry 容器 | `ccr.ccs.tencentyun.com/yizuo/mcp-registry:sha-a95f7a0` | origin/main（fd-1.3.0） | healthy |
| auth-server 容器 | `ccr.ccs.tencentyun.com/yizuo/mcp-auth-server:sha-a95f7a0` | origin/main（fd-1.3.0） | healthy |
| mcpgw 容器 | `ccr.ccs.tencentyun.com/yizuo/mcpgw:1.32.0`（钉版，非我方构建；上游原版 `public.ecr.aws/p3v1o3c6/mcpgw` 已转存 ccr） | 上游 | healthy |

- 双容器自 fd-1.3.0 起**同源出片**（同一 commit、同一构建 matrix）。
- 2026-10-13 晚补正：fd-1.3.0 切换当时 registry 容器曾以 cheap-1 本地构建的
  `mcp-gateway-registry-registry:latest`（digest 131321e5，代码同源 48c1d23）顶班，
  当晚已 `docker compose up -d registry` 滚回 compose pin 的 ccr 同源镜像
  （digest e3af2230）——live / compose pin / 本台账三方一致。本地构建的
  latest 镜像留宿主机作应急回退，不再用作运行镜像。
- 栈运行目录：cheap-1 `/opt/mcp-gateway-registry`，compose 文件 = `docker-compose.prebuilt.yml`（本仓跟踪，随仓更新；镜像 pin 在内）。
- 运行时 nginx conf 由 /opt checkout **bind-mount**（`docker/nginx_rev_proxy_http_and_https.conf` + `docker/nginx_rev_proxy_http_only.conf`），与仓库内容字节一致；镜像内置 conf 仅作无挂载时的回退。
- 构建：GitHub Actions（org 账单阻断期走 scs001 fork 临时线，`image.yml` workflow_dispatch 到 `fd-*` 分支）→ hkccr → cheap-3 `tcr-relay-sync.sh` 回灌 ccr（手动单 tag：`tcr-relay-sync.sh <repo> <tag>`）。
- 测试门：scs001 分支名必须 `fd-*` 才触发 auth-server/registry/frontend 三套；**全绿才允许 dispatch 镜像**。

## §2 谱系地图（fd-1.3.0 归一后）

2026-10-13 前存在**三条互无共同祖先的线**（两次独立 hard-fork 造出），2026-10-13 `fd-1.3.0-reconcile` 把 wire 侧线载荷全部并回 origin/main：

- `origin/main`（FindDataTechnology/mcp-gateway-registry）：fd 瘦身谱系。fd-1.0.0…fd-1.2.2 的 registry 出片线；曾缺 backend-auth 生产者半与 quota-gate 演化。
- wire 侧线（`fd-quota-gate-scope`@3739eb7 / scs001 历史线）：自上游 1.32.0 二次独立成根，携带 tier/call-grant 栈、quota-gate 演化、backend-auth 移植对、caller-map。**fd-1.1.3 auth-server 出片线**；fd-1.3.0 后全部内容并入 main，侧线冻结。
- `gitee/main`：bind-mount 模板 + 旧部署线（含 942705ac 原修）。fd-1.3.0 起归一并冻结（内容已被 main 覆盖：confs 字节一致、原修被移植对取代）。

**新纪律：单一谱系**。所有改动进 origin/main（或其分支），双镜像同源构建；不再向侧线 cherry-pick。

## §3 发布史

| 版本 | commit | 内容 |
|---|---|---|
| fd-1.0.0 | 7fbc2d63 | 硬分叉独立谱系 + TCR 出片线 |
| fd-1.1.0–1.1.3 | …3739eb7 | wire 侧线（tier/quota/caller-map/backend-auth 移植对）；auth-server 用 |
| fd-1.2.0–1.2.2 | …ad118d4a | main 线（i18n/fastembed/card examples/skill 传播）；registry 用 |
| **fd-1.3.0** | **a95f7a01** | **谱系归一**：wire 载荷并回 main，双容器同源；修 business-mcp 401（backend-auth 生产者半缺位） |

## §4 关键机制备忘

- **backend-auth 注入链**：mcp_proxy（auth-server）→ `POST {egress_registry_internal_url}/_egress_internal/egress-token` → **registry 容器**执行 vend（`registry/api/egress_auth_routes.py` `_consent_with_backend_auth`）→ 响应携带 `backend_auth_header/value` → auth-server 消费注入（server.py ~L8944，日志 `injected shared backend auth credential`）。任一半缺位即上游 401——两容器必须同源。fd-1.3.0 生产实证：24h 窗口 `injected shared backend auth credential server=fd-find-data-business-mcp` 132 次；滚回 ccr 镜像后 vend 端点持续 200。
- compose env 白名单：wire 门控变量（FD_WIRE_*/CALL_GRANT_*/PREFLIGHT_*）写在 `docker-compose.prebuilt.yml` 的 environment 段，`.env` 提供值；**改 compose 时勿丢这批透传**。
- relay 的 seen-tags 去重：新 sha tag 通常被全量轮自动首见拷贝；紧急时手动 `ssh cheap3 /usr/local/bin/tcr-relay-sync.sh yizuo/<repo> <tag>`（注意 flock 并发跳过）。
- `/opt` checkout 有未跟踪运维文件（.admin-jwt* 等），切分支时保留即可。
