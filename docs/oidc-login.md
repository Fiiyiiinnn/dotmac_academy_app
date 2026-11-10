# Academy Keycloak (OIDC) staff login

Scope of this release: **staff pilot** (admins and instructors) against
the existing `dotmac` Keycloak realm, beside — never replacing — local
password login. Students are not in the pilot cohort.

## How it works

1. `POST /login/oidc` starts a server-side ceremony
   (`dotmac-auth-oidc` Authorization Code + PKCE S256) and redirects the
   browser to Keycloak. The ceremony row lives in
   `academy_oidc_login_states`; the state value in the browser is an
   opaque lookup key and is also held in an HttpOnly cookie.
2. Keycloak redirects to `GET /login/oidc/callback`. Academy verifies
   the ID token through the package, then asks the Kernel's
   `finalize_external_login` which local Party the verified
   `(issuer, subject)` is explicitly bound to.
3. Academy re-checks the staff cohort and mints its **own** session
   cookie in the same transaction, stamped with the binding id
   (`auth_sessions.external_identity_binding_id`) so disabling the
   binding revokes exactly those sessions.

Hard rules: no just-in-time provisioning, no matching by email, no
provider roles/groups/scopes read as authorization. Bindings are an
explicit operator act with recorded evidence.

## Deployment configuration (environment only)

| Variable | Default | Notes |
|---|---|---|
| `OIDC_ENABLED` | `false` | Master gate. Off = no button, routes refuse. |
| `OIDC_ISSUER` | `https://idp.dotmac.io/realms/dotmac` | Permanent issuer. Changing it orphans bindings. |
| `OIDC_CLIENT_ID` | `dotmac-academy` | Dedicated confidential client. |
| `OIDC_CLIENT_SECRET` | — | Secret. Held like `SMTP_PASSWORD`; never a stored setting. |
| `OIDC_REDIRECT_URL` | `https://academy.dotmac.io/login/oidc/callback` | Must match the client registration exactly. |
| `OIDC_PROVIDER_BINDING` | `dotmac-keycloak` | Local registration name written on bindings. |
| `OIDC_STAFF_ROLES` | `instructor,admin` | Pilot cohort role slugs. |
| `OIDC_CEREMONY_TTL_SECONDS` | `600` | Ceremony lifetime. |
| `OIDC_HTTP_TIMEOUT_SECONDS` | `10` | Every provider call is bounded. |

Startup refuses an enabled-but-incomplete provider (missing secret,
non-HTTPS issuer/redirect).

## Keycloak client (operator act, before enabling)

In realm `dotmac`, create client `dotmac-academy`:

- Confidential client, standard flow only (implicit off, direct grants
  off, service accounts off).
- Redirect URI exactly `https://academy.dotmac.io/login/oidc/callback`
  — no wildcard.
- PKCE `S256` required; ID-token signing algorithm pinned `RS256`.
- Deliver the secret to the Academy host through the usual held-secret
  path (OpenBao), exposed to the app as `OIDC_CLIENT_SECRET`. Never
  commit it.

## Binding a staff member

The subject comes from the person's user record in the Keycloak admin
console. Email is only the operator's lookup key for the local party.

```bash
python -m app.cli oidc-bind \
  --tenant-slug <slug> --email <academy-email> \
  --subject <keycloak-sub> --bound-by <operator-id> \
  --reason "OIDC pilot enrolment, <evidence ref>"
python -m app.cli oidc-disable --tenant-slug <slug> --email <academy-email>
```

`oidc-bind` refuses non-staff parties. `oidc-disable` disables the
binding and revokes sessions it produced; password sessions are
untouched.

## Live proof checklist (before calling this done)

- [ ] Bound staff member completes Continue with Dotmac → Keycloak →
      Academy session with their existing access.
- [ ] Unbound Keycloak user is refused with the generic message.
- [ ] Bound student (direct kernel bind) is refused by the staff gate.
- [ ] Replayed callback is refused (single-use ceremony).
- [ ] `oidc-disable` revokes the live OIDC session; password login
      still works for the same person.
- [ ] `OIDC_ENABLED=false` removes the button and refuses the routes.

## Rollback

Set `OIDC_ENABLED=false` and restart the web role. Local password
login is unaffected at every stage. Existing OIDC sessions expire
naturally or can be cut immediately with `oidc-disable` per person.

Mailcow is a separate follow-on (its own client and immutable
subject-mapping proof); this Academy client must not be reused for it.
