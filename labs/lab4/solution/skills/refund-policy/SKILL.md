---
name: refund-policy
description: 退款类工单（重复扣款、商品破损、少发漏发、退货退款）。处理任何退款前加载。
---
# 退款操作步骤（摘自 SOP-REFUND v2.2）

1. 查询订单和该订单的全部支付记录（get_order、get_payments）。
2. 重复扣款：同一订单有两笔及以上 captured 扣款时，只退多出来的那一笔（时间较晚的一笔），保留一笔。
3. 按单笔退款金额决定权限：
   - ≤ 200 元：直接 issue_refund；
   - 200 元 < 金额 ≤ 1000 元：先 request_approval，批准后再 issue_refund；
   - > 1000 元：不退款，escalate_to_human。
4. 同时检查升级政策（escalation-policy）：VIP 客户超过 500 元、或出现法律字眼时必须升级，不退款。
5. reason 写具体原因，例如"重复扣款，退还多扣的一笔"。
6. 通知客户退款金额和到账时间（原路退回，1–3 个工作日）。
