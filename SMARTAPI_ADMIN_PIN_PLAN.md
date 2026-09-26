# SmartAPI Local — Simple Admin PIN Plan

## Decision

Use one PIN only for the browser Admin UI. Keep SmartAPI client authentication unchanged. This is enough for a simulation server when EC2 access is also limited by firewall/IP rules.

## Configuration

- Add `admin_pin_enabled: true` to `default.yaml` and server settings.
- Read the PIN only from environment variable `SMARTAPI_ADMIN_PIN`; never save it in YAML, SQLite, logs, audit rows, or HTML.
- When PIN protection is enabled but the environment variable is missing, keep REST/SDK APIs running but return a setup error for `/admin`.
- Default PIN protection should be enabled. Local users may explicitly disable it with `admin_pin_enabled: false`.

## Authentication Flow

1. User opens `/admin`.
2. Server redirects to `/admin/login` when no valid admin session exists.
3. User enters PIN.
4. Server compares PIN safely and creates a random in-memory session ID.
5. Browser receives an HTTP-only, SameSite cookie valid for 12 hours.
6. `/admin/logout` removes the session and cookie.
7. Server restart removes all admin sessions and requires PIN again.

## Protected Scope

- Protect every `/admin/*` page and mutation except `/admin/login`.
- Keep `/health`, SmartAPI REST routes, and SDK login unchanged.
- Keep `/local/v1/market/*` using its existing SmartAPI client token. It does not need the Admin browser cookie.
- `order_data: null` remains dummy/local. `order_data: angelone` remains an explicit configuration choice.

## Minimal Safety

- Never include the PIN in URLs.
- Do not log submitted PIN or request body.
- Compare PIN with constant-time comparison.
- Limit failed attempts per client IP, for example five attempts in five minutes.
- Use HTTPS when exposing the Admin UI outside the private network.

## Implementation Steps

1. Extend configuration validation and Admin Settings with `admin_pin_enabled`.
2. Add a small admin-session module using an in-memory token set with expiry.
3. Add `/admin/login` GET/POST and `/admin/logout` POST.
4. Add one middleware check for all protected `/admin/*` routes.
5. Add a small login template and navigation logout action.
6. Update README and plan documentation.
7. Add offline tests for redirect, invalid PIN, valid PIN, cookie access, logout, expiry/restart, missing environment PIN, and unaffected SDK routes.
8. Run smoke and full test suites before commit.

## Acceptance Criteria

- Anonymous Admin access redirects to login.
- Correct PIN opens Admin pages until logout, expiry, or restart.
- Wrong PIN never creates a session and is rate-limited.
- PIN never appears in configuration files, database, logs, responses, or audit records.
- Existing SmartAPI SDK behavior and dummy/real source selection remain unchanged.
