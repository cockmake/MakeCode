import pandas as pd
from typing import Optional

# 加载数据表
df = load_table("xiao_shou_ding_dan_biao_j_engine_sheet_0_20260326195521108", ["下单时间", "销售额", "销售数量"])

# 筛选2025年4月到2026年3月的数据
df = df[(df["下单时间"] >= "2025-04-01") & (df["下单时间"] <= "2026-03-31")]

# 提取月份
df["月份"] = df["下单时间"].dt.strftime("%Y-%m")

# 按月分组统计
result = df.groupby("月份").agg(
    总销售额=("销售额", "sum"),
    总订单量=("销售数量", "sum")
).reset_index()

# 重命名列并添加后缀
result = result.rename(columns={
    "总销售额": "总销售额_numeric",
    "总订单量": "总订单量_numeric"
})

print_result(result)