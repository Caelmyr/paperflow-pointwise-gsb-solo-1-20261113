# PaperFlow 本地接口

默认基址 `http://127.0.0.1:8888/api`。实际端口以本次启动配置为准。JSON 使用UTF-8。所有写请求必须带 `X-PaperFlow: 1`，JSON 写请求带 `Content-Type: application/json`。浏览器跨站写入被拒绝。无付费服务、外部账号或认证依赖。

错误：`{"error":"中文说明","code":"invalid_request"}`，状态400参数无效、403请求来源/校验头、404不存在、409版本/状态冲突、413文件过大、429队列已满、503 OCR不可用。批量导入有失败时状态207并返回 `failures`，已成功文件保留。各操作 JSON 修改成功才返回成功结果。

## 核心流程示例

```sh
curl http://127.0.0.1:8888/api/workspace
curl -X POST http://127.0.0.1:8888/api/collections -H 'X-PaperFlow: 1' -H 'Content-Type: application/json' -d '{"name":"研究资料","category":"工作"}'
```

用返回的集合ID创建文档：

```json
POST /documents
{"collection_id":"collection_实际ID","name":"会议扫描件","category":"会议"}
```

上传：`POST /documents/{id}/import`，multipart `files` 可以重复；可选 form `position` 是从0开始的插入索引。返回 page_ids、failures、document。源文件完整保留，页面生成缩略图。

```json
POST /pages/{id}/process
{"operation":"illumination","params":{"strength":0.7},"revision":0}
```

revision 可选，不匹配返回409。操作包括 perspective / rotate / deskew / illumination / color / denoise / crop / enhance 及高级滤镜。完整参数模式在 `GET /operations`，每个参数包含类型、默认值、范围、枚举及奇数约束。

```json
POST /pages/{id}/process
{"operation":"perspective","params":{"points":[[10,20],[1300,20],[1300,1800],[10,1800]]}}
```

角点单位为当前图像像素。纸张必须为凸四边形。选区格式 `[x,y,width,height]`，最小32×32。

```json
POST /pages/{id}/ocr
{"language":"chi_sim+eng","psm":3,"minimum":0}
```

识别返回页面，其 `ocr` 含text、boxes、confidence、language、stale、edited。每个文字框有id、text、confidence、x、y、width、height和line。

```json
PATCH /pages/{id}/ocr
{"text":"人工校订文本 Hello PaperFlow"}
```

单词编辑用 `{"boxes":[{"id":"word_实际ID","text":"修改文字"}]}`。整段修改生成文本覆盖层；单词编辑保持原坐标。

```json
POST /tasks
{"page_ids":["page_实际ID"],"operation":"ocr","params":{"language":"chi_sim+eng"}}
```

返回202与任务id。`GET /tasks/{id}` 查询。`POST /tasks/{id}/cancel` 逐页取消，`POST /tasks/{id}/retry` 重试失败和未完成页。单页失败保存在results，其他页继续；done/total/progress/succeeded/failed均真实持久化。

```json
POST /documents/{id}/export
{"kind":"searchable_pdf","options":{"dpi":150}}
```

kind 为images/txt/pdf/searchable_pdf/zip；options 可传page_ids（指定导出顺序）、dpi、image_format、quality、include_originals。结果返回导出id，`GET /exports/{id}/download` 下载；`GET /exports/{id}/verify` 校验。可搜索PDF拒绝缺失或过期OCR。

## 文档编排

- `POST /documents/{id}/reorder`：page_ids 必须完整且不重复。
- `POST /pages/copy`：page_ids、target_id、可选position，复制图像及OCR记录。
- `POST /pages/move`：page_ids、target_id、可选position，移动关联。
- `POST /pages/delete`：page_ids，删除记录，二进制原文件保留。
- `POST /documents/{id}/split`：page_ids、name，按源页序拆分。
- `POST /documents/merge`：document_ids（顺序有效）、name，可选collection_id、consume。默认复制合并；consume=true移动并移除源文档记录。
- `POST /documents/{id}/blank`：name、width、height、position，插入空白页。
- `POST /pages/{id}/split-spread`：axis、ratio、overlap，拆开跨页扫描。
- `POST /pages/concatenate`：page_ids、target_id、orientation、gap，拼接页面。

## 双面扫描配对工作台

用于两次扫描的双面资料：背面可能倒置进纸，并混入缺扫、重扫与补拍单页。配对会话独立保存在工作区 `pairing_sessions` 中，全程不改动原文档页序与原图；只有显式 finalize 才复制生成新文档。

- `POST /documents/{id}/pairing`：为文档建立配对会话，立即分析批次、重复页、空白页与配对建议。
- `GET /pairing`：会话列表；`GET /pairing/{session_id}`：会话完整视图（含分析、确认配对、忽略清单、就绪度）。
- `POST /pairing/{session_id}/analyze`：重新计算建议。
- `GET /pairing/{session_id}/candidates/{page_id}`：某页的候选配对（含补拍页），返回相似度、建议旋转与占用状态。
- `POST /pairing/{session_id}/confirm-preview`：确认前的影响分析，返回会新增/移除/改动哪些建议、是否替换既有配对、未配对页数变化。
- `POST /pairing/{session_id}/confirm`：确认一对正反页，参数 `front_id`、`back_id`、`turn`（0/1/2/3，2 表示 180° 倒置转正）、`anchor`。锚点确认后其余建议按锚点重新对齐。
- `POST /pairing/{session_id}/unconfirm`：撤销配对，返回受影响建议。
- `POST /pairing/{session_id}/turn`：调整某确认配对的背面旋转。
- `POST /pairing/{session_id}/ignore` / `POST /pairing/{session_id}/keep`：忽略或保留页面。疑似空白页忽略时必须传 `acknowledge_blank:true`，系统不会自动删除任何页面。
- `POST /pairing/{session_id}/roles`：调整批次角色（front/back/supplement），同时只能有一个正面、一个背面批次。
- `GET /pairing/{session_id}/readiness`：完成度检查；仍有未配对/未忽略页、或空白页未显式确认时不可生成。
- `POST /pairing/{session_id}/finalize`：校验通过后复制页面生成阅读顺序正确的新文档（正反交错、倒置背面在副本上转正），原扫描批次、原图与各页校订独立保留。
- `POST /pairing/{session_id}/reopen`：复制为新会话继续调整，已确认配对完整继承。
- `DELETE /pairing/{session_id}`：删除配对工作记录，不影响任何扫描页或已生成文档。
- `POST /demo/duplex`：载入含倒置背面、缺扫、重扫、空白页与补拍页的双面练习资料。

相似度基于 SIFT 关键点的 RANSAC 空间一致性（内点落格分散度）结合感知哈希，对重扫噪声稳健；描述子以二进制存于 `paircache` 目录，不写入工作区 JSON。

## 区域、历史、快照

`POST /pages/{id}/regions` 参数name、kind（text/illustration/other）、region。返回区域id，下载 `GET /pages/{id}/regions/{region_id}`。`POST /pages/{id}/restore` 参数空对象恢复原图，或传history_id恢复该版本。`GET /pages/{id}` 返回完整图像与OCR历史。

`POST /documents/{id}/checkpoints` 保存name命名快照；`GET /documents/{id}/checkpoints` 列表；`GET /checkpoints/{id}/diff` 差异；`POST /checkpoints/{id}/restore` 恢复为新文档（可选name、collection_id）。

## 其他服务

质量分析 `/pages/{id}/analysis`，版面 `/layout`，处理前后差异 `/comparison`，建议 `/recommendations`；元数据 `/documents/{id}/metadata`；检索分面 `/catalog/facets`；POST `/catalog/query` 支持分类、标签、作者、集合、OCR、分页排序；复核队列 `/review/queue`；低置信度 `/pages/{id}/low-confidence`；页面批注 `/notes`；工作流 `/pipelines` 与 `/pipeline-runs`；导出预检 `/documents/{id}/preflight`；完整性 `/audit/integrity?deep=1`；空间 `/audit/storage`；来源 `/sources`；备份 `/backups`；归档导入 multipart `/archives/import`（file、collection_id）；用户设置 `/preferences`；会话 `/session`。

所有已注册接口完整列表见下方（由运行中的同版本 Flask 路由表生成）。

| 方法 | 路径 |
|---|---|
| POST | `/api/archives/import` |
| POST | `/api/archives/preview` |
| POST | `/api/audit/cleanup` |
| GET | `/api/audit/duplicates` |
| POST | `/api/audit/events` |
| GET | `/api/audit/integrity` |
| POST | `/api/audit/orphans` |
| GET | `/api/audit/storage` |
| GET | `/api/audit/summary` |
| POST | `/api/backups` |
| GET | `/api/backups` |
| GET | `/api/backups/<identifier>/download` |
| GET | `/api/backups/<identifier>/verify` |
| POST | `/api/catalog/batch` |
| GET | `/api/catalog/facets` |
| POST | `/api/catalog/query` |
| DELETE | `/api/checkpoints/<identifier>` |
| GET | `/api/checkpoints/<identifier>/diff` |
| POST | `/api/checkpoints/<identifier>/restore` |
| POST | `/api/collections` |
| PATCH | `/api/collections/<identifier>` |
| DELETE | `/api/collections/<identifier>` |
| POST | `/api/demo` |
| POST | `/api/documents` |
| POST | `/api/documents/<document_id>/blank` |
| GET | `/api/documents/<document_id>/bookmarks` |
| POST | `/api/documents/<document_id>/bookmarks` |
| DELETE | `/api/documents/<document_id>/bookmarks/<bookmark_id>` |
| POST | `/api/documents/<document_id>/checkpoints` |
| GET | `/api/documents/<document_id>/checkpoints` |
| POST | `/api/documents/<document_id>/contact-sheet` |
| GET | `/api/documents/<document_id>/export-history` |
| POST | `/api/documents/<document_id>/interchange` |
| PATCH | `/api/documents/<document_id>/metadata` |
| GET | `/api/documents/<document_id>/preflight` |
| POST | `/api/documents/<document_id>/replace` |
| POST | `/api/documents/<document_id>/replace-preview` |
| GET | `/api/documents/<document_id>/report` |
| POST | `/api/documents/<document_id>/report` |
| GET | `/api/documents/<document_id>/suggest-name` |
| GET | `/api/documents/<document_id>/text-statistics` |
| GET | `/api/demo/recovery` 见 POST |
| POST | `/api/demo/duplex` |
| POST | `/api/demo/recovery` |
| GET | `/api/documents` 见集合 |
| POST | `/api/documents` |
| POST | `/api/documents/<document_id>/pairing` |
| GET | `/api/pairing` |
| GET | `/api/pairing/<session_id>` |
| POST | `/api/pairing/<session_id>/analyze` |
| GET | `/api/pairing/<session_id>/candidates/<page_id>` |
| POST | `/api/pairing/<session_id>/confirm-preview` |
| POST | `/api/pairing/<session_id>/confirm` |
| POST | `/api/pairing/<session_id>/unconfirm` |
| POST | `/api/pairing/<session_id>/turn` |
| POST | `/api/pairing/<session_id>/ignore` |
| POST | `/api/pairing/<session_id>/keep` |
| POST | `/api/pairing/<session_id>/roles` |
| GET | `/api/pairing/<session_id>/readiness` |
| POST | `/api/pairing/<session_id>/finalize` |
| POST | `/api/pairing/<session_id>/reopen` |
| DELETE | `/api/pairing/<session_id>` |
| PATCH | `/api/documents/<identifier>` |
| DELETE | `/api/documents/<identifier>` |
| POST | `/api/documents/<identifier>/export` |
| POST | `/api/documents/<identifier>/import` |
| POST | `/api/documents/<identifier>/reorder` |
| POST | `/api/documents/<identifier>/split` |
| POST | `/api/documents/merge` |
| GET | `/api/events` |
| GET | `/api/exports` |
| GET | `/api/exports/<identifier>/download` |
| GET | `/api/exports/<identifier>/verify` |
| GET | `/api/health` |
| GET | `/api/operations` |
| GET | `/api/pages/<identifier>` |
| PATCH | `/api/pages/<identifier>` |
| GET | `/api/pages/<identifier>/edges` |
| GET | `/api/pages/<identifier>/image` |
| POST | `/api/pages/<identifier>/ocr` |
| PATCH | `/api/pages/<identifier>/ocr` |
| POST | `/api/pages/<identifier>/process` |
| POST | `/api/pages/<identifier>/regions` |
| GET | `/api/pages/<identifier>/regions/<region_id>` |
| POST | `/api/pages/<identifier>/restore` |
| GET | `/api/pages/<identifier>/skew` |
| GET | `/api/pages/<page_id>/analysis` |
| GET | `/api/pages/<page_id>/comparison` |
| GET | `/api/pages/<page_id>/layout` |
| GET | `/api/pages/<page_id>/low-confidence` |
| POST | `/api/pages/<page_id>/notes` |
| PATCH | `/api/pages/<page_id>/notes/<note_id>` |
| DELETE | `/api/pages/<page_id>/notes/<note_id>` |
| GET | `/api/pages/<page_id>/provenance` |
| GET | `/api/pages/<page_id>/recommendations` |
| POST | `/api/pages/<page_id>/review` |
| POST | `/api/pages/<page_id>/split-spread` |
| POST | `/api/pages/concatenate` |
| POST | `/api/pages/copy` |
| POST | `/api/pages/delete` |
| POST | `/api/pages/move` |
| GET | `/api/pipeline-runs` |
| GET | `/api/pipeline-runs/<identifier>` |
| POST | `/api/pipeline-runs/<identifier>/cancel` |
| GET | `/api/pipelines` |
| POST | `/api/pipelines` |
| GET | `/api/pipelines/<identifier>` |
| PATCH | `/api/pipelines/<identifier>` |
| DELETE | `/api/pipelines/<identifier>` |
| POST | `/api/pipelines/<identifier>/run` |
| GET | `/api/preferences` |
| PATCH | `/api/preferences` |
| POST | `/api/preferences/reset` |
| GET | `/api/reports/workspace` |
| GET | `/api/review/queue` |
| GET | `/api/review/settings` |
| PATCH | `/api/review/settings` |
| GET | `/api/search` |
| GET | `/api/session` |
| PUT | `/api/session` |
| GET | `/api/sources` |
| GET | `/api/sources/<identifier>/download` |
| GET | `/api/sources/<identifier>/verify` |
| POST | `/api/tasks` |
| GET | `/api/tasks` |
| GET | `/api/tasks/<identifier>` |
| POST | `/api/tasks/<identifier>/cancel` |
| POST | `/api/tasks/<identifier>/retry` |
| GET | `/api/workspace` |

页面的“恢复练习”使用 POST /api/demo/recovery 载入从真实中断服务保存的练习资料；不是自动验收脚本。页面书签和复核入口对应已有目录与审核接口。
