"""Topic names, partition counts, and creating them explicitly (auto-create is disabled on the broker)."""

from dataclasses import dataclass

from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

# Partitions = the unit of parallelism: at most ONE consumer per partition per group.
# 3 partitions → up to 3 validator workers can share the load. You can add partitions later, never remove.
PARTITIONS = 3


@dataclass(frozen=True)
class Topics:
    prefix: str = ""

    @property
    def submitted(self) -> str:  # key = receipt_id → spreads load; per-receipt ordering
        return f"{self.prefix}receipts.submitted"

    @property
    def validated(self) -> str:  # key = member_id → one member's events stay in order
        return f"{self.prefix}receipts.validated"

    @property
    def dlq(self) -> str:  # dead-letter queue: messages we gave up on, kept for inspection/replay
        return f"{self.prefix}receipts.submitted.dlq"

    def all(self) -> list[str]:
        return [self.submitted, self.validated, self.dlq]


async def ensure_topics(bootstrap: str, topics: Topics) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        new = [NewTopic(t, num_partitions=PARTITIONS, replication_factor=1)  # prod: replication_factor=3
               for t in topics.all() if t not in existing]
        if new:
            try:
                await admin.create_topics(new)
            except TopicAlreadyExistsError:  # another process created it first: fine
                pass
    finally:
        await admin.close()


async def delete_topics(bootstrap: str, topics: Topics) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        doomed = [t for t in topics.all() if t in existing]
        if doomed:
            await admin.delete_topics(doomed)
    finally:
        await admin.close()
