# 监管资金数仓运行手册

本程序用本地合成资料形成资金、义务和覆盖关系的明细及项目期间数据集，供复核
人员查询。程序只做资料整理与解释，不执行实际付款或审批。

回答的核心问题：已到账款项在退款、专项保留和工程节点支付之后，还覆盖哪些
已签约交付义务。

## 1. 概念

### 1.1 双时钟

- **知识时点（K）**：监管方收到某份证据的时间。银行汇总先到、明细后到，
  节点更正、楼栋变更也都有各自的到达时间。
- **业务时点（D）**：业务事实发生的时间（到账日、支付日、义务生效日等）。

一次**发布**就是在指定 (K, D) 下，对全部资料做一次性、内部一致的解释，
原子写入一组 `rel_*` 表。同一发布内资金解释与覆盖解释必然一致——不会看到
"到账已冲正而覆盖仍显示充足"。

两类查询口径：

| 口径 | 命令 | 含义 |
| --- | --- | --- |
| 截至某次收到资料时的监管解释 | `release --as-of 日期` | K=D=该日，只用当日及之前收到且已生效的资料 |
| 业务月份的现状 | `release --month 月份` | D=月末，K 不设限，用已收到的全部资料（含事后更正）解释该业务月份 |

### 1.2 覆盖的三分类

每条交付义务在给定发布下被分解为三部分（三者相加恒等于责任金额）：

- **确定可覆盖**：有归集凭证明确归集到该合同、用途与楼栋一致、冲正后仍然
  存在、且未被退款/归属支出/无法归属支出侵蚀的资金。
- **尚待核实**：证据不足或互相矛盾，无法唯一确定归属的部分。原因包括：
  明细未确认、归集超额、单据矛盾、支出无法归属到合同、合同楼栋已变更等。
  程序给出可覆盖合计与受影响候选，不虚构唯一分配。
- **已缺口**：按现有证据确定没有资金支持的部分（如资金已实际退回、
  从未归集到合同）。

项目层面只给出这三类金额与状态标记（确定可覆盖/尚待核实/已缺口），
**不会**把整份项目简单标成"成功"。

### 1.3 资金口径

- 银行 `receipt/payment/refund` 分别为购房到账、工程支出、实际退回；
  `reversal` 冲正引用原行，按被冲正部分减少原行可用额，**原行不删除**；
  `internal` 内部调拨不产生新的购房款，不计入监管资金。
- 同一 `coverage_key` 的汇总行与明细行是同一资金的两种证据，只算一次：
  以汇总金额为准；明细未齐（`complete=false`）时不以明细小计替代全量；
  明细补齐且与汇总一致时证据状态为"已确认"；不一致则整笔资金按待核实处理。
- 归集（assignment）把资金片段归集到合同和用途，金额不得超过对应资金
  可归集部分；**未归集余额保留在账户层**，不为凑齐合同回款而分给任意项目。
- 退款分两个环节：退款申请（`refund_requested`）只产生义务，形成专项
  保留；银行实际退款（`refund`）才减少资金并结算该义务。两个环节不会
  各扣一次。`refund_cancelled` 撤销原申请，解除保留。
- 不同用途（purpose）的专项余额不允许互补：多用途合同按用途分别核实，
  项目汇总取单一用途最优结果，不把用途间余额相加。
- 工程节点（nodes）按楼栋与用途限定可释放额度；被更正（supersedes）的
  证明不再提供额度；`depends_on` 前序证明失效时后续证明一并失效。
  已付款超出当前有效额度的部分标记为"失去合法资金支持"；未发生的支付
  计划不得沿用旧额度（剩余可释放按更正后额度计算）。
- 合同跨楼栋变更（transfers）生效后，义务按新楼栋解释，原楼栋归集资金
  与节点覆盖需要重新核实，相应金额进入"尚待核实"。

## 2. 环境准备

Python 3.11，依赖见 requirements.txt（仅 DuckDB）：

```bash
python3 -m pip install -r requirements.txt
```

所有命令在仓库根目录执行。数据库文件默认在 `build/escrow.duckdb`，可用
`--db` 指定其他路径。

## 3. 操作流程

### 3.1 资料入仓

```bash
python3 -m escrow_dw ingest --data data --db build/escrow.duckdb
```

入仓先按 `data/manifest.json` 校验字节哈希、行数与字段形状（与
`tools/inspect_source.py` 同一约定），再把原始行装入 `raw_*` 表并重建
`stg_*` 整理层。重复执行是幂等重建。资料中的缺失、歧义与冲突不在入仓时
丢弃，而是带入后续对账。

### 3.2 生成发布

```bash
# 截至某日收到资料时的监管解释
python3 -m escrow_dw release --db build/escrow.duckdb --as-of 2026-11-05 --label "明细补齐、退款申请已到"

# 业务月份现状（用全部已收资料解释该月）
python3 -m escrow_dw release --db build/escrow.duckdb --month 2026-11 --label "11月业务现状"

# 查看全部发布
python3 -m escrow_dw releases --db build/escrow.duckdb
```

每次发布自动生成一致性检查（见 3.4），不通过会以非零码退出。

### 3.3 下钻查询

`--release` 缺省为最新发布；加 `--json` 输出机器可读结果（金额单位：分）。

```bash
# 项目总览：三类覆盖金额、留存资金、整体缺口、账户层未归集余额
python3 -m escrow_dw query --db build/escrow.duckdb project

# 合同明细：义务覆盖分解及原因、可归集资金构成、归集凭证、义务状态
python3 -m escrow_dw query --db build/escrow.duckdb contract C1

# 单笔义务：为什么待核实/缺口（原因、金额、关联单据）
python3 -m escrow_dw query --db build/escrow.duckdb obligation O1

# 账户层：到账(冲正后)、支出、退款、留存、已归集、暂不能归属余额
python3 -m escrow_dw query --db build/escrow.duckdb account A1

# 节点额度：有效额度、已付款、剩余可释放、失去支持金额、有效/失效证明
python3 -m escrow_dw query --db build/escrow.duckdb nodes --building B1

# 对账发现：重复单据、矛盾金额、超额归集、节点超限等
python3 -m escrow_dw query --db build/escrow.duckdb findings --severity error

# 资金覆盖证据：汇总/明细归并结果、成员行、冲正后可用额
python3 -m escrow_dw query --db build/escrow.duckdb portions --coverage-key oct-in
```

### 3.4 一致性验证

```bash
python3 -m escrow_dw verify --db build/escrow.duckdb --release R0003
```

检查项：每条义务 确定+待核+缺口=责任金额；项目确定可覆盖不超过留存资金；
资金桶 留存=到账-支出-退款；归集不超过冲正后可用额（已冲正资金不出现在
任何合同的确定可覆盖中）。

### 3.5 自动化场景验证

```bash
python3 -m unittest discover -s tests -v
```

覆盖以下场景（tests/test_scenarios.py）：

- 分次到账：一份合同分几笔到账、一笔流水归集多个合同；
- 冲正：同一发布内到账冲正后覆盖同步减少；
- 冲正与节点更正相遇：已付款事实保留、失去合法支持部分显现、旧额度不得
  沿用、发布一致性不变量；
- 退款两环节：申请只保留、实际退款才减资金、不重复扣减、撤销解除保留；
- 汇总与明细：两种证据不相加、明细未齐以汇总为准、汇总明细矛盾按待核实；
- 内部调拨不产生购房款；重复单据与矛盾金额可区分；
- 跨楼栋变更的联合重新解释；用途间不互补；未归集余额保留账户层；
- 节点前序依赖失效；归集超额不虚构分配；双时钟与项目期间视图。

CLI 端到端冒烟见 tests/test_cli_smoke.py。

## 4. 主数据示例解读

`data/` 中的合成资料演示了一条完整时间线，可用三个发布观察：

```bash
python3 -m escrow_dw release --db build/escrow.duckdb --as-of 2026-11-01
python3 -m escrow_dw release --db build/escrow.duckdb --as-of 2026-11-05
python3 -m escrow_dw release --db build/escrow.duckdb --month 2026-11
```

- **11-01（仅日终汇总）**：账户 A1 到账 100,000.00 元只有汇总证据；归集凭证
  引用的明细行银行尚未提供，合同覆盖全部"尚待核实"；资金保留在账户层。
- **11-05（明细补齐、退款申请已到）**：明细确认，归集生效；PAY1 工程支出
  20,000.00 元无法归属到具体合同，C1/C2 各有一部分"尚待核实"；REQ1 退款
  申请 10,000.00 元形成专项保留（资金未减、义务增加）。
- **11 月业务现状**：REF1 实际退款结算 REQ1（资金减少、义务关闭，只扣一次）；
  N2 把 B1/construction 额度从 30,000.00 更正为 10,000.00 元，PAY1 已付款
  超出 10,000.00 元被标记"失去合法资金支持"，剩余可释放为 0；C2 楼栋
  变更至 B2，其 40,000.00 元归集资金需重新核实。项目最终：确定可覆盖
  30,000.00 元、尚待核实 60,000.00 元、已缺口 10,000.00 元。

## 5. 对账发现类别

| 类别 | 级别 | 含义 |
| --- | --- | --- |
| duplicate_document | info | 内容完全一致的重复单据，只按一份生效 |
| conflicting_document | error | 主键相同但内容不同的矛盾单据，相关金额待核实 |
| statement_ref_missing | error | 汇总声明引用了未收到的银行记录 |
| statement_extra_row | warn | 银行记录声称属于某覆盖但未被声明引用 |
| statement_conflict | error | 同一覆盖在同一到达时间收到内容矛盾的汇总声明 |
| summary_detail_mismatch | error | 明细补齐后与汇总金额不符，整笔资金待核实 |
| summary_row_missing | error | 缺少汇总行，明细小计不能替代全量 |
| detail_incomplete | info | 明细未齐，以汇总金额为准 |
| coverage_kind_mixed | error | 同一覆盖混有不同业务性质的记录 |
| reversal_target_unknown / reversal_exceeds_original | error | 冲正引用未知或超过原行 |
| internal_excluded | info | 内部调拨不计入监管资金 |
| assignment_target_unknown / target_unconfirmed | error/warn | 归集目标未知或证据未确认 |
| assignment_overdraw | error | 归集合计超过资金可归集部分，相关归集全部待核实 |
| refund_request_unknown / refund_cancel_unknown | error | 退款/撤销引用未知 |
| refund_settled_twice / refund_cancel_after_settle | error/warn | 重复结算或结算后撤销 |
| refund_exceeds_assignment | error | 已归属支出与应退保留超过可归集资金 |
| payment_reference_unknown | warn | 工程支出引用无法识别 |
| payment_not_attributed | warn | 未归属支出超出未归集余额，影响合同归集资金 |
| node_dependency_missing / invalid / cycle | error | 节点前序证明缺失、失效或循环 |
| node_cap_breach | error | 已付款超出当前有效额度，失去合法资金支持 |
| contract_building_changed | info | 合同楼栋变更，义务/资金/节点已重新解释 |
| contract_multi_project / contract_without_project | warn | 合同项目归属异常 |
| multi_purpose_review | info | 多用途合同，用途间不互补，分别核实 |

## 6. 数据层结构

- `raw_*`：来源原行（含重复与矛盾版本），附来源文件、行号、内容哈希。
- `stg_*`：整理层。知识日期、重复/矛盾标记、有效行标记。
- `rel_*`（按 release_id 区分）：发布结果。
  `rel_portion` 资金覆盖证据；`rel_fund_bucket` 资金桶（账户×项目×楼栋×
  用途）；`rel_assignment` 归集状态；`rel_obligation` 义务状态；
  `rel_node_cap` 节点额度；`rel_coverage` 义务覆盖分解（含原因）；
  `rel_contract_purpose` 合同-用途-楼栋可归集资金；`rel_project_status`
  项目期间视图；`rel_account_view` 账户层视图；`rel_finding` 对账发现。

## 7. 约定与限制

- 知识日期推导：statements/nodes/transfers 用自身 `received`；bank 行取
  引用它的 statement 的最早 `received`，无引用时用业务日期（视为随单到
  达）；assignments/obligations 来源无 `received` 字段，用 `effective`。
- 退款撤销按整单关闭处理；撤销金额与原申请不一致时以撤销为准并提示核对。
- 同一资金覆盖内业务性质不同的记录（如到账与支出混排）整笔按待核实处理。
- 无法归属到合同的工程支出，先冲减账户层未归集余额，超出部分视为影响了
  合同归集资金但无法唯一归属——相关合同各按全部影响额列"尚待核实"
  （保守口径，合同间会重叠，项目合计以"整体缺口"为准）。
- 本程序只读取本地合成资料做解释，不执行实际付款或审批；任何"确定可
  覆盖"结论都以所收证据为限。
