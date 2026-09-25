import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import (
    Investigation,
    analyze_payment,
    analyze_shipment,
    choose_issue,
    owned_by,
    select_record,
    unique,
)


def row(purchase: str, carrier: str, delivered: str | None, estimated: str, status="delivered"):
    return {
        "order_status": status,
        "order_purchase_timestamp": f"{purchase}T09:00:00-03:00",
        "order_approved_at": f"{purchase}T10:00:00-03:00",
        "order_delivered_carrier_date": f"{carrier}T09:00:00-03:00",
        "order_delivered_customer_date": delivered and f"{delivered}T09:00:00-03:00",
        "order_estimated_delivery_date": f"{estimated}T09:00:00-03:00",
    }


def test_seller_delay_uses_record_before_case_open() -> None:
    decoy = row("2018-05-02", "2018-05-04", None, "2018-05-12", status="canceled")
    real = row("2017-12-29", "2018-01-05", "2018-01-12", "2018-01-08")
    rows = [decoy, real]
    record = select_record(rows, datetime.fromisoformat("2018-01-10T09:00:00-03:00"))
    assert record is real
    mine = owned_by(rows, record)
    items = [
        {"order_item_id": "i", "seller_id": "s1", "shipping_limit_date": d,
         "price": "79.00", "freight_value": f}
        for d, f in (("2018-05-05T09:00:00-03:00", "10.00"), ("2018-01-01T09:00:00-03:00", "18.00"))
    ]
    items = [i for i in items if mine(i["shipping_limit_date"])]
    assert [i["freight_value"] for i in items] == ["18.00"]
    shipment, conflict = analyze_shipment(record, items, [])
    assert shipment["verdict"] == "seller_delay" and shipment["late_seller_ids"] == ["s1"]
    assert conflict is None


def test_payment_split_vs_duplicate_and_issue_choice() -> None:
    items = [{"price": "79.00", "freight_value": "10.00"}]
    split = [{"event_type": "captured", "amount_brl": "44.50", "status": "confirmed"}] * 2
    dup = [{"event_type": "captured", "amount_brl": "64.00", "status": "confirmed"}] * 2
    assert analyze_payment(items, split, [])[1] == {"valid_split_payment"}
    assert analyze_payment(items, dup, [])[1] == {"duplicate_charge"}
    failed = [{"event_type": "refund_requested", "amount_brl": "52.00", "status": "failed"}]
    assert analyze_payment(items, [], failed)[1] == {"refund_failed"}
    assert choose_issue("late_delivery_seller", set()) == ("unsupported_claim", 0.7)
    assert choose_issue("refund_failed", {"refund_failed"})[0] == "refund_failed"


def test_record_must_be_due_before_case_open() -> None:
    not_due = row("2018-08-14", "2018-08-16", "2018-08-23", "2018-08-24")
    overdue = row("2018-08-05", "2018-08-07", "2018-08-20", "2018-08-15")
    rows = [not_due, overdue]
    record = select_record(rows, datetime.fromisoformat("2018-08-17T09:00:00-03:00"))
    assert record is overdue
    # delivery event shares the overdue record's exact timestamp, despite the later purchase
    assert owned_by(rows, record)("2018-08-20T09:00:00-03:00")


def test_split_found_among_extra_captures() -> None:
    items = [{"price": "79.00", "freight_value": "10.00"}]
    events = [
        {"event_type": "captured", "amount_brl": a, "status": "confirmed"}
        for a in ("52.00", "44.50", "44.50")
    ]
    payment, issues = analyze_payment(items, events, [])
    assert issues == {"valid_split_payment"} and payment["captured_total_brl"] == 89.0


def test_duplicate_record_rows_are_not_a_duplicate_charge() -> None:
    items = [{"price": "89.00", "freight_value": "0.00"}]
    same = {"event_type": "captured", "amount_brl": "89.00", "status": "confirmed",
            "event_at": "2018-07-27T10:00:00-03:00"}
    later = {**same, "event_at": "2018-07-27T11:00:00-03:00"}
    assert analyze_payment(items, unique([same, dict(same)]), [])[1] == set()
    assert analyze_payment(items, unique([same, later]), [])[1] == {"duplicate_charge"}


def test_agent_card_blocks_tools_outside_its_skill(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    trace = TraceWriter(tmp_path / "trace.jsonl", Contracts(root / "contracts" / "schemas"))
    inv = Investigation({"case_id": "CASE_001"}, gateway=None, trace=trace)
    with pytest.raises(PermissionError):  # raised before the gateway is touched
        asyncio.run(inv.fetch("shipment-agent", "get_policy", policy_version="EC_POLICY_V2"))
