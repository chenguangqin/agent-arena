---
name: compensation-policy
description: 物流延误补偿。客户反映包裹晚到、要求补偿时加载。
---
# 延误补偿（摘自 SOP-COMPENSATION v3.0）

1. get_shipment 查承诺送达日期（promised）和实际送达日期（delivered），延误天数 = delivered − promised（自然日）。
2. 面额：1–3 天 10 元；4–7 天 30 元；8 天及以上 50 元，并在回复中致歉。
3. 例外：物流 notes 注明客户在发货后修改了收货地址的，不补偿，向客户说明原因。
4. 用 issue_coupon 发券，通知客户面额和有效期（30 天）。同一订单只补偿一次。
