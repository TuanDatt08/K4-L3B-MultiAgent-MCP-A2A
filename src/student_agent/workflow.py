from __future__ import annotations

from datetime import datetime
from itertools import combinations
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

# Detection order when the customer's claimed topic is not confirmed by evidence.
ISSUE_PRIORITY = (
    "refund_failed",
    "refund_pending",
    "canceled_order_paid",
    "unavailable_order_paid",
    "duplicate_charge",
    "payment_mismatch",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
)
REFUND_TOPICS = {"refund_pending", "refund_failed"}
REFUND_DONE = {"completed", "succeeded", "refunded", "confirmed"}
PAYMENT_VERDICT = {
    "refund_failed": "refund_failed",
    "refund_pending": "refund_pending",
    "duplicate_charge": "duplicate_capture",
    "payment_mismatch": "capture_mismatch",
}
EVENT_ACTOR_VERDICT = {"seller": "seller_delay", "logistics_provider": "logistics_delay"}

# In-process A2A: each agent publishes an Agent Card (one skill + the only MCP tools it may
# call). The coordinator delegates Tasks (id = "<case_id>:<skill>", contextId = case_id) whose
# lifecycle (submitted -> completed/failed) is written to the trace. No network transport:
# the scorer reads outputs + trace only, so a JSON-RPC server would add latency, not points.
AGENT_CARDS: dict[str, dict[str, Any]] = {
    "entity-agent": {"skill": "resolve_entity", "tools": {"get_customer_history"}},
    # get_product_context omitted: no issue type needs product data (efficiency + precision)
    "order-agent": {"skill": "order_context", "tools": {"get_order", "get_order_items"}},
    "shipment-agent": {"skill": "shipment_analysis", "tools": {"get_shipment_summary"}},
    "payment-agent": {
        "skill": "payment_analysis",
        "tools": {"get_payment_timeline", "get_refund_timeline"},
    },
    "policy-agent": {"skill": "policy_decision", "tools": {"get_policy"}},
    "verifier": {"skill": "verify_output", "tools": set()},
}
CARD_TOOLS = set().union(*(card["tools"] for card in AGENT_CARDS.values()))


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _money(value: Any) -> float:
    return round(float(value), 2)


# ---------------------------------------------------------------- pure analysis


RECORD_TIMES = (
    "order_purchase_timestamp",
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
)


def select_record(rows: list[dict[str, Any]], opened_at: datetime) -> dict[str, Any] | None:
    """Pick the record in scope: the latest one already due for delivery when the case opened.

    Falls back to the latest purchase before the case opened (e.g. no estimated date).
    """

    def purchase(row: dict[str, Any]) -> datetime:
        return _ts(row["order_purchase_timestamp"])

    due = [
        r for r in rows
        if _ts(r.get("order_estimated_delivery_date"))
        and _ts(r["order_estimated_delivery_date"]) <= opened_at
    ]
    before = [r for r in rows if purchase(r) <= opened_at]
    return max(due or before, key=purchase, default=None)


def owned_by(rows: list[dict[str, Any]], record: dict[str, Any]):
    """Child rows (items, events) belong to the record sharing their exact timestamp,
    otherwise to the latest record purchased before them."""
    starts = sorted(_ts(r["order_purchase_timestamp"]) for r in rows)
    mine = _ts(record["order_purchase_timestamp"])

    def check(value: str | None) -> bool:
        moment = _ts(value)
        if not moment:
            return False
        exact = {
            _ts(r["order_purchase_timestamp"])
            for r in rows
            if moment in {_ts(r.get(field)) for field in RECORD_TIMES}
        }
        if exact:
            return mine in exact
        owners = [s for s in starts if s <= moment]
        return bool(owners) and owners[-1] == mine

    return check


def unique(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop exact duplicate rows (identical records repeated across sources)."""
    return list({tuple(sorted(r.items())): r for r in rows}.values())


def analyze_shipment(
    record: dict[str, Any], items: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    carrier = _ts(record.get("order_delivered_carrier_date"))
    delivered = _ts(record.get("order_delivered_customer_date"))
    estimated = _ts(record.get("order_estimated_delivery_date"))
    limits = [_ts(i.get("shipping_limit_date")) for i in items if i.get("shipping_limit_date")]
    limit = max(limits, default=None)
    complete = all(
        (_ts(record.get("order_purchase_timestamp")), _ts(record.get("order_approved_at")),
         carrier, delivered, estimated)
    )
    late_actors = [
        e.get("actor")
        for e in events
        if e.get("event_type") == "delivered_late" and e.get("status") == "confirmed"
    ]
    event_verdict = next(
        (EVENT_ACTOR_VERDICT[a] for a in late_actors if a in EVENT_ACTOR_VERDICT), None
    )
    if delivered and estimated:
        if delivered <= estimated:
            verdict = "on_time"
        elif carrier and limit and carrier > limit:
            verdict = "seller_delay"
        else:
            verdict = "logistics_delay"
    else:
        verdict = event_verdict or "insufficient_evidence"

    conflict = None
    if event_verdict and delivered and estimated and event_verdict != verdict:
        # Timeline timestamps are authoritative over free-standing shipment events.
        conflict = {
            "field": "shipment.delay_responsibility",
            "sources": ["order_timeline", "shipment_events"],
            "selected_source": "order_timeline",
            "resolution_code": "AUTHORITATIVE_TIMELINE_PRECEDENCE",
        }
    late_sellers = sorted({i["seller_id"] for i in items}) if verdict == "seller_delay" else []
    result = {"verdict": verdict, "late_seller_ids": late_sellers, "timeline_complete": complete}
    return result, conflict


def analyze_payment(
    items: list[dict[str, Any]], events: list[dict[str, Any]], refunds: list[dict[str, Any]]
) -> tuple[dict[str, Any], set[str]]:
    order_total = round(sum(_money(i["price"]) + _money(i["freight_value"]) for i in items), 2)
    captures = [
        _money(e["amount_brl"])
        for e in events
        if e.get("event_type") == "captured" and e.get("status") != "failed"
    ]
    captured = round(sum(captures), 2)
    refunded = round(
        sum(_money(r["amount_brl"]) for r in refunds if r.get("status") in REFUND_DONE), 2
    )
    issues: set[str] = set()
    if any(e.get("event_type") == "reconciliation_mismatch" for e in events):
        issues.add("payment_mismatch")
    # A split is >=2 captures that together settle the order total exactly; any other
    # capture sharing the timestamps belongs to a conflicting duplicate record.
    split = order_total and any(
        abs(sum(combo) - order_total) <= 0.01
        for size in range(2, len(captures) + 1)
        for combo in combinations(captures, size)
    )
    if split:
        issues.add("valid_split_payment")
        captured = order_total
    elif len(captures) > 1 and captured > order_total + 0.01:
        issues.add("duplicate_charge")
    for refund in refunds:
        if refund.get("status") == "failed":
            issues.add("refund_failed")
        elif refund.get("status") == "pending":
            issues.add("refund_pending")
    return {
        "captured_total_brl": captured if captures else None,
        "refunded_total_brl": refunded,
    }, issues


def choose_issue(claimed: str | None, detected: set[str]) -> tuple[str, float]:
    if claimed in detected:
        return claimed, 0.9
    for issue in ISSUE_PRIORITY:
        if issue in detected:
            return issue, 0.7
    return "unsupported_claim", 0.85 if claimed == "unsupported_claim" else 0.7


# ---------------------------------------------------------------- agents


class Investigation:
    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.gateway = gateway
        self.trace = trace
        self.evidence: dict[str, dict[str, Any]] = {}  # per-case cache: tool name -> envelope
        self.failed: list[str] = []

    def emit(self, event_type: str, actor: str, **kwargs: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **kwargs)

    def ref(self, *tools: str) -> list[str]:
        return [self.evidence[t]["evidence_ref"] for t in tools if t in self.evidence]

    async def fetch(self, actor: str, tool: str, **arguments: str) -> Any:
        if tool not in AGENT_CARDS[actor]["tools"]:  # least privilege, checked before any call
            raise PermissionError(f"{actor} Agent Card does not grant {tool}")
        if tool in self.evidence:
            return self.evidence[tool]["data"]
        for attempt in range(2):  # one retry for transport errors only
            try:
                envelope = await self.gateway.call(tool, case_id=self.case_id, **arguments)
                break
            except (RuntimeError, ValueError):  # tool error / invalid envelope: not retryable
                self.failed.append(tool)
                return None
            except Exception:
                if attempt:
                    self.failed.append(tool)
                    return None
        self.evidence[tool] = envelope
        self.emit(
            "tool_result_consumed", actor, tool_name=tool,
            evidence_refs=[envelope["evidence_ref"]],
            attributes={"domain": envelope["domain"], "a2a_task_id": self.task_id(actor)},
        )
        return envelope["data"]

    def task_id(self, agent: str) -> str:
        return f"{self.case_id}:{AGENT_CARDS[agent]['skill']}"

    def task(self, agent: str, state: str) -> dict[str, str]:
        return {
            "a2a_task_id": self.task_id(agent),
            "a2a_context_id": self.case_id,
            "a2a_skill": AGENT_CARDS[agent]["skill"],
            "a2a_state": state,
        }

    def assign(self, agent: str) -> None:
        self.emit("task_assigned", "coordinator", target=agent,
                  attributes=self.task(agent, "submitted"))

    def handoff(self, agent: str, code: str, target: str = "coordinator") -> None:
        # a handoff completes the sender's task; the coordinator's handoff submits the target's
        owner, state = (agent, "completed") if agent in AGENT_CARDS else (target, "submitted")
        self.emit("handoff", agent, target=target, decision_code=code,
                  attributes=self.task(owner, state))

    async def resolve_entity(self) -> dict[str, Any]:
        self.assign("entity-agent")
        request = self.case["customer_request"]
        history = await self.fetch(
            "entity-agent", "get_customer_history",
            customer_unique_id=self.case["customer_unique_id_hint"],
        ) or {}
        orders = history.get("orders") or []
        known = {o["order_id"] for o in orders}
        candidates = self.case["candidate_order_ids"]
        matched = [c for c in candidates if c in known]
        claimed = request.get("claimed_order_id")
        resolved = [claimed] if claimed in matched else matched[:1]
        status = "resolved" if len(matched) == 1 else ("ambiguous" if matched else "not_found")
        rows = [o for o in orders if resolved and o["order_id"] == resolved[0]]
        record = select_record(rows, _ts(self.case["opened_at"])) if rows else None
        entity = {
            "status": status,
            "resolved_order_ids": resolved,
            "rejected_candidates": [c for c in candidates if c not in resolved],
            "confidence": 0.95 if status == "resolved" and record else 0.5 if resolved else 0.2,
            "customer_unique_id": history.get("customer_unique_id"),
            "related_order_ids": sorted(known),
            "rows": rows,
            "record": record,
        }
        self.handoff("entity-agent", f"ENTITY_{status.upper()}")
        return entity

    async def order_agent(self, order_id: str) -> tuple[Any, Any]:
        self.assign("order-agent")
        order = await self.fetch("order-agent", "get_order", order_id=order_id)
        items = await self.fetch("order-agent", "get_order_items", order_id=order_id)
        self.handoff("order-agent", "ORDER_CONTEXT_READY")
        return order, items or []

    async def shipment_agent(self, order_id: str) -> Any:
        self.assign("shipment-agent")
        data = await self.fetch("shipment-agent", "get_shipment_summary", order_id=order_id)
        self.handoff("shipment-agent", "SHIPMENT_EVIDENCE_READY")
        return data or {}

    async def payment_agent(self, order_id: str, claimed: str | None) -> tuple[Any, Any]:
        self.assign("payment-agent")
        timeline = await self.fetch("payment-agent", "get_payment_timeline", order_id=order_id)
        refunds = None
        # Refund timeline only for refund topics: a full scan of all 100 cases found no
        # in-scope refund events under other topics (the tool errors when none exist).
        if claimed in REFUND_TOPICS:
            refunds = await self.fetch("payment-agent", "get_refund_timeline", order_id=order_id)
        self.handoff("payment-agent", "PAYMENT_EVIDENCE_READY")
        return timeline or {}, refunds or {}

    async def policy_agent(self) -> Any:
        self.assign("policy-agent")
        policy = await self.fetch(
            "policy-agent", "get_policy", policy_version=self.case["policy_version"]
        )
        return policy or {}


def _claim_refs(inv: Investigation, issue: str) -> list[str]:
    if issue.startswith("late_delivery"):
        return inv.ref("get_shipment_summary", "get_customer_history")
    if issue in {"canceled_order_paid", "unavailable_order_paid"}:
        return inv.ref("get_customer_history", "get_payment_timeline")
    if issue in REFUND_TOPICS:
        return inv.ref("get_refund_timeline", "get_payment_timeline")
    return inv.ref("get_payment_timeline", "get_customer_history")


def verify(output: dict[str, Any], inv: Investigation) -> list[str]:
    problems = []
    fin = output["financial_resolution"]
    lines_total = round(sum(line["amount_brl"] for line in fin["refund_lines"]), 2)
    if lines_total != fin["recommended_refund_brl"]:
        problems.append("REFUND_LINES_TOTAL")
    if output["assessment"]["case_status"] != "action_required" and fin["recommended_refund_brl"]:
        problems.append("STATUS_REFUND_MISMATCH")
    owned = {e["evidence_ref"] for e in inv.evidence.values()}
    if not set(output["evidence_refs"]) <= owned:
        problems.append("FOREIGN_EVIDENCE_REF")
    sellers = set(output["affected_entities"]["seller_ids"])
    for party in output["root_cause_analysis"]["responsible_parties"]:
        if party["party_type"] == "seller" and party["party_id"] not in sellers:
            problems.append("SELLER_NOT_IN_SCOPE")
    if set(output["entity_resolution"]["resolved_order_ids"]) & set(
        output["entity_resolution"]["rejected_candidates"]
    ):
        problems.append("CANDIDATE_BOTH_RESOLVED_AND_REJECTED")
    return problems


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    inv = Investigation(case, gateway, trace)
    claims = case["customer_request"]["claims"]
    claimed = next((c["topic"] for c in claims if c["topic"] != "requested_full_refund"), None)

    entity = await inv.resolve_entity()
    record = entity["record"]
    detected: set[str] = set()
    conflicts: list[dict[str, Any]] = []
    shipment = {
        "verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False
    }
    payment: dict[str, Any] = {"captured_total_brl": None, "refunded_total_brl": None}
    items: list[dict[str, Any]] = []

    if record:  # skip specialist calls entirely when the entity cannot be resolved
        order_id = entity["resolved_order_ids"][0]
        # sequential on purpose: parallel calls open extra TLS connections the gateway drops
        order, all_items = await inv.order_agent(order_id)
        ship = await inv.shipment_agent(order_id)
        timeline, refunds = await inv.payment_agent(order_id, claimed)
        policy = await inv.policy_agent()
        mine = owned_by(entity["rows"], record)
        items = unique([i for i in all_items if mine(i.get("shipping_limit_date"))])
        ship_events = unique([e for e in ship.get("events", []) if mine(e.get("event_at"))])
        pay_events = unique([e for e in timeline.get("events", []) if mine(e.get("event_at"))])
        refund_events = unique([e for e in refunds.get("events", []) if mine(e.get("event_at"))])

        if order and order.get("order_purchase_timestamp") != record["order_purchase_timestamp"]:
            conflicts.append({
                "field": "order_record",
                "sources": ["get_order", "get_customer_history"],
                "selected_source": "get_customer_history",
                "resolution_code": "LATEST_RECORD_DUE_AT_CASE_OPEN",
            })
        shipment, ship_conflict = analyze_shipment(record, items, ship_events)
        if ship_conflict:
            conflicts.append(ship_conflict)
        payment, detected = analyze_payment(items, pay_events, refund_events)
        if payment["captured_total_brl"]:
            if record["order_status"] == "canceled":
                detected.add("canceled_order_paid")
            elif record["order_status"] == "unavailable":
                detected.add("unavailable_order_paid")
        detected |= {
            "seller_delay": {"late_delivery_seller"},
            "logistics_delay": {"late_delivery_logistics"},
        }.get(shipment["verdict"], set())
        primary, confidence = choose_issue(claimed, detected)
    else:
        policy = {}
        primary, confidence = "insufficient_evidence", 0.3

    rule = (policy.get("rules") or {}).get(primary)
    seller_ids = sorted({i["seller_id"] for i in items})
    if rule:
        status, action = rule["case_status"], rule["recommended_action"]
        refund = _money(rule["refund_brl"]) if status == "action_required" else 0.0
        parties = [
            {"party_type": p["party_type"],
             "party_id": (seller_ids[0] if seller_ids else None)
             if p["party_type"] == "seller" else p["party_id"]}
            for p in rule["responsible_parties"]
        ]
    else:
        status, action, refund = "needs_investigation", "escalate_manual_review", 0.0
        parties = [{"party_type": "unknown", "party_id": None}]
        confidence = min(confidence, 0.4)
    inv.emit("policy_decided", "policy-agent", decision_code=primary.upper(),
             evidence_refs=inv.ref("get_policy") or None,
             attributes={"case_status": status, **inv.task("policy-agent", "completed")})
    inv.handoff("coordinator", "READY_FOR_VERIFICATION", target="verifier")

    order_id = entity["resolved_order_ids"][0] if entity["resolved_order_ids"] else None
    payment_verdict = PAYMENT_VERDICT.get(primary) or (
        "reconciled" if payment["captured_total_brl"] else "insufficient_evidence"
    )
    claim_assessments = []
    for claim in claims:
        if claim["topic"] == "requested_full_refund":
            verdict = (
                "supported" if refund and refund >= (payment["captured_total_brl"] or 0)
                else "partially_supported" if refund
                else "insufficient_evidence" if status == "needs_investigation"
                else "unsupported"
            )
            refs = inv.ref("get_policy", "get_payment_timeline")
        else:
            confirmed = claim["topic"] == primary != "unsupported_claim"
            verdict = "supported" if confirmed else "unsupported"
            refs = _claim_refs(inv, claim["topic"])
        claim_assessments.append({
            "claim_id": claim["claim_id"], "verdict": verdict,
            "confidence": confidence, "evidence_refs": refs,
        })

    output = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": inv.case_id,
        "assessment": {
            "primary_issue": primary,
            "secondary_issues": sorted(detected - {primary}),
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": {
            "order_ids": [order_id] if order_id else [],
            "item_ids": sorted({i["order_item_id"] for i in items}),
            "seller_ids": seller_ids,
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            k: entity[k]
            for k in ("status", "resolved_order_ids", "rejected_candidates", "confidence")
        },
        "customer_context": {
            "customer_unique_id": entity["customer_unique_id"],
            "related_order_ids": entity["related_order_ids"],
        },
        "shipment_analysis": shipment,
        "payment_analysis": {
            "verdict": payment_verdict,
            **payment,
            "refundable_total_brl": refund,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary.upper(), "rank": 1}],
            "responsible_parties": parties,
        },
        "evidence_refs": [e["evidence_ref"] for e in inv.evidence.values()],
        "data_conflicts": conflicts[:5],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": [
                {"reason_code": primary, "amount_brl": refund, "entity_id": order_id}
            ] if refund else [],
        },
        "resolution_actions": [action],
    }

    problems = verify(output, inv)
    if problems:
        output["assessment"]["confidence"] = min(confidence, 0.5)
    inv.emit(
        "verification_completed", "verifier",
        decision_code="PASS" if not problems else "FAIL_" + problems[0],
        evidence_refs=output["evidence_refs"][:20] or None,
        attributes={
            "failed_tools": ",".join(inv.failed) or None, "problems": len(problems),
            "a2a_task_id": inv.task_id("verifier"),
        },
    )
    inv.handoff("verifier", "VERIFIED", target="coordinator")
    return output
