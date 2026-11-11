"""Local validation and serializers reproduced from the mobile care forms."""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from functools import lru_cache
from importlib.resources import files
from typing import Any

from .client import CfeError

FIELD_MAP = {
    "reasonCare": "reasonCare",
    "dateCare": "new_datedusoin",
    "zoneCare": "new_precisezlazone",
    "isPrescribed": "new_absencedatedescription",
    "datePrescribe": "new_datedeprescription",
    "dateStart": "new_datededebutdetraitement",
    "dateEnd": "new_datedefindetraitement",
    "amountSession": "new_nombredeseancesdispensees",
    "description": "new_descriptiondutraitement",
    "descriptionFee": "new_descriptiondesfraisfactures",
    "dateExec": "new_datedexecution",
    "dateDeliv": "new_datededelivrance",
    "targetDuration": "new_dureeprevisibleenmois",
    "transportModeDetails": "new_precisezlemodedetransport",
    "dateFirstTransport": "new_datedu1ertransport",
    "codeDevise": "new_codedevise",
    "amount": "new_montant",
    "localAmount": "new_montantprisenchargeparorganismelocal",
    "sejourStart": "new_datedebutdesejour",
    "sejourEnd": "new_datefindesejour",
}
CLAIM_KEYS = {
    "beneficiary_id",
    "country_code",
    "currency_code",
    "circumstance_code",
    "circumstance_date",
    "comment",
    "care_items",
}


@lru_cache
def forms() -> dict[str, dict]:
    return json.loads(files("cfe_mcp").joinpath("data/care_forms.json").read_text())


def care_schema(code: str, catalog: dict) -> dict:
    active = {str(row["code"]).upper(): row for row in catalog["care_types"]}
    code = code.strip().upper()
    if code not in active or code.lower() not in forms():
        raise CfeError("Code de soin inconnu ou inactif dans le catalogue CFE actuel.")
    fields = {}
    for name, definition in forms()[code.lower()].items():
        # The same checkbox is named absDatePresc in one asset, isPrescribed elsewhere.
        name = "isPrescribed" if name == "absDatePresc" else name
        fields[name] = {
            "label": definition["label"],
            "required": bool(definition.get("obligatoire")),
            "type": definition["input_type"],
            "rule": definition.get("RG", ""),
        }
    return {
        **active[code],
        "fields": fields,
        "prescription_checkbox": "isPrescribed=true signifie ABSENCE de prescription.",
        "dates": "YYYY-MM-DD",
        "amounts": "Chaînes décimales, par exemple 123.45.",
        "currency": "codeDevise est repris de currency_code, la devise facturée du dossier.",
        "schema_source": "CFE & moi 3.7.0 ; code confirmé dans le catalogue actuel",
    }


def iso_date(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CfeError(f"{label} : date attendue au format YYYY-MM-DD.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise CfeError(f"{label} : date inexistante.") from None


def decimal_string(value: Any, label: str, *, zero: bool = False, integer: bool = False) -> str:
    # Floats are intentionally rejected: use the exact amount printed on the invoice.
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise CfeError(f"{label} : utiliser une chaîne décimale exacte.")
    text = str(value).strip().replace(",", ".")
    if len(text) > 30 or not re.fullmatch(r"\d+(?:\.\d+)?", text):
        raise CfeError(f"{label} : nombre positif sans séparateur de milliers attendu.")
    number = Decimal(text)
    if number < 0 or (not zero and not number) or (integer and number != number.to_integral()):
        raise CfeError(f"{label} : valeur positive{' entière' if integer else ''} attendue.")
    return format(number, "f").rstrip("0").rstrip(".") if "." in text else str(int(number))


def normalize_care(item: Any, catalog: dict, currency: str, country: str) -> tuple[dict, dict]:
    if not isinstance(item, dict) or not isinstance(item.get("code"), str):
        raise CfeError("Chaque soin doit avoir un code du catalogue et ses champs.")
    schema = care_schema(item["code"], catalog)
    definitions = schema["fields"]
    if set(item) - {"code", *definitions}:
        raise CfeError("Le soin contient un champ non prévu par son formulaire CFE.")
    values = {k: v for k, v in item.items() if k != "code" and v is not None and v != ""}
    if values.get("codeDevise", currency) != currency:
        raise CfeError("La devise du soin doit correspondre à la devise facturée du dossier.")
    values["codeDevise"] = currency
    absence = values.get("isPrescribed", False)
    if not isinstance(absence, bool):
        raise CfeError("Absence de prescription : fournir true ou false.")
    if absence and values.get("datePrescribe"):
        raise CfeError("Choisir une date de prescription ou son absence, pas les deux.")
    normalized = {"code": schema["code"]}
    for name, definition in definitions.items():
        value = values.get(name)
        if name == "datePrescribe" and absence:
            continue
        if value is None:
            if definition["required"]:
                raise CfeError(f"{definition['label']} : champ obligatoire pour ce soin.")
            continue
        kind, label = definition["type"], definition["label"]
        if kind in {"date", "date-range"}:
            value = iso_date(value, label)
        elif kind == "checkbox":
            if not isinstance(value, bool):
                raise CfeError(f"{label} : booléen attendu.")
        elif kind == "text_form_number":
            value = decimal_string(
                value,
                label,
                zero=name == "localAmount",
                integer=name in {"amountSession", "targetDuration"},
            )
        elif not isinstance(value, str) or not value.strip():
            raise CfeError(f"{label} : texte attendu.")
        else:
            value = value.strip()
        normalized[name] = value
    for first, last in (("dateStart", "dateEnd"), ("sejourStart", "sejourEnd")):
        if first in normalized and last in normalized and normalized[first] > normalized[last]:
            raise CfeError("La date de début du soin dépasse sa date de fin.")
    if "localAmount" in normalized:
        if country == "100" and Decimal(normalized["localAmount"]):
            raise CfeError("Le montant pris en charge localement concerne les soins à l'étranger.")
        if Decimal(normalized["localAmount"]) > Decimal(normalized["amount"]):
            raise CfeError("Le montant pris en charge localement dépasse le montant facturé.")
    payload = {remote: normalized.get(local) for local, remote in FIELD_MAP.items()}
    payload["new_verification"] = schema["code"]
    payload["new_absencedatedescription"] = absence
    return normalized, payload


def normalize_claim(value: dict, catalog: dict, account: dict) -> tuple[dict, dict, dict]:
    if not isinstance(value, dict) or set(value) - CLAIM_KEYS:
        raise CfeError("Champs de demande inconnus.")
    required = {
        "beneficiary_id",
        "country_code",
        "currency_code",
        "circumstance_code",
        "care_items",
    }
    if required - set(value):
        raise CfeError("Bénéficiaire, pays, devise, circonstance et soins sont obligatoires.")
    for key in required - {"care_items"}:
        if not isinstance(value[key], str):
            raise CfeError(f"{key} : chaîne attendue.")
    people = {person["id"]: person for person in account["beneficiaries"]}
    if value["beneficiary_id"] not in people:
        raise CfeError("Ce bénéficiaire n'appartient pas au compte connecté.")
    country = value["country_code"]
    if country not in {str(row["countryCode"]) for row in catalog["countries"]}:
        raise CfeError("Pays absent du catalogue CFE.")
    currency = value["currency_code"].upper()
    if currency not in {row["currencyCode"] for row in catalog["countries"]}:
        raise CfeError("Devise absente du catalogue CFE.")
    if value["circumstance_code"] not in {str(row["value"]) for row in catalog["circumstances"]}:
        raise CfeError("Circonstance absente du catalogue CFE.")
    event_date = value.get("circumstance_date")
    if event_date is not None:
        event_date = iso_date(event_date, "Date de circonstance")
    if value["circumstance_code"] != "100000000" and not event_date:
        raise CfeError("Préciser la date liée à l'accident ou à la maternité (circumstance_date).")
    comment = value.get("comment", "")
    if not isinstance(comment, str):
        raise CfeError("Le commentaire doit être un texte.")
    suffix = "FR" if country == "100" else "ETR"
    try:
        limits = {
            "attachments": int(catalog["limits"][f"NB_PJ_MAX_{suffix}"]),
            "care_items": int(catalog["limits"][f"NB_SOINS_MAX_{suffix}"]),
        }
        if not all(0 < n <= 100 for n in limits.values()):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise CfeError("Les limites de dépôt CFE sont absentes ou invalides.") from None
    items = value["care_items"]
    if not isinstance(items, list) or not 1 <= len(items) <= limits["care_items"]:
        raise CfeError(f"Prévoir entre 1 et {limits['care_items']} soins pour ce pays.")
    normalized, remote = [], []
    for item in items:
        care, payload = normalize_care(item, catalog, currency, country)
        normalized.append(care)
        remote.append(payload)
    clean = {
        **{k: value[k] for k in required - {"care_items"}},
        "currency_code": currency,
        "circumstance_date": event_date,
        "comment": comment.strip(),
        "care_items": normalized,
    }
    incident = {
        "new_peridt": account["owner"],
        "benefIDT": clean["beneficiary_id"],
        "new_typecontact": "adh" if people[clean["beneficiary_id"]]["holder"] else "benef",
        "new_codepays": country,
        "new_codedevise": currency,
        "new_circonstance": clean["circumstance_code"],
        "new_datededemandederemboursement": event_date,
        "new_commentaire": clean["comment"],
    }
    return clean, {"incident": incident, "soins": remote}, limits
