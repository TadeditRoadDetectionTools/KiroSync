# GitLab API 格式與功能對應

KiroSync 進度整合(`/changes`、`/summary`、`/usage`)所用到的 GitLab REST API 整理。
全部端點都已在本地 **GitLab CE 19.2.0**(`gitlab-test`,http://localhost:8929)以
reporter / admin token 實測(M1–M3,2026-07-19)。

## 認證與 token

所有請求帶 header:

```
PRIVATE-TOKEN: glpat-xxxx
```

(也支援 `Authorization: Bearer glpat-xxxx`;**不要**把 token 放 URL query。)

| 用途 | 需要的 scope | 持有者 | 存放 |
|---|---|---|---|
| 讀 commit / 分支 / 專案(日常查詢與 poller)| `read_api` + `read_repository` | reporter 服務帳號 | **只放 bot 主機**(比照 `BOT_TOKEN`)|
| 建 group、加成員(一次性初始化)| `api` | admin/owner | 只在初始化時使用 |

scope 不足時回 `403`,body:

```json
{"error":"insufficient_scope","error_description":"The request requires higher privileges…","scope":"api read_api"}
```

## 通用慣例

- **Base**:`{GITLAB_URL}/api/v4`
- **專案定位**:用數字 `id`,或 **URL-encode 的 full path**(`acme/platform/api` → `acme%2Fplatform%2Fapi`)。
- **404 的語意**:私有專案對「非成員」回 **404 而非 403**(實測)——bot 端不能把 404 當「專案不存在」,只能當「看不見」。
- **時間格式**:ISO 8601,回應為 UTC(`2026-07-18T23:52:09.742Z`);`since`/`last_activity_after` 參數同格式。
- **分頁**:`per_page`(≤100)+ `page`;回應 header `X-Next-Page`(空 = 最後一頁)、`X-Total`。poller 逐頁抓到空為止。

---

## 端點總表(按 KiroSync 功能)

| KiroSync 功能 | 端點 | 已驗 |
|---|---|---|
| `/changes` 探索層(哪些專案有動靜)| `GET /projects?last_activity_after=` | ✅ |
| `/changes` 分支層 + branch autocomplete | `GET /projects/:id/repository/branches` | ✅ |
| `/summary` 主資料(commit 彙總)| `GET /projects/:id/repository/commits` | ✅ |
| `/usage` 未 push 懶惰重查 | `GET /projects/:id/repository/commits/:sha` | ✅ |
| remote 正規化 key → 專案解析(`repos` 表)| `GET /projects/:url_encoded_path` | ✅ |
| 服務帳號初始化(加入所有頂層 group)| `GET /groups?top_level_only=true` + `POST /groups/:id/members` | ✅ |
| bot 健康檢查 / `/status` GitLab 行 | `GET /version`、`GET /user` | ✅ |

---

## 1. commit 列表 — `/summary` 主資料、poller 增量拉取

```
GET /projects/:id/repository/commits?ref_name=<branch>&since=<ISO>&per_page=100
```

| 參數 | 說明 |
|---|---|
| `ref_name` | 分支(或 tag);**不給 = 專案預設分支** → `/summary branch:` 直接對映 |
| `since` / `until` | 只取此時間之後/之前(ISO 8601)→ `/summary since:` 直接對映 |
| `author` | 依作者過濾(名或 email 開頭)|

回應(list)重點欄位 → **M4 `commits` 表**:

| API 欄位 | 型別/例 | 存進 `commits` 表 |
|---|---|---|
| `id` | 40-hex SHA | `sha`(每專案 UNIQUE)|
| `short_id` | `38bb649f` | —(顯示時由 sha 截)|
| `title` | commit 首行 | `title` |
| `message` | 完整訊息 | `message` |
| `author_name` / `author_email` | | `author_name` / `author_email` |
| `authored_date` / `committed_date` | ISO 8601 | `authored_at` / `committed_at` |
| `parent_ids` | list | —(判 merge:len>1)|
| `web_url` | 可點連結 | `web_url`(summary 輸出直接用)|

實測(reporter token,`ref_name` 過濾正確):feature-x 上的 commit 只出現在
`ref_name=feature-x`,不出現在 `ref_name=main`。

## 2. 單一 commit — 未 push 偵測

```
GET /projects/:id/repository/commits/:sha
```

| 結果 | 語意 |
|---|---|
| `200` | 該 SHA 已在 GitLab(已 push)|
| `404` | GitLab 沒有這個 SHA → **本機領先(未 push)** |

用法:client 快照帶來的 `head` SHA,bot 收快照時查一次寫入
`session_repos.head_in_gitlab`;`/usage` 輸出前對仍標未 push 的少數 HEAD **懶惰重查**,
避免「事後 push 了但旗標過期」的誤報。實測:本機 commit 未 push `404` → push 後 `200`。

## 3. 分支列表 — `/changes` 分支層、branch autocomplete 快取

```
GET /projects/:id/repository/branches
```

回應(list)重點欄位 → **M4 `branches` 表**:

| API 欄位 | 存進 `branches` 表 | 用途 |
|---|---|---|
| `name` | `name` | branch autocomplete 候選 |
| `default` | `is_default` | `/summary` 不給 branch 時的預設 |
| `commit.committed_date` | `last_commit_at` | 和 `since` 比對 → `/changes` 列「有動靜的分支」 |
| `commit.id` | —(可存供比對)| |

注意:autocomplete 回呼**只准查本地快取**(Discord 3 秒限時),此 API 由 poller 定期呼叫更新快取,不在 autocomplete 路徑上現打。

## 4. 專案探索 — `/changes` 探索層

```
GET /projects?last_activity_after=<ISO>&simple=true&per_page=100
```

- 只回「該 token 看得見」的專案(reporter 的可見範圍天然就是它加入的 group)。
- `simple=true` 減少 payload。

| API 欄位 | 用途 |
|---|---|
| `id` | 後續 commits/branches 呼叫 |
| `path_with_namespace` | 顯示名(`acme/platform/api`),與 remote 正規化 key 的 path 部分對映 |
| `last_activity_at` | `/changes` 排序 |

實測:push 後一小時內查 `last_activity_after=1h 前` 正確列出 3 個有動靜專案。
注意:`last_activity_at` 涵蓋**非 commit 活動**(issue、MR 等)也會更新,`/changes`
要以 branches/commits 的時間為準做第二層確認,`last_activity_after` 只當粗篩。

## 5. 專案解析 — remote 正規化 key → project id(`repos` 表)

```
GET /projects/:url_encoded_full_path        # 例: /projects/acme%2Fplatform%2Fapi
```

| API 欄位 | 存進 `repos` 表 |
|---|---|
| `id` | `gitlab_project_id` |
| `path_with_namespace` | `full_path` |
| `default_branch` | `default_branch` |

流程:client 快照帶 `remote` → bot 正規化(去協定/憑證/`.git` 尾碼、標準 port 去除)
→ 取 path 部分 URL-encode 查此端點 → 建立 `repos` 對應(`remote_norm` UNIQUE)。
`404` = reporter 看不見或非本 GitLab → 標 `external`/`unknown`,不擋對話同步。

⚠️ 同專案可能有多種 remote 寫法(http/ssh、含 port 與否)——`repos` 表要允許
多個 `remote_norm` 別名指向同一 `gitlab_project_id`。

## 6. group 與成員 — 服務帳號一次性初始化

```
GET  /groups?top_level_only=true&per_page=100     # 列所有頂層 group(要 admin token)
POST /groups/:id/members                          # body: user_id=<uid>&access_level=20
GET  /groups/:url_encoded_path                    # 例: /groups/acme%2Fplatform → id
```

- `access_level=20` = **Reporter**。
- **成員資格向下繼承**(實測):只加**頂層** group,subgroup 專案(`acme/platform/api`)自動可讀 —— 不用逐 subgroup/逐專案加。
- 成功回 `201`;已是成員回 `409`(初始化腳本要冪等處理)。

## 7. 雜項

```
GET /version     # {"version":"19.2.0", ...} — bot 開機健康檢查
GET /user        # token 自身身分 {"id":2,"username":"reporter1",...} — /status 顯示與開機自檢
```

---

## reporter 權限邊界(實測矩陣)

| 操作 | HTTP | 結論 |
|---|---|---|
| 讀所屬 group(含 subgroup)專案 commits/files | `200` | 讀取足夠 |
| 讀非成員私有專案 | `404` | 看不見(非 403)|
| 寫入(建 commit / push / 建專案)| `403` | 不能寫 ✓ 最小權限 |
| 管理端點(application/settings)| `403` | 非 admin ✓ |

## 對應的 M4 資料表(bot SQLite)

| 表 | 來源端點 | key |
|---|---|---|
| `repos` | §5 專案解析 | `remote_norm` UNIQUE → `gitlab_project_id` |
| `commits` | §1 commit 列表 | (`project_id`, `sha`) UNIQUE |
| `branches` | §3 分支列表 | (`project_id`, `name`) |
| `session_repos` | client 快照 header(remote/branch/head)+ §2 驗證 | `session_id` |
