import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from redact import redact_log_line, redact_text  # noqa: E402


def test_email_phone_name():
    out = redact_text("signup validated name=Priya Nair email=priya.nair.3fa2c1@example.com phone=+91 98123 45678")
    assert out == "signup validated name=<NAME> email=<EMAIL> phone=<PHONE>"


def test_phone_without_country_code():
    assert redact_text("phone=98234 46325") == "phone=<PHONE>"


def test_phone_inside_db_error_keeps_the_useful_part():
    msg = 'Value too long for column "PHONE CHARACTER VARYING(15)": "\'+91-+91 98123 45678\' (19)"'
    out = redact_text(msg)
    assert "98123" not in out
    assert 'Value too long for column "PHONE CHARACTER VARYING(15)"' in out
    assert "(19)" in out


def test_card_numbers_only_when_luhn_valid():
    assert redact_text("card=4111111111111111") == "card=<CARD>"
    assert redact_text("card=4111 1111 1111 1111") == "card=<CARD>"
    assert redact_text("orderId=1234567890123") == "orderId=1234567890123"  # not a valid card number


def test_name_and_address():
    out = redact_text("order validated customerId=7 customer=Manoj Menon productId=3")
    assert out == "order validated customerId=7 customer=<NAME> productId=3"
    out = redact_text("address updated customerId=7 street=12 MG Road city=Pune")
    assert out == "address updated customerId=7 street=<ADDRESS> city=Pune"


def test_large_counts_are_not_phones():
    assert redact_text("stock checked productId=46 available=1000000000") == "stock checked productId=46 available=1000000000"


def test_ids_and_numbers_survive():
    text = "order saved orderId=300001 total=1222.00 latencyMs=124 requestId=68e18399-d03"
    assert redact_text(text) == text


def test_json_log_line_redacts_every_string_field():
    line = json.dumps({
        "@timestamp": "2026-11-01T11:30:46.78+11:00",
        "message": "could not save customer email=kavya.rao.11aa22@example.com: Value too long",
        "stack_trace": "PaymentException: charge failed for card=5500005555555559",
        "requestId": "128cb24b-fd9",
    })
    out = json.loads(redact_log_line(line))
    assert out["message"] == "could not save customer email=<EMAIL>: Value too long"
    assert "<CARD>" in out["stack_trace"]
    assert out["@timestamp"] == "2026-11-01T11:30:46.78+11:00"
    assert out["requestId"] == "128cb24b-fd9"
