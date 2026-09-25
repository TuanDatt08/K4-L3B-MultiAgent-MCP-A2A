# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Toàn bộ workflow là deterministic, rule-based, nằm trong `src/student_agent/workflow.py` (không dùng LLM).

```text
Input ─► Entity agent ─► Coordinator ─┬─► Order agent ────┐
          (customer history,           ├─► Shipment agent ─┤
           chọn order record)          ├─► Payment agent ──┼─► Conflict resolution ─► Policy agent ─► Verifier ─► Output
                                       └─► Policy agent ───┘   (timeline, record)      (policy_decided)
            │                               │                         │                    │
            └──────────────── MCP (evidence envelope, cache per case) ┴──── Trace (observable events)
```

1. **Entity agent** gọi `get_customer_history` với `customer_unique_id_hint`, giao với `candidate_order_ids` để resolve order, rồi chọn *order record* đúng trong các record trùng `order_id`.
2. **Coordinator** giao việc lần lượt cho order/shipment/payment/policy agent.
3. Các specialist chỉ lấy evidence thuộc record đã chọn (gán theo timestamp), phân tích và handoff về coordinator.
4. **Policy agent** chọn `primary_issue` và áp rule của `EC_POLICY_V2` (status, refund, action, responsible party).
5. **Verifier** kiểm tra invariant rồi emit `verification_completed`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case input (candidates, hint, `opened_at`) | Resolve order, reject candidate, chọn record trước `opened_at` | `get_customer_history` | entity_resolution, customer_context → coordinator |
| Coordinator | Kết quả entity | Giao task, gom kết quả, chuyển verifier | không gọi tool | `task_assigned`, `handoff` → verifier |
| Order | resolved order_id | Items, seller, record của `get_order` để phát hiện conflict | `get_order`, `get_order_items` | items, sellers → coordinator |
| Shipment | order_id + record | Verdict on_time / seller_delay / logistics_delay | `get_shipment_summary` | shipment_analysis → coordinator |
| Payment/refund | order_id + claim topic | Captured/refunded, mismatch, duplicate, split, refund lifecycle | `get_payment_timeline`, `get_refund_timeline` (chỉ khi claim là refund_*) | payment_analysis → coordinator |
| Policy | policy_version, issue | Map issue → status, refund, action, responsible parties | `get_policy` | `policy_decided` |
| Conflict resolver | Record từ nhiều nguồn | Áp source precedence, ghi `data_conflicts` | không gọi tool (dùng evidence đã cache) | data_conflicts |
| Verifier | Output nháp + evidence cache | Kiểm tra invariant, hạ confidence nếu fail | không gọi tool | `verification_completed` → coordinator |

Least privilege: mỗi actor chỉ gọi tool trong cột permission; verifier/conflict resolver chỉ đọc cache.

## 3. Entity resolution và A2A protocol

- **Candidate ranking:** candidate hợp lệ phải xuất hiện trong customer history. Ưu tiên `claimed_order_id` nếu nằm trong history. Còn lại đưa vào `rejected_candidates` (ví dụ `candidate-NNN`).
- **Status:** 1 match → `resolved` (confidence 0.95); >1 match → `ambiguous` (0.5); 0 → `not_found` (0.2, bỏ qua mọi specialist call, `primary_issue=insufficient_evidence`).
- **Record selection:** một `order_id` có thể có nhiều record mâu thuẫn (kể cả record trùng lặp y hệt). Record được chọn là record muộn nhất **đã đến hạn giao** (`order_estimated_delivery_date` ≤ `opened_at`). Nếu không có thì fallback về purchase muộn nhất ≤ `opened_at`. Record "tương lai" hoặc chưa đến hạn không thể là đối tượng khiếu nại.
- **Child row ownership:** event/item thuộc record có timestamp trùng khớp chính xác, nếu không có thì thuộc record purchase muộn nhất trước nó. Row/event trùng y hệt được dedupe. Split payment được nhận khi có một tập con ≥2 capture khớp tổng đơn.
- **Message envelope:** mỗi handoff là trace event gồm `case_id`, `actor`, `target`, `decision_code` và attributes A2A (mục 3.1). Correlation là `case_id`, mọi state nằm trong object `Investigation` riêng từng case.
- **Chống vòng lặp:** luồng một chiều entity → specialists → policy → verifier, không re-dispatch. Mỗi tool tối đa 1 lần/case.
- Nội dung khiếu nại chỉ được đọc như dữ liệu (topic), không được thực thi như instruction.

### 3.1 Ánh xạ Google A2A v1.0 (in-process)

Workflow áp dụng các khái niệm cốt lõi của A2A v1.0 nhưng chạy trong cùng process (không có HTTP/JSON-RPC server). Lý do: scorer chỉ đọc output, trace và MCP audit, không gọi agent qua mạng, nên transport mạng chỉ thêm độ trễ và điểm lỗi.

| Khái niệm A2A | Cài đặt trong repo |
| --- | --- |
| Agent Card (`skills`, security) | `AGENT_CARDS` trong `workflow.py`: mỗi agent có 1 skill + tập MCP tool được cấp |
| Least privilege | `Investigation.fetch` raise `PermissionError` nếu tool nằm ngoài card, **trước** khi gọi MCP (không sinh audited call) |
| Discovery | `day09 run` đối chiếu mọi tool trong card với `list_tools()` của gateway, dừng nếu thiếu |
| Task (`taskId`, `contextId`) | `a2a_task_id = "<case_id>:<skill>"`, `a2a_context_id = case_id` trong `attributes` của trace |
| Task lifecycle | `task_assigned` = `submitted`; `handoff` của agent = `completed`; `handoff` coordinator → verifier = `submitted` cho task của verifier |
| Message parts / artifacts | Dict Python truyền trực tiếp giữa các agent; artifact cuối là output theo `l3b-output-v2` |

| Agent Card | Skill | MCP tools | Bảng Olist tương ứng |
| --- | --- | --- | --- |
| entity-agent | `resolve_entity` | `get_customer_history` | customers, orders |
| order-agent | `order_context` | `get_order`, `get_order_items` | orders, order_items |
| shipment-agent | `shipment_analysis` | `get_shipment_summary` | orders (timestamps), order_items (`shipping_limit_date`) |
| payment-agent | `payment_analysis` | `get_payment_timeline`, `get_refund_timeline` | order_payments (+ refund lifecycle) |
| policy-agent | `policy_decision` | `get_policy` | policy `EC_POLICY_V2` |
| verifier | `verify_output` | không có | chỉ đọc evidence cache |

Không có agent Review/Geo vì MCP gateway không cung cấp tool cho bảng reviews/geolocation.

## 4. Evidence và conflict lifecycle

- Mọi MCP response được validate bằng `mcp-evidence-response-v1` trong `EvidenceGateway.call`.
- Envelope lưu nguyên vẹn trong cache `Investigation.evidence` (key = tool name), `evidence_ref` không bao giờ bị sửa hay tự tạo. Mỗi call thành công emit `tool_result_consumed` với đúng ref đó.
- Child rows (items, payment/shipment/refund events) được gán cho record có timestamp trùng khớp chính xác; nếu không có thì cho record có purchase timestamp muộn nhất ≤ timestamp của row. Row thuộc record khác bị loại, row trùng y hệt được dedupe.
- **Source precedence:**
  - Record trong customer history (lọc theo `opened_at`) > record `get_order` → conflict `order_record`, `LATEST_RECORD_DUE_AT_CASE_OPEN`.
  - Timestamp timeline > shipment event rời → conflict `shipment.delay_responsibility`, `AUTHORITATIVE_TIMELINE_PRECEDENCE`.
- **Claim linkage:** mỗi claim trong `claim_assessments` trỏ tới evidence của domain liên quan (shipment cho late delivery, payment/refund cho payment issue, policy cho refund request).
- Output `evidence_refs` = toàn bộ ref đã consume trong case đó, không dùng chéo case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout / transport error | 1 | Bỏ evidence đó, domain → `insufficient_evidence` | `verification_completed.attributes.failed_tools` |
| MCP tool error / envelope sai schema | 0 | Như trên (không retry vì không idempotent-hữu ích) | `failed_tools` |
| Entity not found/ambiguous | 0 | Dừng specialist call, `needs_investigation` | `handoff` `ENTITY_NOT_FOUND` / `ENTITY_AMBIGUOUS` |
| Source conflict | 0 | Áp precedence ở mục 4, ghi `data_conflicts` | `data_conflicts[].resolution_code` |
| Invalid specialist result | 0 | Verifier hạ confidence ≤ 0.5 | `verification_completed` `FAIL_<CODE>` |

**Query budget:** 6 call/case (history, order, items, shipment, payment timeline, policy), thêm `get_refund_timeline` chỉ khi claim là `refund_pending`/`refund_failed` (7 call). Không gọi `get_order_payments`/`get_sellers` vì trùng dữ liệu với payment timeline/items (thử nghiệm thêm 2 tool này: evidence −3, efficiency về 0). Không gọi `get_product_context` vì không issue nào cần dữ liệu sản phẩm. Không gọi tool với candidate chưa có trong customer history.

## 6. Verification invariants

Trước finalize, verifier kiểm tra:

- Schema: `contracts.validate_output` (CLI) trước khi ghi file.
- Entity scope: order không vừa resolved vừa rejected, output chỉ chứa record đã chọn.
- Evidence ownership: mọi `evidence_refs` ⊆ ref đã consume trong chính case.
- Payment/refund totals: tổng `refund_lines` = `recommended_refund_brl`, `refundable_total_brl` = refund đề xuất.
- Status/action consistency: `case_status != action_required` ⇒ refund = 0. Action lấy đúng từ policy rule.
- Responsibility: seller trong `responsible_parties` phải thuộc `affected_entities.seller_ids`, và `late_seller_ids` chỉ có khi verdict `seller_delay`.
- Confidence bounds: 0.9 khi evidence xác nhận claim, 0.7 khi evidence chỉ ra issue khác, ≤0.5 khi verifier fail hoặc thiếu policy rule.

## 7. Reproducibility

- Python ≥ 3.11 (đã chạy với 3.12), dependency theo `pyproject.toml` (`mcp` 2.x, `httpx2` 2.x).
- Không có model/LLM, không random: cùng MCP data sẽ cho cùng output.
- Concurrency: tuần tự hoàn toàn (case và specialist) trên một MCP session keep-alive; transport retry connect tối đa 3 lần.
- Lệnh: `day09 run && day09 validate && day09 package --output dist/submission.zip`.
- API key chỉ nằm trong `.env` (gitignored), không ghi vào output/trace.
