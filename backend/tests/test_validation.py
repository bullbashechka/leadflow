import pytest

from leadflow.crm.validation import InputError
from leadflow.crm.validation import validate_contacts
from leadflow.crm.validation import validate_text


@pytest.mark.parametrize("value", ["", " \n\t", None, 42])
def test_required_text_rejects_blank_or_non_text(value):
    with pytest.raises(InputError) as error:
        validate_text(value, "name", 100)
    assert "name" in error.value.field_errors


@pytest.mark.parametrize("field,limit", [("name", 100), ("request", 2000)])
def test_text_limit_preserves_accepted_input(field, limit):
    value = " Я" + "а" * (limit - 3) + " "
    assert validate_text(value, field, limit) == value
    with pytest.raises(InputError):
        validate_text(value + "а", field, limit)


@pytest.mark.parametrize(
    "value,kind,key",
    [
        ("+7 (701) 123-45-67", "phone", "+77011234567"),
        ("Alex@EXAMPLE.com", "email", "Alex@example.com"),
        ("@Alexander", "telegram", "alexander"),
        ("https://t.me/Alexander", "telegram", "alexander"),
        ("t.me/Alexander", "telegram", "alexander"),
        ("https://telegram.me/Alexander", "telegram", "alexander"),
    ],
)
def test_contact_formats_preserve_value(value, kind, key):
    contact = validate_contacts([value])[0]
    assert (contact.type, contact.value, contact.key) == (kind, value, key)


@pytest.mark.parametrize(
    "value",
    [
        "8 (701) 123-45-67",
        "77011234567",
        "+123",
        "+0123456789",
        "+1234567890123456",
        "+7/7011234567",
        "+٧٧٠١١٢٣٤٥٦٧",
        "alexander",
        "alex@",
        "@ab",
        "@12345",
        "https://t.me/+invite",
        "https://t.me/alexander/123",
        "https://evil.test/alexander",
        "https://t.me/alexander?start=payload",
        "",
        " \n",
        None,
        123,
    ],
)
def test_invalid_contact_is_indexed(value):
    with pytest.raises(InputError) as error:
        validate_contacts(["+77011234567", value])
    assert "contacts.1" in error.value.field_errors


@pytest.mark.parametrize("values", [[], None, "@alexander", {}])
def test_contact_list_is_required(values):
    with pytest.raises(InputError) as error:
        validate_contacts(values)
    assert "contacts" in error.value.field_errors


def test_all_contacts_are_checked_before_deduplication():
    with pytest.raises(InputError) as error:
        validate_contacts(["@alexander", "@ALEXANDER", "alex@"])
    assert "contacts.2" in error.value.field_errors


def test_deduplication_preserves_order_and_first_spelling():
    contacts = validate_contacts(
        [
            "+7 (701) 123-45-67",
            "Alex@EXAMPLE.com",
            "+77011234567",
            "@Alexander",
            "https://t.me/alexander",
            "Alex@example.com",
            "alex@example.com",
        ]
    )
    assert [contact.value for contact in contacts] == [
        "+7 (701) 123-45-67",
        "Alex@EXAMPLE.com",
        "@Alexander",
        "alex@example.com",
    ]


def test_contact_length_limit_checks_original_input():
    value = "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 57 + ".com"
    assert len(value) == 254
    assert validate_contacts([value])[0].value == value
    with pytest.raises(InputError):
        validate_contacts([" " + value])
