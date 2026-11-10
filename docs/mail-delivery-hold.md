# Exclusive mail delivery hold

Admissions live acceptance creates synthetic mail intents that must remain unsent
and retain zero attempts until their verified removal. Stopping scheduled workers
alone does not exclude manual consumers or the administrator's test email route.

Every configured SMTP delivery and outbox consumer must acquire the shared mail
delivery gate. The gate is mandatory in every environment; an absent or unsafe
gate defers delivery. Enqueuing remains available. The gate uses no database
migration or additional dependency.

The fixed directory is `/var/lib/dotmac-academy/mail-delivery`. Its `lock` file is
a permanent inode; its `state` is a separately atomically replaced file containing
exactly `OPEN\n` or `HELD\n`. Root owns both regular files and the directory.
Neither the files nor their parent directories may be writable by another user.
Never replace or delete the lock inode while any sender or holder can exist.

The coordinator provisions and verifies the gate on every supported sender host,
then binds those identities into the deployment and acceptance evidence. A new
source candidate and its gate provisioning need their own reviewed deployment
packet; an earlier exact-source approval does not cover these changed bytes.
Legacy consumers must be stopped and excluded separately because they do not
implement this contract.

To establish a hold, first persist and fsync `HELD` using an atomic state-file
replacement and directory fsync. Then acquire the exclusive lock, waiting for
existing shared deliveries to finish. Readiness begins only after the exclusive
lock is held and all configured hosts, processes, and scheduled consumers are
accounted for. Existing delivery leases may finish while the coordinator drains
them. New delivery attempts must defer without changing outbox delivery fields.
Keep the exclusive lock throughout acceptance and any bounded recovery.

A holder crash releases its lock but leaves `HELD` on disk. The coordinator must
never reopen automatically on a timeout, a crash, or a failed proof. Only after
independently verifying zero owned deliverable mail and the recorded cleanup
conditions may it persist and fsync `OPEN` while still holding the exclusive
lock, then release it and restore the originally active timers.

This gate governs supported application senders using this host filesystem.
It is not a barrier against an administrator deliberately bypassing the supported
application or executing old code. The coordinator must still inventory and
exclude legacy, external, and manual consumers throughout the operation.

Hosted CI provisions its own root-owned open gate on the disposable runner.
Concurrency and failure tests use isolated gates and independent processes;
no acceptance mail is sent by the qualification suite.
