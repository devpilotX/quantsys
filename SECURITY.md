# Security Policy

This repository contains software that can place orders against a real
brokerage account. Treat security findings here as financial, not cosmetic.

## Reporting a vulnerability

Report privately. Do not open a public issue for anything exploitable.

- GitHub private vulnerability reporting: use the **Security → Report a
  vulnerability** tab on this repository.
- Email: devpilotx@gmail.com

Please include what an attacker reaches, not only what is theoretically
wrong. A reproduction or a specific file and line is worth more than a scanner
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
  must be rotated before that changes: not as a task, as a gate.
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

Stated plainly rather than left for a reader to discover. These are
preconditions for live arming, not accepted risks.

The five listed here until 2026-09 are fixed:

- Reconciliation compares the engine's own book (a baseline snapshot taken
  when live trading first starts, plus every live fill recorded after it)
  with one broker read per bar. An unreadable broker halts the bar. After a
  mismatch has been explained, the `rebaseline_live_book` operator command
  takes the broker's book as the new baseline.
- Every order row is committed as `PENDING_SUBMIT` before the broker call. A
  send whose response is lost is looked up in the order book by ordertag
  before anything may send it again, and a restart reloads open orders.
  Client ids carry the year and a decision/flatten tag.
- The postback webhook refuses every request (503) when no secret is set.
  Unsigned postbacks need an explicit flag, honoured only with `ENV=dev`.
  `livegate` and `preflight.py` refuse to arm with a secret that is empty,
  shorter than 32 characters or the example placeholder.
- Every SmartAPI call carries a (3.05 s, 10 s) timeout. The SDK's own logger,
  which wrote request headers (the session JWT) and login parameters (client
  code, MPIN, TOTP) to `./logs/<date>/app.log` on every failed call, is muted,
  and our error messages mask every credential we hold.
- The SQL console runs each statement read-only (on SQLite as well), with a
  5 s timeout on Postgres, refuses the `users` and `sessions` tables and the
  functions that run SQL built at run time, and can run on a dedicated
  read-only role (`CONSOLE_DATABASE_URL`; the role SQL is in
  `qsdash/api/dbadmin.py`).

What is still open:

1. **Delivery holdings are not read.** `positions()` reads Angel's position
   book only. If settled delivery equity moves to the holdings book, the next
   day's reconciliation halts, sells of those shares are refused as shorts,
   and a flatten misses them. Check against the live API before arming.
2. **Broker behaviour the duplicate-order guard relies on is unverified.**
   That the order book carries `ordertag` and shows a just-accepted order
   within about a second, that a hyphen in the ordertag is accepted, that a
   cancelled order stays in the day's book, and the full set of order status
   strings (an unknown one blocks its symbol, which is safe but stops
   trading it). Also unverified: `CARRYFORWARD` as the product type for
   carried NFO positions, and the 120 s postback grace.
3. **The console's read-only role is not created by default.** Without
   `CONSOLE_DATABASE_URL` the console runs on the application role, which in
   `deploy/docker-compose.yml` is the Postgres bootstrap superuser, and only
   text checks stand between a query and the credential tables. In prod every
   console response then carries a warning saying so.
4. **Live does not short cash equities.** A cash-segment short cannot be
   carried overnight, so the live sizer drops any group that would net short
   an equity, whole. The factor sleeve's short side and equity pairs
   therefore do not trade live; routing shorts through stock futures is not
   implemented.
5. **Two restart corners.** The flatten attempt counter is in memory, so a
   flatten repeated in the same minute right after a restart reuses its id;
   the journal refuses to send it again and reports the symbol as blocked.
   Re-baselining while a postback is in flight counts that fill twice; the
   command's result warns when orders are still working.

## Reporting a research-integrity problem

Not a vulnerability, but treated with the same seriousness: if you find a
look-ahead bias, a leaked label, a survivorship artefact, or a cost that is
charged in the gate but not on the fill, open a normal issue and say so
directly. A defect that inflates a backtest is a defect in this repository's
core claim. One such bug has already been found and fixed (market impact was
charged in the cost gate but never on any fill); assume there are more.
