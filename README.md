# Kalix Health session script

Python script that signs in to [Kalix](https://app.kalixhealth.com) and repeats the clinician app’s private calls:

1. Create a client (`POST /clients`), the same request as **Clients → New → Client → Save**.
2. Record a medication on Important Notes (`PUT /clients/{id}/notes`), the same request as **Edit Important Notes → Save**.

Important Notes is not a field on the create request. The medication is saved after the client id exists.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Put credentials in a  `.env` file, or export them in the shell:

```bash
KALIX_EMAIL=you@example.com
KALIX_PASSWORD=your-password
```

`--email` and `--password` override those values. `--env-file` selects a different file.

## Run

```bash
python kalix.py \
  --first-name Jordan \
  --last-name Example \
  --medication metformin \
  --dose "500 mg" \
  --frequency "twice daily"
```

Required on every run: `--first-name`, `--last-name`, and `--medication`. Every other flag is optional.

## `POST /clients`

Every create flag is in this table. `insurers` is always `[]`. The sections under the table explain the rows whose value depends on another field.

| Flag | Body field | When omitted |
| --- | --- | --- |
| `--title` | `name.title` | `""` |
| `--first-name` | `name.givenName` | required |
| `--last-name` | `name.lastName` | required |
| `--preferred-name` | `preferredName` | `""` |
| `--external-id` | `externalId` | `""` |
| `--client-email` | `email` | `""` |
| `--dob` | `dateOfBirth` | `null` |
| `--adjusted-dob` | `adjustedDob` | `null` |
| `--gender` | `gender` | `""` |
| `--gender-identity` | `genderIdentity` | `""` |
| `--pronouns` | `pronouns` | `""` |
| `--phone` | `numbers` | `[]` |
| `--phone-type` | `numbers[].type` | `0` |
| `--timezone` | `timezone` | `""` |
| `--occupation` | `occupation` | `""` |
| `--medicare-number` | `medicareNumber` | `""` |
| `--medicare-ref` | `medicareRefNumber` | `""` |
| `--concession-type` | `concessionType` | `""` |
| `--concession-card` | `concessionCardNumber` | `""` |
| `--health-fund` | `healthFund` | `""` |
| `--membership-number` | `membershipNumber` | `""` |
| `--messaging-methods` | `messagingMethods` | account default |
| `--send-portal-invite` | `sendPortalInvite` | `false` without an email; `true` when the email is set and the portal is on |
| `--street1`, `--street2`, `--street3`, `--suburb`, `--postcode`, `--address-state`, `--country` | `postalAddress` | omitted |
| `--billing-same-as-postal` | `billingSameAsPostal` | `true` |
| `--billing-street1`, `--billing-street2`, `--billing-street3`, `--billing-suburb`, `--billing-postcode`, `--billing-state`, `--billing-country` | `billingAddress` | omitted |
| `--residential-same-as-postal` | `residentialSameAsPostal` | `true` |
| `--residential-street1`, `--residential-street2`, `--residential-street3`, `--residential-suburb`, `--residential-postcode`, `--residential-state`, `--residential-country` | `residentialAddress` | omitted |




### Dates

`--dob` and `--adjusted-dob` accept `MM/DD/YYYY` or `YYYY-MM-DD`. The body sends `YYYY-MM-DD`. An empty date is `null`. A value that is not a real date is rejected before the request.

`adjustedDob` is included only when the date of birth is at least 0 and under 2 years old and `--adjusted-dob` is set. In every other case the body sends `null`, including when an adjusted date is supplied for a client who is 2 or older, and when the date of birth is in the future. `useAdjustedDob` is not sent.

### Select fields

`--title`, `--gender`, and `--concession-type` accept either the enum id or the label. The body sends the integer id from `GET /meta/enums?culture=en-US`.


| Flag                | Enum             | Examples                       |
| ------------------- | ---------------- | ------------------------------ |
| `--title`           | `titleType`      | `Dr` or `5`                    |
| `--gender`          | `gender`         | `Female` or `2`, `Male` or `1` |
| `--concession-type` | `concessionType` | `Health Care Card` or `1`      |


A label or id that is not in that enum is rejected before the POST. Leave the flag off and the field is `""`.

`--gender-identity` and `--pronouns` are sent as the text you pass. They are not converted to enum ids.

### Phones

Repeat `--phone` for more than one number. A blank number is omitted. No usable numbers sends `numbers: []`.

`--phone-type` is `phoneNumberType` on every kept number. The default is `0`.


| Id  | Label       |
| --- | ----------- |
| `0` | Cell Phone  |
| `1` | Home        |
| `2` | Work        |
| `3` | After Hours |
| `4` | Fax         |
| `5` | Other       |




### Portal invite

`sendPortalInvite` is decided before the create:

- No `--client-email` sends `false`. The portal settings call is skipped.
- An email address that fails the format check is rejected before the request.
- An email with the organization portal disabled sends `false`, even when `--send-portal-invite` is passed. The script reads `enabled` from `GET /clients/portal/settings`.
- An email with the portal enabled, and neither invite flag, sends `true`.
- `--send-portal-invite` or `--no-send-portal-invite` applies only when an email is present and the portal is enabled.

The create body sends `sendPortalInvite`. The client record reports the result later as `hasSentPortalInvite`.

### Messaging

`--messaging-methods` is a comma-separated list of ids, sent as `messagingMethods`. When the flag is omitted, the body uses `defaultMethods` from `GET /reminders/settings`. If that call fails or returns an empty list, the body uses `[3]`.

### Addresses

`postalAddress` is included only when at least one of `--street1`, `--street2`, `--street3`, `--suburb`, `--postcode`, `--address-state`, or `--country` is set. Blank parts inside an address object are `null`. With no postal parts, `postalAddress` is left out of the body.

`billingSameAsPostal` and `residentialSameAsPostal` are always sent. Both default to `true`.

`--no-billing-same-as-postal` requires a billing address: `--billing-street1`, `--billing-street2`, `--billing-street3`, `--billing-suburb`, `--billing-postcode`, `--billing-state`, `--billing-country`. Those parts are sent as `billingAddress`. `--no-residential-same-as-postal` uses the matching `--residential-*` flags and sends `residentialAddress`.

A false flag with no address parts is rejected before the request. Kalix stores the flag as `true` unless that address object is in the same body.

## `PUT /clients/{id}/notes`

| Flag | Body field | When omitted |
| --- | --- | --- |
| `--medication` | `notes` | required |
| `--dose` | appended inside `notes` | left out of the line |
| `--frequency` | appended inside `notes` | left out of the line |

The body is one field:

```json
{"notes": "Medications: metformin 500 mg twice daily"}
```

The line starts with `Medications:` and the medication name. `--dose` and `--frequency` are appended only when they contain text. Either flag can be omitted, and a blank value is left out.

The script loads the client’s current notes, appends the new line, and sends the full string. A client with no existing note is sent only the new line.

## Request chain

1. `POST https://app.kalixhealth.com/authproxy/auth/login` with form fields `Email` and `Password`. Sets cookies `KAuth` and `KVer`.
2. `GET https://app.kalixhealth.com/authproxy/currentuser`. The regional API base is `auth.endpoint` (for example `https://us-api.kalixhealth.com/`).
3. `GET {api}/reminders/settings` when `--messaging-methods` is omitted.
4. `GET {api}/clients/portal/settings` when `--client-email` is set.
5. `GET {api}/meta/enums?culture=en-US` when `--title`, `--gender`, or `--concession-type` is set.
6. `POST {api}/clients`.
7. `GET {api}/clients/{id}` for the current notes, then `PUT {api}/clients/{id}/notes`.

API calls send:

```text
Authorization: Session {sessionId}:{verification}
```

`sessionId` is the `KAuth` cookie. `verification` is the `KVer` cookie.

## Output

Success prints JSON and exits `0`. The payload includes `client_id`, `patient_id`, the create body, and the notes body.

Failure prints a JSON error to stderr and exits `1`. That includes rejected input, HTTP errors, and an HTTP 200 body that still contains an error. A login response of `429` means too many attempts.
