"""Shared input rules for the API and bot. No provider lookups."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator

MAX_CONTACTS = 20
MAX_CONTACT_INPUTS = 100
MAX_SAFE_INTEGER = 2**53 - 1


class InputError(ValueError):
    def __init__(self, field_errors):
        self.field_errors = field_errors
        super().__init__("Invalid input")


@dataclass(frozen=True)
class ContactValue:
    type: str
    value: str
    key: str


def validate_text(value, field, limit):
    value = validate_text_content(value, field)
    if len(value) > limit:
        raise InputError({field: [f"Не более {limit} символов."]})
    return value


def validate_text_content(value, field):
    if not isinstance(value, str) or not value.strip():
        raise InputError({field: ["Заполните поле."]})
    if "\x00" in value:
        raise InputError({field: ["Удалите недопустимый нулевой символ."]})
    return value


def validate_contacts(values):
    if not isinstance(values, list) or not values:
        raise InputError({"contacts": ["Укажите хотя бы один контакт."]})
    if len(values) > MAX_CONTACT_INPUTS:
        raise InputError({"contacts": ["Передайте не более 100 элементов."]})
    errors = {}
    accepted = []
    for index, value in enumerate(values):
        field = f"contacts.{index}"
        try:
            validate_text(value, field, 254)
            accepted.append(_contact(value, field))
        except InputError as error:
            errors.update(error.field_errors)
    if errors:
        raise InputError(errors)
    unique = {}
    for contact in accepted:
        unique.setdefault((contact.type, contact.key), contact)
    if len(unique) > MAX_CONTACTS:
        raise InputError({"contacts": ["Не более 20 разных контактов."]})
    return list(unique.values())


def _contact(value, field):
    text = value.strip()
    if re.fullmatch(r"\+[0-9 ()-]+", text):
        number = re.sub(r"[ ()-]", "", text)
        if re.fullmatch(r"\+[1-9][0-9]{6,14}", number):
            return ContactValue("phone", value, number)
    username = _telegram_username(text)
    if username and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
        return ContactValue("telegram", value, username.casefold())
    try:
        EmailValidator(allowlist=[])(text)
    except ValidationError:
        pass
    else:
        local, domain = text.rsplit("@", 1)
        return ContactValue("email", value, local + "@" + domain.casefold())
    raise InputError(
        {
            field: [
                "Укажите телефон с + и кодом страны, email, @username "
                "или ссылку на Telegram-профиль.",
            ]
        }
    )


def _telegram_username(text):
    if text.startswith("@"):
        return text[1:]
    if text.startswith(("t.me/", "telegram.me/")):
        text = "https://" + text
    try:
        link = urlsplit(text)
        if (
            link.scheme not in {"http", "https"}
            or link.netloc.casefold() not in {"t.me", "telegram.me"}
            or link.query
            or link.fragment
            or "?" in text
            or "#" in text
        ):
            return None
        return link.path.removeprefix("/").removesuffix("/")
    except ValueError:
        return None
