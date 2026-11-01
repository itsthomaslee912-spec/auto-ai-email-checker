from app.api.emails import _parse_recipient_addresses


def test_parse_recipient_addresses_keeps_email_after_comma_in_display_name():
    assert _parse_recipient_addresses("Aslam, Komal <Komal.Aslam@hilti.com>") == [
        "Komal.Aslam@hilti.com"
    ]


def test_parse_recipient_addresses_accepts_multiple_and_removes_duplicates():
    assert _parse_recipient_addresses(
        "first@example.com; Second Person <second@example.com>, FIRST@example.com"
    ) == ["first@example.com", "second@example.com"]


def test_parse_recipient_addresses_rejects_invalid_values():
    assert _parse_recipient_addresses("Aslam, not-an-email") == []
