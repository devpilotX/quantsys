# Operations runbook

Procedures for running quantsys on the VPS. No secret values belong here:
secrets live in `/opt/quant/deploy/.env` on the VPS (mode 600) and nowhere in
the repository. [DEPLOY.md](DEPLOY.md) covers the first deploy and
[GOLIVE.md](GOLIVE.md) the steps before live money.

## Status

| Item | State | Since |
|---|---|---|
| Repository free of secrets (tree and history) | verified with `git grep`, `git log -S` and gitleaks | 2026-06-17 |
| Angel One credential rotation | pending, blocks live trading | exposed 2026-06-11 |
| Dashboard operator TOTP | configured, login tested end to end over TLS | 2026-06-12 |
| On-box database dumps | every 6 hours, 14 kept | 2026-06-17 |
| Restore drill | passed | 2026-06-17 |
| Off-box backup | daily encrypted dump to the operator's Telegram chat (`quant-offbox.timer`) | 2026-07-03 |

## Rotate the Angel One credentials

Required before any live money. The credentials were briefly public on
2026-06-11 ([SECURITY.md](../SECURITY.md)); they never entered this repository,
so no history rewrite is needed, only rotation. These steps happen in Angel
One's own portals and need the account holder:

1. SmartAPI dashboard: regenerate the app's API key, which invalidates the old
   one.
2. Angel One app or web: change the login PIN.
3. Disable and re-enrol the authenticator to get a new TOTP secret.
4. On the VPS, edit `/opt/quant/deploy/.env`: `ANGEL_API_KEY`, `ANGEL_PASSWORD`
   (the new PIN) and `ANGEL_TOTP_SECRET`. `ANGEL_CLIENT_CODE` does not change.
   Keep the file at mode 600.
5. Restart the engine:
   `cd /opt/quant/deploy && sudo docker compose --profile paper up -d engine-paper`.
6. Check the engine log for `Angel One connected`.
7. Confirm the SmartAPI app's allowed IP still matches the VPS's public IP
   (`WHITELISTED_IP` in `deploy/scripts/preflight.py`, which checks it).

## Alerts

`qsdash/notify.py` sends alerts to Telegram when `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` are set in `deploy/.env`; every alert is also stored and
shown on the dashboard's logs page whether or not delivery works. The daily
self-check pushes RED and WARN results to the same chat.

In June 2026 `api.telegram.org` was unreachable from the VPS while other
egress worked, so a delivery failure there is a network problem before it is a
configuration one. To set up a bot from scratch:

1. Message @BotFather, send `/newbot`, and keep the bot token.
2. Send the new bot any message, then read your chat id from
   `https://api.telegram.org/bot<token>/getUpdates`.
3. Set both values in `/opt/quant/deploy/.env`.
4. `sudo docker compose --profile paper up -d engine-paper api`.
5. Restart the engine; it raises an `engine_start` alert, which should arrive.

## Dashboard operator login

Every operator has TOTP (`users.totp_secret` is NOT NULL). Consecutive failed
logins lock the account for a cooldown; a password reset clears the lock:

```bash
cd /opt/quant/deploy
sudo docker compose run --rm api python -m qsdash.cli reset-password --username NAME
```

## Backups

On-box dumps run every 6 hours and the newest 14 are kept in `deploy/backups/`.
`quant-offbox.timer` runs `deploy/scripts/offbox_backup.sh` daily at 20:00 IST:
a fresh `pg_dump`, encrypted with `BACKUP_PASSPHRASE` from `deploy/.env`, sent
to the operator's Telegram chat. The script's header has the decrypt command.
`selfcheck.sh` goes RED if the last off-box backup is older than 26 hours.

To repeat the restore drill against a scratch database:

```bash
cd /opt/quant/deploy; D=$(ls -t backups/*.dump | head -1)
sudo docker compose exec -T postgres psql -U quantsys -d postgres -c "CREATE DATABASE qs_restore_test;"
sudo docker compose exec -T postgres pg_restore -U quantsys -d qs_restore_test --no-owner < "$D"
sudo docker compose exec -T postgres psql -U quantsys -d qs_restore_test -c "SELECT count(*) FROM decisions;"
sudo docker compose exec -T postgres psql -U quantsys -d postgres -c "DROP DATABASE qs_restore_test;"
```

A production restore overwrites the live database:
`deploy/scripts/restore.sh backups/<file>.dump`.
