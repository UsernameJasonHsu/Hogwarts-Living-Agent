"""Deterministic diagnostic: DB row order is not resource identity."""

from types import SimpleNamespace

import pytest
from hogwarts.models import IdempotencyRecord, OutputImport, Project, Proposal, PrototypeRun
from hogwarts.reconciliation.inventory import ResourceInventory
from hogwarts.service import digest
from test_workspaces import create
from test_workspaces import workspace as workspace


@pytest.mark.parametrize("kind", ["journals", "imports"])
def test_rc1_inventory_order_independent_and_mutation_sensitive(workspace, kind):
    w = workspace
    create(w)
    with w["factory"].begin() as s:
        for name in ("a", "b"):
            s.add(
                IdempotencyRecord(
                    owner_id=w["actor"].owner_id,
                    actor_id="patch-builder.v1",
                    key="patch-builder.v1:order-" + name,
                    request_hash=digest(name),
                    response={"run_id": w["run"], "state": "COMPLETE", "content": name},
                )
            )
    inventory = ResourceInventory(w["manager"])
    with w["factory"]() as session:
        run = session.get(PrototypeRun, w["run"])
        proposal = session.get(Proposal, w["proposal"])
        project = session.get(Project, proposal.project_id)

        class OrderedSession:
            def __init__(self, reverse=False, changed=False):
                self.reverse, self.changed = reverse, changed

            def __getattr__(self, name):
                return getattr(session, name)

            def scalars(self, statement, *args, **kwargs):
                entity = statement.column_descriptions[0].get("entity")
                if entity is OutputImport:
                    values = (
                        [
                            SimpleNamespace(id="import-a", state="COMPLETE", version=1),
                            SimpleNamespace(id="import-b", state="COMPLETE", version=2),
                        ]
                        if kind == "imports"
                        else []
                    )
                    if self.changed and kind == "imports":
                        values[0].version = 3
                else:
                    values = list(session.scalars(statement, *args, **kwargs))
                    if self.changed and entity is IdempotencyRecord and kind == "journals":
                        values = [
                            SimpleNamespace(
                                id=x.id, key=x.key, response={**x.response, "content": "changed"}
                            )
                            if x.key == "patch-builder.v1:order-a"
                            else x
                            for x in values
                        ]
                if self.reverse and (
                    (kind == "journals" and entity is IdempotencyRecord)
                    or (kind == "imports" and entity is OutputImport)
                ):
                    values.reverse()
                return iter(values)

        first = inventory.inspect(OrderedSession(), run, proposal, project)
        reordered = inventory.inspect(OrderedSession(reverse=True), run, proposal, project)
        assert first == reordered, (
            "Identical rows in different order must not change cleanup identity"
        )
        changed = inventory.inspect(OrderedSession(changed=True), run, proposal, project)
        assert first != changed, "Actual journal/import mutations must still invalidate identity"
