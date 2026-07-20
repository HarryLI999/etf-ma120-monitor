# ETF MA120 自动监控

每个A股工作日北京时间15:40自动运行：

- 通过AKShare的 `fund_etf_hist_em` 接口获取东方财富ETF历史行情；
- 当天不复权收盘价作为ETF现价；
- 最近120个交易日前复权收盘价计算MA120；
- 计算 `现价 / MA120` 并由低到高排序；
- 生成 `data/latest.json` 与 `data/latest.csv`；
- 若当天无行情（周末、法定休市或数据尚未更新），不覆盖上一份结果。

## 手动测试

进入仓库的 Actions 页面，选择 `Update ETF MA120`，点击 `Run workflow`。

## 固定数据地址

仓库设为Public后，JSON地址格式为：

`https://raw.githubusercontent.com/你的GitHub用户名/etf-ma120-monitor/main/data/latest.json`

CSV地址格式为：

`https://raw.githubusercontent.com/你的GitHub用户名/etf-ma120-monitor/main/data/latest.csv`
