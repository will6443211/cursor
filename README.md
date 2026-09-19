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

每个交易日每只可小仓只记一次：入选日 + 入选价。盘中反复跑不追加。
盘中 09:30–14:50 入选，成交价=入选价；收盘后入选，成交价=次日开。A股 T+1，胜负看次日收相对成交价并扣成本。

## 隔夜数据

外盘报价优先新浪美股/期货（Yahoo 常 403），并每天抓新浪财经滚动作第1/2节快讯。
笔记 `macro_notes.json` 只作背景，过期会标明截止日期。

## 运行期产物

`reports/journal/`（信号留档）、`reports/gate_state.json`（判别滞后带状态）、
`reports/ztpool/`（涨停池历史缓存）、`reports/overnight_cache.json`（隔夜报价缓存）。
删掉不影响运行，但复盘样本会丢。

## 回滚

本目录是 git 仓库。`git log --oneline` 看版本，`git checkout .` 回到上一次提交，
完整包备份在 `/opt/zixuan-backup/`。
