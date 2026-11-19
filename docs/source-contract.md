# 来源资料约定

银行归集、合同义务和工程节点限定资料。

金额均为人民币分；bank kind的receipt/payment/refund/reversal/internal分别为购房到账、工程支出、实际退回、对原行冲正与内部调拨。reversal引用reference，金额取被冲正部分绝对值。

statement范围以coverage_key标识，detail声明相同coverage_key中的逐笔组成，complete声明所含银行记录是否覆盖汇总。细节未齐不以其小计替代全量。

assignment将某来源资金片段归集到合同和用途，amount不得超过对应资金可归集部分；未归集余额保留账户，不推断分配。

obligations的delivery为合同交付责任金额，refund_requested为应退款责任，refund_cancelled引用原申请；合同现金回款与义务分开。nodes的amount是用途及楼栋限定释放上限，depends_on列出仍需有效的前序证明。计划行不是银行已付款。

同用途义务没有隐含的优先顺位；若现有凭证不足以唯一决定合同覆盖，应给可覆盖合计与受影响候选、未决原因，不虚构唯一分配。已付款与实际退款的reference给出对应义务或证明。

## 数据载体

每类资料位于 data 中同名的 NDJSON 文件，一行一个原始对象；字段和基本类型见 data/schema.json。data/manifest.json 固定资料行数与字节哈希。资料中的缺失、歧义、未完整交付及冲突声明属于来源事实，不由资料检查程序推断业务结论。

### bank

银行现金记录。字段：bank_id (string)、account (string)、kind (string)、amount_cents (integer)、date (string)、coverage_key (string)、reference (any)、project (string)、building (string)、purpose (string)。

### statements

汇总与明细覆盖声明。字段：coverage_key (string)、summary_id (string)、detail_ids (array)、complete (boolean)、received (string)。

### assignments

资金归集凭证。字段：assignment_id (string)、bank_id (string)、contract (string)、amount_cents (integer)、purpose (string)、effective (string)。

### obligations

交付与退款责任。字段：obligation_id (string)、contract (string)、kind (string)、building (string)、amount_cents (integer)、effective (string)、reference (any)。

### nodes

工程释放资格。字段：proof_id (string)、building (string)、purpose (string)、amount_cents (integer)、effective (string)、depends_on (array)、supersedes (any)、received (string)。

### transfers

合同楼栋归属变更。字段：record_id (string)、contract (string)、from_building (string)、to_building (string)、effective (string)、received (string)。

