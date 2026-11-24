# 预售监管资金的可覆盖履约事实仓

银行归集、合同义务和工程节点限定资料。本仓库包含原始合成资料、来源格式
文档，以及资料入仓、相互对账、覆盖数据生成与下钻查询的完整程序。
程序只做资料整理与解释，不执行实际付款或审批。

## 环境

使用 Python 3.11，依赖精确版本记录在 requirements.txt。DuckDB 用于本地
分析存储。

    python3 -m pip install -r requirements.txt

## 业务入口

    # 入仓（校验 manifest 后装入 raw 层并重建整理层）
    python3 -m escrow_dw ingest --data data --db build/escrow.duckdb

    # 生成发布：截至某日收到资料的监管解释 / 业务月份现状
    python3 -m escrow_dw release --db build/escrow.duckdb --as-of 2026-11-05
    python3 -m escrow_dw release --db build/escrow.duckdb --month 2026-11

    # 下钻查询：项目 / 合同 / 义务 / 账户 / 节点 / 对账发现 / 资金覆盖证据
    python3 -m escrow_dw query --db build/escrow.duckdb project
    python3 -m escrow_dw query --db build/escrow.duckdb contract C1

    # 发布一致性检查与自动化场景验证
    python3 -m escrow_dw verify --db build/escrow.duckdb
    python3 -m unittest discover -s tests -v

概念、口径、对账发现类别与完整操作步骤见 docs/runbook.md（中文运行手册）。
资料格式和来源语义见 docs/source-contract.md；data 内均为本地合成记录。

## 资料与运行时检查

检查资料字节、行数与字段形状：

    python3 tools/inspect_source.py

检查 Python 文件能否编译：

    python3 -m compileall -q escrow_dw tools tests
