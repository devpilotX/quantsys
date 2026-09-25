# Security Policy

This repository contains software that can place orders against a real
brokerage account. Treat security findings here as financial, not cosmetic.

## Reporting a vulnerability

Report privately. Do not open a public issue for anything exploitable.

- GitHub private vulnerability reporting: use the **Security → Report a
  vulnerability** tab on this repository.
- Email: devpilotx@gmail.com

Please include what an attacker reaches, not only what is theoretically
wrong — a reproduction or a specific file and line is worth more than a scanner
export. Expect an acknowledgement within 7 days.

There is no bug bounty. This is a single-maintainer project.

## Supported versions

`main` only. There are no maintained release branches, and no security
backports to tags.

## Prior incident: credential exposure, 2026-06-11

Angel One API credentials were exposed in this project's history. The
consequences are still live constraints on the repository, and they explain
several rules that would otherwise look paranoid:

- **Rotation is a hard precondition for ever arming live trading.** See
  `docs/GOLIVE.md` §0. The engine runs with `QS_LIVE_ARMED=0` and the research
  verdict is closed, so nothing is at risk today, but the leaked credentials
  must be rotated before that changes — not as a task, as a gate.
- `.gitignore` carries explicit rules for `VPS_DEPLOYMENT_REPORT.*`, `*.pdf`,
  `*.docx`, `secrets/`, `deploy/backups/` and every `.env` variant. Rendered
  exports (PDF/DOCX) are ignored because they can carry the same secrets as
  their source document. Use `git add -f` only when you have read the file.
- Secret scanning runs in two places: `gitleaks` as a pre-commit hook on staged
  changes, and a `gitleaks` job in CI over full history. The pre-commit hook is
  ordered first so an author sees a credential finding before formatting noise.
- `scripts/check_no_live_arming.py` refuses any commit that sets
  `QS_LIVE_ARMED` to a truthy value in a config file, template, compose file or
  systemd unit. Every other lock in the go-live chain sits downstream of that
  flag, so a forgotten local toggle is the cheapest way to cause the most
  expensive outcome.

## Handling secrets in this project

- Real credentials live only in `.env` on the deployment host. Never in
  `.env.example`, never in YAML, never in a doc.
- `.env.example` holds names and shapes, never values.
- The TOTP seed is a credential. It is read from the environment and must not
  be logged, printed, or included in an exception message.
- Broker error objects can embed raw API responses. Scrub before logging if you
  touch that path.

## Known security-relevant limitations

Stated plainly rather than left for a reader to discover. These are tracked and
are preconditions for live arming, not accepted risks:

1. **Reconciliation is not yet a real check in the live path.** The live runner
   builds its "internal" position book by reading the broker, then diffs it
   against the broker, so the documented freeze-on-mismatch can never fire. The
   engine must maintain its own intended book from recorded fills. Until that
   lands, treat the live reconciliation guarantee as absent. Paper mode is
   unaffected (PaperBroker keeps its own book).
2. **Order idempotency is in-memory only.** The OMS and the Angel adapter hold
   their submitted-order maps in RAM and persist neither, and the adapter
   records the client-order-to-broker-order mapping only *after* a successful
   response. A process restart, or a send that succeeds while its response is
   lost, can therefore admit a duplicate order on retry.
3. **The Angel postback webhook fails open when its secret is unset**, and the
   secret defaults to empty. An unauthenticated POST can mutate order, fill and
   position rows. Set `angel_webhook_secret` before any live use.
4. **No explicit timeouts on broker HTTP calls.** A hung endpoint blocks the
   decision loop.
5. **The dashboard SQL console relies on a keyword blocklist** plus a read-only
   transaction rather than a dedicated read-only database role. It is
   authenticated and CSRF-protected, so this is defence-in-depth rather than an
   open door, but a read-only role is the correct fix.

## Reporting a research-integrity problem

Not a vulnerability, but treated with the same seriousness: if you find a
look-ahead bias, a leaked label, a survivorship artefact, or a cost that is
charged in the gate but not on the fill, open a normal issue and say so
directly. A defect that inflates a backtest is a defect in this repository's
core claim. One such bug has already been found and fixed (market impact was
charged in the cost gate but never on any fill); assume there are more.
