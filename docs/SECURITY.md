# Local secrets and credentials

Google Vision credentials are local-only files. Store the real service-account JSON outside Git, for example at `config/secret/google-vision.json`, and point `GOOGLE_VISION_CREDENTIAL` in `.env` to that path.

To configure a development machine:

1. Copy `config/secret/google-vision.example.json` to a local credential file.
2. Replace every placeholder with credentials obtained from the approved secret-management process.
3. Set `GOOGLE_VISION_CREDENTIAL` in the local `.env` file to that file's path.

Never commit service-account JSON, `.env` files, private keys, certificates, API keys, database passwords, or `MASTER_KEY` values. If a service-account key was committed or shared through Git, chat, CI logs, or a remote repository, revoke and rotate it immediately. Removing a file from the latest tree does not remove it from historical commits; any history rewrite requires separate approval.
