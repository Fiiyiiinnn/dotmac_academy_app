"""V6 storage-transfer caller for the Academy flip (operator tool).

SnapshotV6 carries the complete Learning storage extent (28 tables);
V1-V5 keep their frozen narrower scopes and are invoked internally by
the V6 writer. The caller therefore runs exactly one participant:
export, then import + verify, each side in one caller root transaction
with whole-catalog visibility (superuser/BYPASSRLS), as the contract
requires. Academy still owns writer fencing (downtime), identity
mapping, external reference/ACK closure, grants and source selection.

This is rehearsal/flip tooling, not runtime code.
"""

from __future__ import annotations

from uuid import UUID

from dotmac_learning import bank_transfer
from sqlalchemy import create_engine


def transfer_tenants(source_url: str, dest_url: str, tenant_ids: tuple[UUID, ...]) -> dict[str, int]:
    """Export SnapshotV6 from source, import+verify into dest.

    Returns inserted-row totals per destination table. Raises on any
    contract refusal; the destination transaction rolls back whole.
    """
    source = create_engine(source_url)
    dest = create_engine(dest_url)
    try:
        with source.connect() as conn, conn.begin():
            snapshot = bank_transfer.export_snapshot_v6(conn, tenant_ids)
        with dest.connect() as conn, conn.begin():
            observation = bank_transfer.import_snapshot_v6(conn, snapshot)
            bank_transfer.verify_snapshot_v6(conn, snapshot)
        return {row.table: row.count for row in observation.inserted_rows}
    finally:
        source.dispose()
        dest.dispose()
