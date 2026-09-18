# A股自选快照

北京时间交易时段自动分析。不代下单。

## 跑一次

```bash
python3 run_snapshot.py
```

报告在 reports/ 与 自选快照.pdf

## 自选

编辑 watchlist.json

## 仓位与风控

编辑 `risk_config.json`：

| 字段 | 含义 |
|---|---|
| capital | 总资金 |
| risk_pct | 单笔愿意亏掉总资金的百分比 |
| max_total_pct | 总仓上限 |
| max_line_pct | 单条主线仓位上限 |
| max_name_pct | 单票仓位上限 |
| max_per_line | 同一条主线最多买几只 |
| cost_pct | 双边交易成本（印花税+佣金+过户费） |

报告第 0c 节按「风险额 ÷ 止损距离」给建议股数，并挡住同主线重复下注和净盈亏比低于 1.2 的位置。

## 信号复盘（闭环）

每次运行会把判别留档到 `reports/journal/YYYY-MM.jsonl`，隔一个交易日后用已抓的日线
回填 T+1/T+3/T+5 收益，报告第 0d 节按「判别 × 战法」出胜率和平均收益。
这张表是改阈值的依据；它不参与当天的判别。

## 运行期产物

`reports/journal/`（信号留档）、`reports/gate_state.json`（判别滞后带状态）、
`reports/ztpool/`（涨停池历史缓存）、`reports/overnight_cache.json`（隔夜报价缓存）。
删掉不影响运行，但复盘样本会丢。

## 回滚

本目录是 git 仓库。`git log --oneline` 看版本，`git checkout .` 回到上一次提交，
完整包备份在 `/opt/zixuan-backup/`。
