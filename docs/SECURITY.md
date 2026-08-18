# Local secrets and credentials

Google Vision credentials are local-only files. Store the real service-account JSON outside Git, for example at `config/secret/google-vision.json`, and point `GOOGLE_VISION_CREDENTIAL` in `.env` to that path.

To configure a development machine:

1. Copy `config/secret/google-vision.example.json` to a local credential file.
2. Replace every placeholder with credentials obtained from the approved secret-management process.
3. Set `GOOGLE_VISION_CREDENTIAL` in the local `.env` file to that file's path.

Never commit service-account JSON, `.env` files, private keys, certificates, API keys, database passwords, or `MASTER_KEY` values. If a service-account key was committed or shared through Git, chat, CI logs, or a remote repository, revoke and rotate it immediately. Removing a file from the latest tree does not remove it from historical commits; any history rewrite requires separate approval.

## External API credential responsibilities

- `GOOGLE_GEOCODING` and `GOOGLE_PLACES` are Backend server credentials. Their
  environment fallback is `GOOGLE_API_KEY`; `GOOGLE_MAP_API_KEY` is retained
  only as the existing Settings compatibility alias.
- `WEATHER` falls back to `WEATHER_API_KEY` and is used only by the Backend
  OpenWeatherMap client.
- `ASTROMETRY` falls back to `ASTROMETRY_API_KEY` and means an Astrometry.net
  API key, not an AstronomyAPI Application ID or Secret.
- Google Maps Android/iOS SDK keys remain client deployment credentials. The
  Backend does not store, return, or provision them.
- Google Vision continues to use the local service-account file referenced by
  `GOOGLE_VISION_CREDENTIAL`; Vision JSON must not be stored in
  `common_api_keys`.

`common_api_keys` has priority over environment fallbacks when an enabled row
exists. Key values are encrypted at rest and are decrypted only inside the
Backend `KeyResolver`. List/create/update responses contain configuration
metadata and `****`, never ciphertext or plaintext.

Provider query strings, response bodies, session credentials, and API keys must
not be included in logs or public errors. Readiness endpoints expose only
configured/source booleans. Vision readiness does not expose the credential
filesystem path.

## Backend Bearer authentication

Set `TC_BACKEND_AUTH_TOKEN` to protect every `/api/*` endpoint, including API
key administration, upload and job polling, Gallery, Changes, monitoring,
AstroJournal records, and external-provider proxy endpoints. The informational
root endpoint is protected as well. Clients send:

```http
Authorization: Bearer <TC Backend token>
```

`GET /health` and `GET /db-test` remain public for Docker/NAS diagnostics and
return only minimal status data. The detailed `/api/common/health`, readiness,
and capabilities endpoints are protected. Authentication failures return `401`
with `WWW-Authenticate: Bearer`; token values and Authorization headers are
never logged or returned.

For existing LAN development, an unset or blank token leaves authentication
disabled and emits a startup warning. This compatibility mode is not safe for
Internet exposure. Generate a strong secret on the deployment host, for example:

```shell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Store only the generated value in the NAS `.env` as
`TC_BACKEND_AUTH_TOKEN=<generated secret>`. Rotate it if the value is exposed.
The repository `.env.example` intentionally contains an empty placeholder.
The bundled Folder Watcher reads the same environment variable and adds the
Bearer header without logging it.

## External NAS topology

The supported external route is:

```text
Internet :8443
  -> Synology HTTPS Reverse Proxy
  -> http://127.0.0.1:8000
  -> tc-backend Bearer authentication
```

Expose only TCP 8443 over HTTPS. Do not forward external TCP 8000 directly to
the Backend and do not expose NAS SSH port 22. Keep the NAS firewall enabled,
protect API-key/admin endpoints with the same strong token, and retain all
provider credentials server-side. Bearer authentication does not trust client
IP or `X-Forwarded-*` headers and does not replace HTTPS.
