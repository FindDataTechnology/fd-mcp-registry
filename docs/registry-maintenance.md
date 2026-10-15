# Registry 运维台账

单一事实源：当前生产运行什么、哪条谱系出的、历史怎么走到这里。
任何发布/换版前，先核本文件 §1，再核 `git log origin/main`（D4 教训：台账与远端都要看）。

## §1 当前生产运行（2026-10-13 晚起：k8s/GitOps 形态，onboard-registry-argocd 切流）

| 组件 | 形态 | 谱系 | 状态 |
|---|---|---|---|
| registry 容器 | cheap k3s `mcp-registry` ns，Deployment hostNetwork@cheap-8（100.64.0.15），镜像 `ccr.../yizuo/mcp-registry:sha-115fea3`（fd-1.4.0，随 origin/main 滚动），nginx listen 18080/18443（启动 sed，全节点宿主 8080 被 k3s 系统组件占用） | origin/main（fd-1.4.0） | healthy |
| auth-server 容器 | 同上拓扑，`ccr.../yizuo/mcp-auth-server:sha-689ec5e`（8888） | origin/main（fd-1.4.0） | healthy |
| mcpgw 容器 | 同上拓扑，`ccr.../yizuo/mcpgw:1.32.0`（8003，钉版上游） | 上游 | healthy |
| mongodb | 同上拓扑，Deployment+hostPath `/opt/mcp-registry-k8s/mongo`（1010MiB mongodump/restore 迁入，对账 servers 14/14） | mongo:8.2 | healthy |
| openbao | **留 cheap-1 compose**（密钥库，k8s 经 tailnet 100.64.0.11:8200 直连；迁移另立项） | 上游 | healthy |

- **滚动=改 manifest**：gitee fd-infra-deploy `mcp-registry/prod/` → ArgoCD app `mcp-registry`（selfHeal）自动 sync。Secret 外建不进仓（mcp-registry-env/-env-auth/-mongo-keyfile + tcr-ccr，ns mcp-registry）。
- **入口链**：`mcp.finddatatech.cloud`（http:8080 与 https:443 同路）→ 雷池 tengine(443)→宿主 Caddy :8080 mcp 站点块→`100.64.0.15:18080`（Caddyfile 三处 reverse_proxy，2026-10-13 晚切）。Caddy SSE 参数（flush_interval -1/900s）与 mint 预检特判块原样保留。
- **回退**：Caddyfile 三处 upstream 改回 `127.0.0.1:18080`+reload（秒级）——compose 双轨栈冻结在 sha-a95f7a0（stop 前保留 ≥1 天观察期；观察期内 compose 侧只读）。数据回退=观察窗内 k8s mongo 新写入反向 dump 回灌。
- models 262MiB 在 cheap-8 hostPath `/opt/mcp-registry-k8s/models`（rsync 自 cheap-1）。
- 历史（compose 时代，2026-10-13 凌晨~晚）：fd-1.3.0 切换当时 registry 容器曾以 cheap-1 本地构建 `latest`（digest 131321e5）顶班，当晚滚回 ccr pin（e3af2230）后随即整体迁 k8s。
- **双轨期两个来回（2026-10-15 核账实录）**：10-13 19:19 切 k8s → 10-14 03:23 被切回 compose（同期 cheap-1 盘满、nginx 模板 reload 失败）→ 10-15 05:53 切回 k8s 并滚 fd-1.4.0（双宿主收口归 add-packs-product-home 4.3）。**回切造成两库分叉**：compose 服务期新增的 `fd-paper-trading`/`vaas-wanxing` 等 28 条注册记录 + 20 条扫描记录没同步，生产 `/fd-paper-trading/mcp` 曾 405；已于 10-15 以 mongoexport→mongoimport `--mode merge` 补齐，双向对账 compose→k8s 差集=0。
- **回退配方补强**：Caddy 三处 upstream 改回 compose 是秒级，但**回切前必须先把 k8s 侧新增数据反向 dump 回灌 compose**（双轨=两个可写库，回切即分叉；症状温和=405 不是 5xx，不易察觉）。
- **compose 栈现状**：registry/auth-server 仍在 cheap-1 运行（sha-adeac80/d82ffed），**不能停**——wire cell 条目的上游是 docker 网络名，k8s 侧解析不了（见下）。
- **已知挂账 → 已修（2026-10-15 晚）**：wire cell 代理路径在 k8s 侧 403（`fd-wire-cell-drillcust001`、`fd-wire-cell-pgauto001`，另有 eco-time/fetch 两个 docker 网内上游）。根因=条目 `proxy_pass_url` 为 docker 网络名，k8s 侧 DNS 解析失败 → 健康检查判 `UNHEALTHY_URL_BLOCKED` → nginx 注释掉该 cell location → 通配 403。**修复=socat 中继**：cheap-1 起 4 个 `alpine/socat` 容器（`cell-relay-drill/pgauto/ecotime/ecofetch`，`--network mcp-gateway-registry_default` 解析容器名，`-p 100.64.0.11:1800{1,2,3,4}` 发布 tailnet，`--restart unless-stopped`），k8s 侧 4 条目 `proxy_pass_url` 改指 `http://100.64.0.11:1800x/...`（mongo updateOne，**仅 k8s 侧**；compose 侧保持容器名零改动）。nginx 重渲染后 location 复活；生产实证：403→401、wire 自然流量 30 次 mcp_proxy 真走 k8s、**business-mcp 注入链首现**（`injected shared backend auth credential server=fd-find-data-business-mcp` + vend 端点 200）。防回切污染：若再回切 compose，只需把 4 条 `proxy_pass_url` 改回容器名（compose 侧本来就是）。
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
| **k8s 迁移** | 同 a95f7a0 | 2026-10-13 晚 onboard-registry-argocd：四工作负载迁 cheap k3s（ArgoCD app mcp-registry），Caddy 三处切流；compose 双轨冻结（回退位）；openbao 留守 |

## §4 关键机制备忘

- **backend-auth 注入链**：mcp_proxy（auth-server）→ `POST {egress_registry_internal_url}/_egress_internal/egress-token` → **registry 容器**执行 vend（`registry/api/egress_auth_routes.py` `_consent_with_backend_auth`）→ 响应携带 `backend_auth_header/value` → auth-server 消费注入（server.py ~L8944，日志 `injected shared backend auth credential`）。任一半缺位即上游 401——两容器必须同源。fd-1.3.0 生产实证：24h 窗口 `injected shared backend auth credential server=fd-find-data-business-mcp` 132 次；滚回 ccr 镜像后 vend 端点持续 200。
- compose env 白名单：wire 门控变量（FD_WIRE_*/CALL_GRANT_*/PREFLIGHT_*）写在 `docker-compose.prebuilt.yml` 的 environment 段，`.env` 提供值；**改 compose 时勿丢这批透传**。
- relay 的 seen-tags 去重：新 sha tag 通常被全量轮自动首见拷贝；紧急时手动 `ssh cheap3 /usr/local/bin/tcr-relay-sync.sh yizuo/<repo> <tag>`（注意 flock 并发跳过）。
- `/opt` checkout 有未跟踪运维文件（.admin-jwt* 等），切分支时保留即可。
