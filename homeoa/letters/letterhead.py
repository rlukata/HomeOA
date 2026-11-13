"""Association letterhead, kept in ``association.settings["letterhead"]``."""

SETTINGS_KEY = "letterhead"


def letterhead(association) -> dict:
    data = (association.settings or {}).get(SETTINGS_KEY) or {}
    lines = data.get("address_lines") or []
    return {
        "name": association.name,
        "address_lines": [str(x) for x in lines if str(x).strip()],
        "phone": str(data.get("phone") or ""),
        "email": str(data.get("email") or ""),
    }


def set_letterhead(association, *, address_lines, phone="", email="") -> None:
    settings = dict(association.settings or {})
    settings[SETTINGS_KEY] = {
        "address_lines": [line.strip() for line in address_lines if line.strip()][:4],
        "phone": phone.strip(),
        "email": email.strip(),
    }
    association.settings = settings
    association.save(update_fields=["settings"])
