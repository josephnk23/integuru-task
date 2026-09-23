#!/usr/bin/env python3
"""Standalone Kalix Health client: create a patient and add a medication note.

1. Creates a client via POST /clients (Clients → New → Client → Save)
2. Adds medication text via PUT /clients/{id}/notes (Edit Important Notes → Save)

Auth matches the browser: form login to /authproxy/auth/login, then
Authorization: Session {KAuth}:{KVer} against the regional API from currentuser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests

APP_ORIGIN = "https://app.kalixhealth.com"
AUTH_LOGIN = f"{APP_ORIGIN}/authproxy/auth/login"
CURRENT_USER = f"{APP_ORIGIN}/authproxy/currentuser"

# Messaging method enum from Kalix meta/enums channelTypes:
# 0=SMS, 1=Text-to-voice, 2=Email, 3=Fax — UI default comes from reminder settings.
DEFAULT_MESSAGING_FALLBACK = [3]


class KalixError(Exception):
    """Raised when Kalix returns an error or the response is unexpected."""

    def __init__(self, message: str, *, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, value = s.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _require(value: str | None, name: str) -> str:
    if value is None or not str(value).strip():
        raise KalixError(f"{name} is required")
    return str(value).strip()


def _validate_email(email: str) -> str:
    email = email.strip()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise KalixError(f"invalid email: {email}")
    return email


def _form_date(value: str | None, *, label: str = "date") -> str | None:
    """Match the new-client form date output: dayjs format YYYY-MM-DD.

    The UI field is typed as MM/DD/YYYY. The form's date output (`cs`) sends
    YYYY-MM-DD, or null when the field is empty.
    """
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    parsed = None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            break
        except ValueError:
            continue
    if parsed is None:
        raise KalixError(f"{label} must be MM/DD/YYYY")
    return parsed.isoformat()


def _dob_shows_adjusted(iso_date: str | None) -> bool:
    """The prematurity checkbox is rendered only when DOB is under 2 years."""
    if not iso_date:
        return False
    born = datetime.strptime(iso_date, "%Y-%m-%d").date()
    today = date.today()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return 0 <= years < 2


def _blank(value: str | None) -> str:
    return "" if value is None else str(value).strip()


def _address_parts(
    street1: str | None,
    street2: str | None,
    street3: str | None,
    suburb: str | None,
    postcode: str | None,
    address_state: str | None,
    country: str | None,
) -> dict[str, str | None] | None:
    parts: dict[str, str | None] = {
        "street1": _blank(street1) or None,
        "street2": _blank(street2) or None,
        "street3": _blank(street3) or None,
        "suburb": _blank(suburb) or None,
        "postcode": _blank(postcode) or None,
        "state": _blank(address_state) or None,
        "country": _blank(country) or None,
    }
    if not any(parts.values()):
        return None
    return parts


def _medication_line(medication: str, dose: str | None, frequency: str | None) -> str:
    parts = [medication.strip()]
    if dose and str(dose).strip():
        parts.append(str(dose).strip())
    if frequency and str(frequency).strip():
        parts.append(str(frequency).strip())
    return "Medications: " + " ".join(parts)


def _compose_notes(prior: str | None, line: str) -> str:
    """The notes editor loads the current text and saves the whole field."""
    existing = (prior or "").strip()
    if not existing:
        return line
    return f"{existing}\n{line}"


def _response_error(resp: requests.Response) -> KalixError:
    body: Any
    try:
        body = resp.json()
    except Exception:
        body = (resp.text or "")[:1000]

    message = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = err.get("message") or err.get("Message")
        elif isinstance(err, str):
            message = err
        message = message or body.get("detail") or body.get("title") or body.get("message")
    if not message:
        message = f"HTTP {resp.status_code}"
    return KalixError(message, status_code=resp.status_code, body=body)


class KalixClient:
    def __init__(self, email: str, password: str, *, timeout: float = 30.0):
        self.email = email
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": APP_ORIGIN,
                "Referer": f"{APP_ORIGIN}/",
            }
        )
        self.api_base: str | None = None
        self._auth_header: str | None = None
        self.user: dict[str, Any] | None = None
        self._enum_cache: dict[str, Any] | None = None

    def login(self) -> dict[str, Any]:
        # Browser hits the SPA first; keep the same entrypoint.
        home = self.session.get(APP_ORIGIN + "/", timeout=self.timeout)
        if home.status_code >= 400:
            raise _response_error(home)

        # Browser sends multipart FormData fields Email + Password (not JSON).
        login = self.session.post(
            AUTH_LOGIN,
            files={
                "Email": (None, self.email),
                "Password": (None, self.password),
            },
            timeout=self.timeout,
        )
        if login.status_code == 429:
            raise KalixError(
                "too many login attempts; wait and retry",
                status_code=429,
                body=login.text,
            )
        if login.status_code == 400:
            raise KalixError("invalid email or password", status_code=400, body=login.text)
        if login.status_code >= 400:
            raise _response_error(login)

        try:
            payload = login.json()
        except Exception as exc:
            raise KalixError(f"login returned non-JSON body: {exc}", status_code=login.status_code)

        # Successful browser login returns {"isRedirect": true, ...}
        if isinstance(payload, dict) and payload.get("isRedirect") is False:
            raise KalixError("login rejected", status_code=login.status_code, body=payload)

        kauth = self.session.cookies.get("KAuth")
        kver = self.session.cookies.get("KVer")
        if not kauth or not kver:
            raise KalixError(
                "login did not set KAuth/KVer session cookies",
                status_code=login.status_code,
                body=payload,
            )

        self._auth_header = f"Session {kauth}:{kver}"
        user = self._get(CURRENT_USER, use_api=False)
        if not isinstance(user, dict) or "auth" not in user:
            raise KalixError("currentuser response missing auth", body=user)

        auth = user["auth"]
        endpoint = (auth.get("endpoint") or "").rstrip("/")
        if not endpoint:
            raise KalixError("currentuser.auth.endpoint missing", body=user)

        # Prefer tokens from currentuser when present (same values as cookies).
        session_id = auth.get("sessionId") or kauth
        verification = auth.get("verification") or kver
        self._auth_header = f"Session {session_id}:{verification}"
        self.api_base = endpoint
        self.user = user
        return user

    def _headers(self) -> dict[str, str]:
        if not self._auth_header:
            raise KalixError("not logged in")
        return {
            "Authorization": self._auth_header,
            "Content-Type": "application/json",
        }

    def _url(self, path: str, *, use_api: bool = True) -> str:
        if path.startswith("http"):
            return path
        if use_api:
            if not self.api_base:
                raise KalixError("API base URL unknown; login first")
            return f"{self.api_base}{path if path.startswith('/') else '/' + path}"
        return path if path.startswith("http") else f"{APP_ORIGIN}{path}"

    def _get(self, path: str, *, use_api: bool = True, params: dict | None = None) -> Any:
        resp = self.session.get(
            self._url(path, use_api=use_api),
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
        )
        return self._parse(resp)

    def _post(self, path: str, body: dict[str, Any], *, use_api: bool = True) -> Any:
        resp = self.session.post(
            self._url(path, use_api=use_api),
            headers=self._headers(),
            json=body,
            timeout=self.timeout,
        )
        return self._parse(resp)

    def _put(self, path: str, body: dict[str, Any], *, use_api: bool = True) -> Any:
        resp = self.session.put(
            self._url(path, use_api=use_api),
            headers=self._headers(),
            json=body,
            timeout=self.timeout,
        )
        return self._parse(resp)

    def _parse(self, resp: requests.Response) -> Any:
        if resp.status_code >= 400:
            raise _response_error(resp)
        if not resp.content:
            return None
        try:
            data = resp.json()
        except Exception as exc:
            raise KalixError(f"non-JSON response: {exc}", status_code=resp.status_code, body=resp.text)

        # Some Kalix failures still return HTTP 200 with an error object.
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and (err.get("message") or err.get("Message")):
                raise KalixError(
                    err.get("message") or err.get("Message"),
                    status_code=resp.status_code,
                    body=data,
                )
            if isinstance(err, str) and err.strip():
                raise KalixError(err, status_code=resp.status_code, body=data)
        return data

    def default_messaging_methods(self) -> list[int]:
        """Mirror UI: new clients get reminderSettings.defaultMethods."""
        try:
            settings = self._get("/reminders/settings")
            methods = settings.get("defaultMethods") if isinstance(settings, dict) else None
            if isinstance(methods, list) and methods:
                return [int(m) for m in methods]
        except KalixError:
            pass
        return list(DEFAULT_MESSAGING_FALLBACK)

    def enums(self) -> dict[str, Any]:
        if self._enum_cache is None:
            data = self._get("/meta/enums", params={"culture": "en-US"})
            self._enum_cache = data if isinstance(data, dict) else {}
        return self._enum_cache

    def _enum_id(self, enum_name: str, value: str | None, *, label: str) -> str | int:
        """Select fields are stored as enum ids. Accept the id or its label."""
        if value is None or not str(value).strip():
            return ""
        raw = str(value).strip()
        options = self.enums().get(enum_name)
        if not isinstance(options, dict) or not options:
            raise KalixError(f"Kalix did not return the {enum_name} list")
        if raw in options:
            return int(raw)
        lowered = raw.casefold()
        matches = [
            int(key)
            for key, text in options.items()
            if str(text).strip() and str(text).casefold() == lowered
        ]
        if len(matches) == 1:
            return matches[0]
        known = ", ".join(f"{key}={text}" for key, text in options.items() if str(text).strip())
        raise KalixError(f"{label} must be an id or label from {enum_name}: {known}")

    def portal_enabled(self) -> bool:
        """The invite checkbox is rendered only when the org portal is enabled."""
        try:
            settings = self._get("/clients/portal/settings")
        except KalixError:
            return False
        return bool(isinstance(settings, dict) and settings.get("enabled"))

    def create_patient(
        self,
        *,
        first_name: str,
        last_name: str,
        title: str | None = None,
        preferred_name: str | None = None,
        external_id: str | None = None,
        email: str | None = None,
        dob: str | None = None,
        adjusted_dob: str | None = None,
        gender: str | None = None,
        gender_identity: str | None = None,
        pronouns: str | None = None,
        phone: str | list[str] | None = None,
        phone_type: int = 0,
        timezone: str | None = None,
        occupation: str | None = None,
        medicare_number: str | None = None,
        medicare_ref: str | None = None,
        concession_type: str | None = None,
        concession_card: str | None = None,
        health_fund: str | None = None,
        membership_number: str | None = None,
        messaging_methods: list[int] | None = None,
        send_portal_invite: bool | None = None,
        street1: str | None = None,
        street2: str | None = None,
        street3: str | None = None,
        suburb: str | None = None,
        postcode: str | None = None,
        address_state: str | None = None,
        country: str | None = None,
        billing_same_as_postal: bool = True,
        billing_street1: str | None = None,
        billing_street2: str | None = None,
        billing_street3: str | None = None,
        billing_suburb: str | None = None,
        billing_postcode: str | None = None,
        billing_state: str | None = None,
        billing_country: str | None = None,
        residential_same_as_postal: bool = True,
        residential_street1: str | None = None,
        residential_street2: str | None = None,
        residential_street3: str | None = None,
        residential_suburb: str | None = None,
        residential_postcode: str | None = None,
        residential_state: str | None = None,
        residential_country: str | None = None,
        medication: str | None = None,
        dose: str | None = None,
        frequency: str | None = None,
    ) -> dict[str, Any]:
        first_name = _require(first_name, "first_name")
        last_name = _require(last_name, "last_name")
        email_value = _validate_email(email) if email else ""
        dob_value = _form_date(dob, label="date of birth")
        # Adjusted DOB is a hidden field until DOB is under 2 and the checkbox is on.
        # The checkbox itself (useAdjustedDob) is deleted before submit.
        if dob_value and _dob_shows_adjusted(dob_value) and adjusted_dob:
            adjusted_value = _form_date(adjusted_dob, label="adjusted date of birth")
        else:
            adjusted_value = None

        # sendPortalInvite = isNew && email && checkbox.
        # Typing an email checks the box only when the portal is enabled.
        # No email, or portal off, forces the body flag false.
        if not email_value or not self.portal_enabled():
            portal_invite = False
        elif send_portal_invite is None:
            portal_invite = True
        else:
            portal_invite = bool(send_portal_invite)

        raw_phones = phone if isinstance(phone, list) else ([phone] if phone else [])
        numbers: list[dict[str, Any]] = []
        for raw in raw_phones:
            number = _blank(raw)
            if number:
                numbers.append({"number": number, "type": int(phone_type)})

        if messaging_methods:
            methods = [int(m) for m in messaging_methods]
        else:
            methods = self.default_messaging_methods()

        # Match the payload the UI posts from the new-client form (blank optionals
        # are empty strings / null, empty phone rows are filtered out).
        body: dict[str, Any] = {
            "numbers": numbers,
            "insurers": [],
            "billingSameAsPostal": bool(billing_same_as_postal),
            "residentialSameAsPostal": bool(residential_same_as_postal),
            "messagingMethods": methods,
            "name": {
                "title": self._enum_id("titleType", title, label="title"),
                "givenName": first_name,
                "lastName": last_name,
            },
            "externalId": _blank(external_id),
            "gender": self._enum_id("gender", gender, label="gender"),
            "dateOfBirth": dob_value,
            "adjustedDob": adjusted_value,
            "timezone": _blank(timezone),
            "email": email_value,
            "occupation": _blank(occupation),
            "medicareNumber": _blank(medicare_number),
            "medicareRefNumber": _blank(medicare_ref),
            "concessionCardNumber": _blank(concession_card),
            "concessionType": self._enum_id("concessionType", concession_type, label="concession type"),
            "healthFund": _blank(health_fund),
            "membershipNumber": _blank(membership_number),
            "preferredName": _blank(preferred_name),
            "genderIdentity": _blank(gender_identity),
            "pronouns": _blank(pronouns),
            "sendPortalInvite": portal_invite,
        }

        postal = _address_parts(street1, street2, street3, suburb, postcode, address_state, country)
        if postal:
            body["postalAddress"] = postal

        # A false same-as flag is stored only when that address object is in the body.
        if not billing_same_as_postal:
            billing = _address_parts(
                billing_street1,
                billing_street2,
                billing_street3,
                billing_suburb,
                billing_postcode,
                billing_state,
                billing_country,
            )
            if not billing:
                raise KalixError(
                    "a billing address is required when billing is not the same as postal"
                )
            body["billingAddress"] = billing
        if not residential_same_as_postal:
            residential = _address_parts(
                residential_street1,
                residential_street2,
                residential_street3,
                residential_suburb,
                residential_postcode,
                residential_state,
                residential_country,
            )
            if not residential:
                raise KalixError(
                    "a residential address is required when residential is not the same as postal"
                )
            body["residentialAddress"] = residential

        created = self._post("/clients", body)
        if not isinstance(created, dict) or not created.get("id"):
            raise KalixError("create client response missing id", body=created)

        result = {
            "client_id": created["id"],
            "patient_id": created.get("patientId"),
            "name": created.get("name"),
            "request_body": body,
            "created": created,
        }

        if medication and str(medication).strip():
            result["medication"] = self.add_medication(
                created["id"],
                medication,
                dose=dose,
                frequency=frequency,
            )

        return result

    def add_medication(
        self,
        client_id: str,
        medication: str,
        *,
        dose: str | None = None,
        frequency: str | None = None,
    ) -> dict[str, Any]:
        """Record a medication on the client via Important Notes.

        Endpoint: PUT /clients/{id}/notes  body {"notes": "..."}.
        Dose and frequency are optional pieces of that single notes string.
        """
        client_id = _require(client_id, "client_id")
        medication = _require(medication, "medication")
        line = _medication_line(medication, dose, frequency)

        existing = self.get_client(client_id)
        prior = existing.get("notes") if isinstance(existing, dict) else ""
        notes_body = _compose_notes(prior if isinstance(prior, str) else "", line)
        payload = {"notes": notes_body}

        self._put(f"/clients/{client_id}/notes", payload)
        verified = self.get_client(client_id)
        notes = verified.get("notes") if isinstance(verified, dict) else None
        if notes != notes_body:
            raise KalixError(
                "medication note was not persisted on the client",
                body={"expected": notes_body, "notes": notes},
            )
        return {
            "client_id": client_id,
            "notes": notes,
            "medication_line": line,
            "request_body": payload,
            "endpoint": f"PUT /clients/{client_id}/notes",
        }

    def get_client(self, client_id: str) -> dict[str, Any]:
        data = self._get(f"/clients/{_require(client_id, 'client_id')}")
        if not isinstance(data, dict):
            raise KalixError("unexpected client payload", body=data)
        return data


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Create a Kalix patient and add a medication via Important Notes."
    )
    p.add_argument("--email", default=os.environ.get("KALIX_EMAIL"), help="Kalix login email")
    p.add_argument(
        "--password",
        default=os.environ.get("KALIX_PASSWORD"),
        help="Kalix login password (or set KALIX_PASSWORD)",
    )
    p.add_argument("--first-name", required=True, help="Client given name")
    p.add_argument("--last-name", required=True, help="Client last name")
    p.add_argument(
        "--title",
        default=None,
        help="Name title id or label (titleType). Example: Dr or 5",
    )
    p.add_argument("--preferred-name", default=None)
    p.add_argument("--external-id", default=None, help="Optional internal ID")
    p.add_argument("--client-email", default=None, help="Client contact email")
    p.add_argument("--dob", default=None, help="MM/DD/YYYY; sent as YYYY-MM-DD")
    p.add_argument(
        "--adjusted-dob",
        default=None,
        help="Due date. Sent only when DOB is under 2 years (prematurity checkbox)",
    )
    p.add_argument("--gender", default=None, help="Sex id or label (gender). Example: Female or 2")
    p.add_argument("--gender-identity", default=None)
    p.add_argument("--pronouns", default=None)
    p.add_argument(
        "--phone",
        action="append",
        default=None,
        help="Phone number. Repeat for more than one. Blank numbers are omitted",
    )
    p.add_argument(
        "--phone-type",
        type=int,
        default=0,
        help="phoneNumberType enum (0=Cell Phone / mobile default in UI)",
    )
    p.add_argument("--timezone", default=None)
    p.add_argument("--occupation", default=None)
    p.add_argument("--medicare-number", default=None)
    p.add_argument("--medicare-ref", default=None)
    p.add_argument(
        "--concession-type",
        default=None,
        help="Concession id or label (concessionType). Example: 1 or Health Care Card",
    )
    p.add_argument("--concession-card", default=None)
    p.add_argument("--health-fund", default=None)
    p.add_argument("--membership-number", default=None)
    p.add_argument(
        "--messaging-methods",
        default=None,
        help="Comma-separated method ids. Default is the account reminder setting",
    )
    p.add_argument(
        "--send-portal-invite",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Defaults to the form: checked when email is set and the portal is on",
    )
    p.add_argument("--street1", default=None)
    p.add_argument("--street2", default=None)
    p.add_argument("--street3", default=None)
    p.add_argument("--suburb", default=None, help="City")
    p.add_argument("--postcode", default=None)
    p.add_argument("--address-state", default=None)
    p.add_argument("--country", default=None)
    p.add_argument(
        "--billing-same-as-postal",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    p.add_argument("--billing-street1", default=None)
    p.add_argument("--billing-street2", default=None)
    p.add_argument("--billing-street3", default=None)
    p.add_argument("--billing-suburb", default=None)
    p.add_argument("--billing-postcode", default=None)
    p.add_argument("--billing-state", default=None)
    p.add_argument("--billing-country", default=None)
    p.add_argument(
        "--residential-same-as-postal",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    p.add_argument("--residential-street1", default=None)
    p.add_argument("--residential-street2", default=None)
    p.add_argument("--residential-street3", default=None)
    p.add_argument("--residential-suburb", default=None)
    p.add_argument("--residential-postcode", default=None)
    p.add_argument("--residential-state", default=None)
    p.add_argument("--residential-country", default=None)
    p.add_argument("--medication", required=True, help="Medication name to record")
    p.add_argument("--dose", default=None)
    p.add_argument("--frequency", default=None)
    p.add_argument(
        "--env-file",
        default=str(Path(__file__).resolve().parent / ".env"),
        help="Optional .env path for KALIX_EMAIL / KALIX_PASSWORD",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(Path(__file__).resolve().parent / ".env")
    parser = build_parser()
    # Allow --env-file before other args by a light pre-parse.
    pre, _ = parser.parse_known_args(argv)
    _load_dotenv(Path(pre.env_file))

    args = parser.parse_args(argv)
    try:
        email = _validate_email(_require(args.email or os.environ.get("KALIX_EMAIL"), "email"))
        password = _require(args.password or os.environ.get("KALIX_PASSWORD"), "password")

        methods = None
        if args.messaging_methods:
            methods = [int(part) for part in args.messaging_methods.split(",") if part.strip()]

        client = KalixClient(email, password)
        client.login()
        result = client.create_patient(
            first_name=args.first_name,
            last_name=args.last_name,
            title=args.title,
            preferred_name=args.preferred_name,
            external_id=args.external_id,
            email=args.client_email,
            dob=args.dob,
            adjusted_dob=args.adjusted_dob,
            gender=args.gender,
            gender_identity=args.gender_identity,
            pronouns=args.pronouns,
            phone=args.phone,
            phone_type=args.phone_type,
            timezone=args.timezone,
            occupation=args.occupation,
            medicare_number=args.medicare_number,
            medicare_ref=args.medicare_ref,
            concession_type=args.concession_type,
            concession_card=args.concession_card,
            health_fund=args.health_fund,
            membership_number=args.membership_number,
            messaging_methods=methods,
            send_portal_invite=args.send_portal_invite,
            street1=args.street1,
            street2=args.street2,
            street3=args.street3,
            suburb=args.suburb,
            postcode=args.postcode,
            address_state=args.address_state,
            country=args.country,
            billing_same_as_postal=args.billing_same_as_postal,
            billing_street1=args.billing_street1,
            billing_street2=args.billing_street2,
            billing_street3=args.billing_street3,
            billing_suburb=args.billing_suburb,
            billing_postcode=args.billing_postcode,
            billing_state=args.billing_state,
            billing_country=args.billing_country,
            residential_same_as_postal=args.residential_same_as_postal,
            residential_street1=args.residential_street1,
            residential_street2=args.residential_street2,
            residential_street3=args.residential_street3,
            residential_suburb=args.residential_suburb,
            residential_postcode=args.residential_postcode,
            residential_state=args.residential_state,
            residential_country=args.residential_country,
            medication=None,
        )
        med = client.add_medication(
            result["client_id"],
            args.medication,
            dose=args.dose,
            frequency=args.frequency,
        )
        result["medication"] = med

        # Final verification GET (not just write status codes).
        # hasSentPortalInvite flips true shortly after a create that asked for the invite.
        verified = client.get_client(result["client_id"])
        if result["request_body"].get("sendPortalInvite"):
            for _ in range(15):
                if verified.get("hasSentPortalInvite"):
                    break
                time.sleep(1)
                verified = client.get_client(result["client_id"])
        result["verified"] = {
            "id": verified.get("id"),
            "patientId": verified.get("patientId"),
            "name": verified.get("name"),
            "gender": verified.get("gender"),
            "concessionType": verified.get("concessionType"),
            "hasSentPortalInvite": verified.get("hasSentPortalInvite"),
            "billingSameAsPostal": verified.get("billingSameAsPostal"),
            "billingAddress": verified.get("billingAddress"),
            "residentialSameAsPostal": verified.get("residentialSameAsPostal"),
            "residentialAddress": verified.get("residentialAddress"),
            "notes": verified.get("notes"),
        }

        print(json.dumps(result, indent=2, default=str))
        return 0
    except KalixError as exc:
        err = {"error": str(exc)}
        if exc.status_code is not None:
            err["status_code"] = exc.status_code
        if exc.body is not None:
            err["body"] = exc.body
        print(json.dumps(err, indent=2, default=str), file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(json.dumps({"error": f"network error: {exc}"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
