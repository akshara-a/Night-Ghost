import pytest
from src.payment import charge, refund, apply_discount, round_money


def test_basic_charge():
    res = charge(100.555, "usd")
    assert res["status"] == "success"
    assert res["amount"] == 100.56
    assert res["currency"] == "USD"


def test_basic_refund():
    res = refund(50.0, "tx_100")
    assert res["status"] == "refunded"
    assert res["amount"] == 50.0
