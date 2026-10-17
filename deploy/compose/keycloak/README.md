# `realm-scaleforge.json` notes

Realm-as-code for M2 start (LLD-01 §3, E2.3), applied by `keycloak-config-cli`
(`deploy/compose/compose.yaml`), not `--import-realm`, so client secrets stay
out of the file as `${VAR}` placeholders resolved from `.env`.

`keycloak-config-cli` deserializes this file strictly against Keycloak's own
`RealmImport`/`ClientRepresentation` model — unknown fields are a hard error,
and it has **no comment support**, which is why this file lives next to the
JSON instead of inside it (confirmed against a running Keycloak 26.0,
2026-10-18).

- **Secret placeholders use `$(VAR)`, not `${VAR}`.** `keycloak-config-cli`'s
  variable substitution defaults to the `$(...)` delimiter — `${...}` is
  Keycloak's own realm-internationalization syntax and is left untouched.
  Using `${VAR}` here silently applies with the literal placeholder string
  as the client secret (no error, no warning): confirmed by pulling a
  client-credentials token and finding `unauthorized_client`, then reading
  the applied client back from the Admin API and seeing
  `"secret": "${SF_SVC_BFF_CLIENT_SECRET}"` verbatim.

- **Service account role grants:** a client's service account is itself a
  Keycloak user (`service-account-<clientId>`). Role grants go under
  `users[].clientRoles` keyed by `serviceAccountClientId`, **not** a
  `serviceAccountClientRoles` field on the client — that field doesn't exist
  on `ClientRepresentation` and `keycloak-config-cli` rejects it.
- `sf-users-admin` / `sf-authz-admin` are granted the realm-management roles
  Keycloak ships (`view-users`, `manage-users`). The LLD's narrower "manage
  group membership for `mfa-required` only" scope needs Keycloak's
  fine-grained admin permissions feature, which isn't configured here yet
  (open item).
- The conditional-OTP/step-up authentication flow (LLD-01 §3.3, §9) is **not**
  in this file: a hand-written Keycloak authentication-flow export is easy to
  get subtly wrong, so it's deferred to a dedicated pass with interactive
  verification in the Keycloak admin console first.
- WebAuthn (open item, Product) and `private_key_jwt` for staging (open item,
  Security) are deferred per LLD-01 §17.
