# 独居关怀提醒的值守交接与权限更新

设备回报只含在线状态。所有人员和联系方式使用合成标记，无真实通信或健康信息。任务接单与实际到场分开记录。**本服务只办理联络关怀，不做任何健康诊断。**

所有资料供本地联调，为合成记录。`samples/register.json` 是基础台账，`samples/intake.json` 是送达的业务事实，`samples/receipts.json` 是对端查询合同。

## 组成

- `partner_feed.py`：本地合成资料端（台账、到达事实、回执证据），只提供事实，不办理业务。
- `care/`：关怀服务后端，真正为值守站、住户与合作联系点办理联络事项。
  - `care/model.py`：联络窗口、授权规则（带版本与有效时段）、设备时段、关怀事件、知会、回执。
  - `care/service.py`：领域规则与办理动作（线程安全，可快照恢复）。
  - `care/feed_client.py`：对接资料端，处理 `/receipt` 的 503 延迟重试，按游标幂等消费。
  - `care/api.py` / `care/__main__.py`：仅监听回环地址的 HTTP 接口与启动入口。
  - `care/storage.py`：原子化 JSON 快照。

使用 Python 3.11.2 标准库，无第三方依赖。

## 运行

```sh
python3 -m unittest -v
python3 partner_feed.py --port 8765
python3 -m care --port 8080 --feed http://127.0.0.1:8765
# 只做一次资料同步并落快照后退出：
python3 -m care --sync-once --store data/care_snapshot.json
```

## 资料端接口

- `GET /register` 读取台账。
- `GET /intake?since=0&limit=50` 读取一页到达记录，将返回的 `cursor` 传给下一次查询的 `since`。消息含 `sequence`、`at`、`source`、`facts`，其中 `at` 是收到记录的时间，业务发生时间可在 `facts` 中明确报告。
- `GET /receipt?action=操作&desk=经办方&ref=编号` 以三项值匹配样例。对端返回 `evidence` 作为独立事实。标记 `delayed` 的样例首次查询返回 503，后续可以读取回执。未知编号返回 404。

## 关怀服务接口

所有业务接口在 `/api/...` 下，用 `X-Auth-Token` 区分角色。本地默认令牌可用环境变量覆盖：`DESK_TOKEN`、`RESIDENT_TOKEN`、`PARTNER_TOKEN`。

### 住户（resident）

| 接口 | 作用 |
| --- | --- |
| `POST /api/register` | 登记本人、联络窗口与分情形授权 |
| `POST /api/windows` | 新增/更换联络窗口（渠道、可用时段） |
| `POST /api/authorizations` | 授予或更新授权（新版本自动收口旧版本，历史保留） |
| `POST /api/revocations` | 撤回联系人：收口规则，在途知会标记失效但保留去向 |
| `GET /api/ledger` | 查本人当前窗口、授权与设备 |
| `GET /api/history` | 查已经做过的全部联系（含知会内容、状态、回执） |
| `POST /api/confirm-safe` | 本人确认平安（明确的完成依据） |

### 值守台（desk）

| 接口 | 作用 |
| --- | --- |
| `POST /api/devices` | 设备登记、暂停、换新（允许时段重叠）、记录在线/离线回报 |
| `POST /api/dispatch` | 按当前规则派单，透露内容不得超过该联系人授权上限 |
| `POST /api/late-decisions` | 迟到接手回执判定：`keep_original` 或 `station_takeover` |
| `POST /api/handovers` | 交班：保留当前负责动作、必要信息与关怀状态 |
| `POST /api/verify` | 值守台核实记录 |
| `POST /api/close` | 关闭提醒，必须有明确完成范围与依据编号 |
| `GET /api/events`、`GET /api/events/{id}` | 事件列表与详情（可按状态过滤） |
| `POST /api/feed-sync` | 从资料端同步台账并幂等消费到达事实 |

### 合作联系点（partner）

| 接口 | 作用 |
| --- | --- |
| `GET /api/tasks` | 只返回自己名下的在途任务与裁剪后的知会包 |
| `POST /api/receipts` | 回三种回执：`accepted` / `cannot_attend` / `verified_safe` |
| `POST /api/handovers` | 合作点接手，仅限自己已有在途任务的事件 |

## 关键规则如何落地

1. **授权与可透露内容**：规则含 `scope`（情形）与 `detail`（可透露上限：`visit_request` < `call_request` < `device_detail`）。派单时超范围透露直接拒绝；知会包按上限裁剪，设备详情只在授权内出现。
2. **规则可改、历史可溯**：授权更新生成新版本并收口旧版本；撤回只影响后续动作。已派出的知会永久记录当时的规则编号、版本与实际去向。
3. **离线不是确认需要帮助**：设备离线只产生 `unconfirmed` 事件；暂停设备的离线被抑制，未知设备只留痕，均不产生待办。
4. **设备暂停与换新**：设备以生效时段表达，换新允许时段重叠；每条回报回到对应设备与时段，同一未关闭事件不重复建单。
5. **回执与改派**：`accepted` 记录接手；`cannot_attend` 后按当前有效规则选择下一联系人，已撤回者不会被补发，无人可派时留值守台处理；`verified_safe` 是明确完成依据。
6. **撤回遇上迟到回执**：迟到的接手仍如实记录，但不恢复授权、负责人挂空；值守台通过 `late-decisions` 判定维持原安排或由值守站继续核对（自动暂停向已撤联系人补发）。
7. **交班不扩散信息**：值守台内部交班只交接事件快照（负责动作、关怀状态、在途知会编号、必要备注），合作点交班只能拿到自己任务的裁剪知会包，不会重新收到全部住户信息；事件编号不变，后续提醒在同一事件内继续办理。
8. **关闭守卫**：没有核实平安或值守核实依据、或缺少依据编号时，关闭请求被拒绝（`scope_unclear` / `refs_required`）。
9. **幂等与持久化**：资料端事实按 `sequence` 去重；状态变化原子写入快照，重启后可继续办理。

## 测试

```sh
python3 -m unittest                       # 全部 30 个用例
python3 -m unittest test_care_service     # 领域规则（20）
python3 -m unittest test_care_integration # 资料端到端样例故事（3）
python3 -m unittest test_care_api         # HTTP 角色与接口（7）
```
