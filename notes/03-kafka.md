# Module 3 — Kafka and event-driven processing

**How to read this file:** Part 1 is the idea, compared throughout with RabbitMQ and Celery, which you already know. Part 2 walks through the code with clickable links. Part 3 shows what happened in the live demo. Parts 4–5 are interview Q&A and a self-check.

Run it:
```bash
./scripts/dev_stack.sh               # Postgres + Kafka + API (kafka mode) + relay + 2 validators + notifier
tail -f .stack-logs/*.log            # another terminal: watch partitions, rebalances, notifications
# POST receipts at http://127.0.0.1:8000/docs, then try: kill -9 one validator and watch the other take over
docker compose exec kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server localhost:9092 --describe --all-groups
```

---

# Part 1 — The idea

## 1.1 The problem we're solving here

Since Module 1, receipt validation has run as a FastAPI `BackgroundTask`: **in the API process's memory**. Gap #2 in [00-end-to-end-flow.md](00-end-to-end-flow.md):
- If the pod is killed, restarted or deployed between the `201` response and the LLM finishing, the task is gone and the receipt is **stuck in `pending` forever**.
- The LLM work competes for CPU and connections with API requests.
- You can't add workers independently, or retry, or see a backlog.

We need durable, retryable, scalable background work. You'd normally reach for Celery + RabbitMQ; this module uses **Kafka**, and explains when each is the better fit.

## 1.2 What Kafka is: a log, not a queue

**RabbitMQ** is a *message broker*: a message sits in a queue until a consumer acknowledges it, and then it's **deleted**.

**Kafka** is a *distributed, append-only log*: messages are **appended** to a topic and **kept** for a retention period (days, or forever), whether or not anyone has read them. Consumers just remember **how far they've read** (an *offset*). Reading doesn't delete anything.

```
topic receipts.submitted, partition 0:   [ev0][ev1][ev2][ev3][ev4][ev5] ← producers append here
                                                       ▲              ▲
                                    validator group offset = 3     notifier group offset = 6
                         (each group has its OWN position; replaying = moving the offset back)
```

That one design difference explains almost everything else:
- **Many independent consumers** (validators, notifier, analytics, fraud ML) each read the same events at their own pace.
- **Replay:** a new service, or a fixed bug, can re-read history from offset 0.
- **Very high throughput:** sequential disk writes, batching, zero-copy reads.

## 1.3 Core concepts (all visible in our demo)

| Concept | What it is | In our project |
|---|---|---|
| **Topic** | A named stream of events | `receipts.submitted`, `receipts.validated`, `receipts.submitted.dlq` |
| **Partition** | A topic is split into N ordered logs. **The unit of parallelism and ordering** | 3 partitions per topic |
| **Offset** | A message's position in a partition (0, 1, 2…) | Logs show `p1@2` = partition 1, offset 2 |
| **Key** | Decides the partition (`hash(key) % N`). **Same key → same partition → ordered** | `receipt_id` for submitted, `member_id` for validated |
| **Consumer group** | Consumers sharing a `group_id` **split** the partitions. Each partition goes to exactly one member | `receipt-validator`: w1 got [0, 2], w2 got [1] |
| **Rebalance** | When a member joins or leaves, partitions are redistributed | w1 killed → w2 got [0, 1, 2] |
| **Committed offset** | "This group has finished up to here", stored in Kafka | We commit *after* processing |
| **Lag** | Log-end offset minus committed offset = backlog | 0 for both groups at the end of the demo |
| **Broker / replication** | Servers storing partitions; each partition copied to N brokers (leader + followers, the *ISR*) | Single dev broker; prod = 3 brokers, replication factor 3 |
| **KRaft** | Kafka manages its own cluster metadata (ZooKeeper has been removed) | `KAFKA_PROCESS_ROLES: broker,controller` |

**Parallelism rule:** a group can use at most as many consumers as there are partitions. With 3 partitions, a 4th validator would sit idle. Choose partition counts with growth in mind: you can add partitions later, but that changes which partition each key maps to.

## 1.4 Kafka vs RabbitMQ vs Celery

| | Celery (+ RabbitMQ/Redis) | RabbitMQ | Kafka |
|---|---|---|---|
| Model | Task queue (call a function later) | Message broker: queues and exchanges | Distributed log with retention |
| After consumption | Removed | Removed (on ack) | **Kept** until retention expires |
| Multiple consumers of the same message | Not its job | One queue per consumer type, bound to an exchange | Just another **consumer group** |
| Replay history | No | No (once acked it's gone) | **Yes**, reset offsets |
| Ordering | No guarantee | Per queue (with one consumer) | **Per partition / key** |
| Routing | Task names | **Rich**: topic, headers, fanout exchanges, priorities | Topic + key only |
| Per-message features | Retries, ETA, countdown, chains | Per-message TTL, priority, delayed delivery | None of these built in (build retry topics yourself) |
| Throughput | Moderate | High | **Very high** (millions/sec) |
| Best for | "Run this function in the background", scheduled jobs | Work queues, complex routing, RPC, low latency | **Event streaming**: many consumers, replay, ordering, audit, analytics, CDC |

**Interview line:** *"Celery or RabbitMQ when I need a work queue: run this job once, with retries, priorities or delays. Kafka when the data is a stream of business events that several systems react to, that I may need to replay, and where per-key ordering matters."* Our receipt events fit Kafka (validator, notifier and future analytics all consume them), but **Celery would also have been a fine choice** for the validation job alone.

## 1.5 Delivery guarantees

| Guarantee | How you get it | Risk |
|---|---|---|
| **At-most-once** | Commit the offset, *then* process | Crash after the commit → message **lost** |
| **At-least-once** ✅ ours | Process, *then* commit | Crash before the commit → **processed twice** |
| **Exactly-once** | Kafka transactions + idempotent producer, *only* for Kafka→Kafka pipelines | Doesn't cover side effects outside Kafka (DB writes, emails, LLM calls) |

**What everyone does in practice:** *at-least-once delivery + idempotent processing = effectively-once results.* We demonstrated it: w1 was killed mid-job, receipt 17 was delivered again to w2, and it produced **exactly one** result and one notification.

## 1.6 The dual-write problem and the transactional outbox

The naive endpoint:
```python
session.add(receipt); await session.commit()     # 1. DB
await kafka.send("receipts.submitted", event)     # 2. Kafka   ← crash here = receipt with no event: stuck forever
```
Swapping the order gives the opposite bug (an event for a receipt that was never saved). **No transaction spans Postgres *and* Kafka.**

**The fix, the transactional outbox:**
```
POST /receipts ──► ONE DB transaction:  INSERT receipt  +  INSERT outbox(topic, key, payload)
                                        (both commit, or neither)
relay process ──► SELECT unsent outbox rows … FOR UPDATE SKIP LOCKED ──► Kafka (acks=all) ──► mark sent
```
- The API **never talks to Kafka**, so it keeps accepting receipts even when Kafka is down; the relay catches up later.
- The relay can crash after sending but before marking a row, so it can **send twice**. That's at-least-once again, which is why consumers must be idempotent.
- At scale, replace the polling relay with **CDC** (Debezium reading Postgres's write-ahead log).

## 1.7 Patterns every Kafka consumer needs

| Pattern | Problem it solves | Ours |
|---|---|---|
| **Idempotent consumer** | Duplicates are guaranteed under at-least-once | Skip if not `pending`; conditional `UPDATE … WHERE status='pending'` |
| **Commit after processing** | Don't lose messages on a crash | `consumer.commit(offset + 1)` after `process()` |
| **Poison message → DLQ** | One bad message would otherwise block its partition forever (retried endlessly) | `PoisonMessage` → `receipts.submitted.dlq` with error and source headers |
| **Bounded retries with backoff** | Transient failures (DB blip) shouldn't be dead-lettered immediately | 3 attempts (waits 1s, 2s between them), then DLQ |
| **Thin events + read current state** | A replayed or late event must not overwrite newer data | Events carry ids; the worker reads the receipt from the DB |
| **Event versioning** | Other teams' consumers depend on your schema | `version` field; in prod, a schema registry (Avro/Protobuf) |
| **Graceful shutdown** | Avoid waiting ~10s for the group to notice a dead member | SIGTERM → finish, commit, `consumer.stop()` → instant rebalance |
| **No long transactions around I/O** | Holding DB locks while waiting on an LLM | Read, release, call the LLM, then a short conditional update |

---

# Part 2 — Code walkthrough (read in this order)

```
POST /receipts ─► receipts.py: receipt + outbox row, ONE transaction ─► Postgres
                                                                         │ poll
                                                   workers/relay.py ◄────┘
                                                         │ acks=all, idempotent producer
                                                         ▼
                                           Kafka: receipts.submitted (3 partitions, key=receipt_id)
                                       ┌─────────────────┴─────────────────┐
                           group receipt-validator                  (any future group)
                           w1 [0,2]        w2 [1]
                             └── workers/validator.py handle(): idempotent, LLM, conditional update
                                     + outbox row ReceiptValidated ─► relay ─► receipts.validated (key=member_id)
                                                                                 │
                                                                   group member-notifier ─► workers/notifier.py
                           poison / failed 3× ─► receipts.submitted.dlq
```

## 2.1 Infrastructure: [docker-compose.yml](../docker-compose.yml)

| Lines | What |
|---|---|
| [17-18](../docker-compose.yml#L17) | `apache/kafka:4.1.0`, the official image, KRaft mode |
| [23](../docker-compose.yml#L23) | One node acts as both broker and controller (dev only; prod = 3+ nodes) |
| [33](../docker-compose.yml#L33) | **Auto-create topics disabled**: topics are created explicitly with chosen partitions. A typo in a topic name fails instead of silently creating a new topic |

## 2.2 Topics and events

**[app/events/topics.py](../app/events/topics.py)**

| Lines | What | Why |
|---|---|---|
| [10](../app/events/topics.py#L10) | `PARTITIONS = 3` | Up to 3 validators in parallel |
| [18-27](../app/events/topics.py#L18) | Topic names, with the **key choice** commented | `receipt_id` spreads load; `member_id` keeps one member's notifications in order (and caused a hot partition in the demo, Part 3) |
| [14](../app/events/topics.py#L14) | A `prefix` | Tests use unique topics and never touch real ones |
| [33-46](../app/events/topics.py#L33) | `ensure_topics` | Idempotent: creates only missing topics and tolerates a race with another process. `replication_factor=1` in dev, 3 in prod |

**[app/events/schemas.py](../app/events/schemas.py)**

| Lines | What |
|---|---|
| [1-5](../app/events/schemas.py#L1) | **Thin events**: ids + what happened, not the whole receipt |
| [13-16](../app/events/schemas.py#L13) | Every event has an `event_id` (for dedupe and tracing), a `version` and `occurred_at` |
| [19](../app/events/schemas.py#L19), [25](../app/events/schemas.py#L25) | `ReceiptSubmitted`, `ReceiptValidated`: the contracts other teams would depend on |

## 2.3 The outbox: [app/events/outbox.py](../app/events/outbox.py)

| Lines | What | Why |
|---|---|---|
| [1-11](../app/events/outbox.py#L1) | The dual-write problem, explained | Read this first |
| [25-33](../app/events/outbox.py#L25) | `outbox` table: topic, key, payload, `published_at` | `id` increases, so events publish in commit order |
| [36-38](../app/events/outbox.py#L36) | `add_event` only `session.add`s | Nothing is sent; it commits **with the caller's transaction** |
| [45-62](../app/events/outbox.py#L45) | `relay_once` | Oldest unsent rows first; [`FOR UPDATE SKIP LOCKED`](../app/events/outbox.py#L51) lets several relays run safely; [send, wait for the ack, then mark](../app/events/outbox.py#L57); [`finally: commit`](../app/events/outbox.py#L61) keeps progress if a later send fails |

**Where it's used:** [app/routers/receipts.py:44-52](../app/routers/receipts.py#L44). In `kafka` mode: `flush()` to get the id, `add_event(...)`, then **one commit** for both rows. Otherwise it falls back to the old `BackgroundTasks`. Config: [app/config.py:28-30](../app/config.py#L28).

## 2.4 The relay: [app/workers/relay.py](../app/workers/relay.py)

| Lines | What |
|---|---|
| [24-26](../app/workers/relay.py#L24) | Producer with **`acks="all"`** (the broker confirms only when all in-sync replicas have the message) and **`enable_idempotence=True`** (the broker de-duplicates the producer's own network retries) |
| [31-41](../app/workers/relay.py#L31) | Poll every 0.5s; on an error, log and retry next tick. Events wait safely in the outbox |

## 2.5 The validator consumer group: [app/workers/validator.py](../app/workers/validator.py)

**`handle()`: one event, idempotent** ([line 37](../app/workers/validator.py#L37))

| Lines | What | Why |
|---|---|---|
| [39-42](../app/workers/validator.py#L39) | Parse the payload; invalid → `PoisonMessage` | It will never succeed, so don't retry |
| [44-50](../app/workers/validator.py#L44) | Load the receipt; missing → poison; **not `pending` → skip** | Cheap duplicate check: the LLM isn't called again |
| [52-53](../app/workers/validator.py#L52) | LLM call **outside** any transaction | Never hold DB connections or locks during slow network I/O |
| [58-64](../app/workers/validator.py#L58) | **Conditional update** `WHERE status = 'pending'`; `rowcount == 0` → another worker won | Optimistic concurrency: exactly one result even when two workers race |
| [66-69](../app/workers/validator.py#L66) | The `ReceiptValidated` event goes into the **outbox in the same transaction** as the status change | The worker has the same dual-write problem, and the same fix |

**`process()`: retries and DLQ** ([line 72](../app/workers/validator.py#L72))

| Lines | What |
|---|---|
| [74-84](../app/workers/validator.py#L74) | Poison → straight to the DLQ; other errors → up to 3 attempts with exponential backoff |
| [86-88](../app/workers/validator.py#L86) | Publish to the DLQ with `error` and `source` (topic/partition/offset) headers, so you can inspect and replay later |

**The consumer loop** ([`main`, line 103](../app/workers/validator.py#L103))

| Lines | What |
|---|---|
| [113](../app/workers/validator.py#L113) | `enable_auto_commit=False`: **we** decide when a message counts as done |
| [114](../app/workers/validator.py#L114) | `auto_offset_reset="earliest"`: a brand-new group starts from the oldest retained message |
| [115](../app/workers/validator.py#L115) | `max_poll_interval_ms`: a worker that stops polling for this long is treated as dead |
| [91-100](../app/workers/validator.py#L91) | Rebalance listener: logs `partitions assigned/revoked`, which is how we watched failover |
| [133](../app/workers/validator.py#L133) | **Commit `offset + 1` after processing**: the line that makes this at-least-once |
| [136](../app/workers/validator.py#L136) | `consumer.stop()` leaves the group cleanly, so the rebalance is immediate |

Graceful shutdown on SIGTERM: [app/workers/common.py:16](../app/workers/common.py#L16).

## 2.6 The notifier, a second group: [app/workers/notifier.py](../app/workers/notifier.py)

Same topic data, **different `group_id`** ([line 20](../app/workers/notifier.py#L20)), so it receives *every* `validated` event independently of anything else. Committing after each batch ([line 49](../app/workers/notifier.py#L49)) is still at-least-once, so a real notifier would dedupe on `event_id` ([line 46](../app/workers/notifier.py#L46)) to avoid sending the same push notification twice.

## 2.7 Tests: [tests/test_kafka.py](../tests/test_kafka.py)

| Lines | Proves |
|---|---|
| [60](../tests/test_kafka.py#L60) | Kafka mode writes an outbox row and runs nothing in-process |
| [77](../tests/test_kafka.py#L77) | Receipt and outbox row **roll back together** |
| [90](../tests/test_kafka.py#L90), [102](../tests/test_kafka.py#L102) | Relay publishes in order, once; a broker failure mid-batch keeps progress and loses nothing |
| [118](../tests/test_kafka.py#L118), [128](../tests/test_kafka.py#L128) | Handler validates and emits the follow-up event; a duplicate is skipped and **the LLM isn't called twice** |
| [138](../tests/test_kafka.py#L138) | **Race:** another worker finishes during our LLM call, so our update loses and no second event is emitted |
| [157](../tests/test_kafka.py#L157)–[197](../tests/test_kafka.py#L197) | Poison → DLQ without retries (with headers); transient → 3 retries → DLQ; success → no DLQ |
| [212](../tests/test_kafka.py#L212) | **Real Kafka:** outbox → relay → broker → consumer → handler, on throwaway topics (skipped if Kafka is down) |

---

# Part 3 — What happened in the live demo

**1. Partitions split across workers:**
```
[w1] partitions assigned: [0, 2]      [w2] partitions assigned: [1]
[w2] p1@0 receipt 12 → rejected       [w1] p0@0 receipt 11 → validated
[w1] p0@1 receipt 15 → needs_review   [w2] p1@1 receipt 13 → validated   ← parallel, in order per partition
```

**2. Fan-out:** the notifier (its own group) received all 6 results without the validators knowing it exists.

**3. A hard crash mid-job (`kill -9`), with no data loss:**
```
16:01:47 [w1] p0@2 receipt 17 → processing        ← killed here, before the commit
16:01:59 [w2] partitions assigned: [0, 1, 2]      ← ~12s later (session timeout) the group rebalanced
16:02:01 [w2] p0@2 receipt 17 → validated         ← same offset redelivered; exactly 1 notification
```
With a graceful stop (SIGTERM) the rebalance is immediate. The ~12s is how long Kafka waits before deciding a *silent* member is dead.

**4. Idempotency and the DLQ:**
```
[w2] p0@3 receipt 11 → skip (already validated)
[w2] p1@6 receipt ? → DEAD-LETTERED (invalid payload: Invalid JSON …)
[w2] p0@4 receipt 99999 → DEAD-LETTERED (receipt 99999 does not exist)
DLQ message headers: error:receipt 99999 does not exist, source:receipts.submitted/0/4
```

**5. Lag 0 for both groups; 0 receipts stuck in `pending`.**

**6. An unplanned lesson, a hot partition:** every `receipts.validated` event landed in **partition 1** (offsets 0, 10, 0). They're keyed by `member_id` and all demo receipts belong to one member, so `hash("demo-member") % 3` sends everything to one partition, and one notifier consumer does all the work. In production a few very active members could overload one partition. This is the **ordering vs parallelism trade-off**: per-member order needs the member as key; if strict order isn't required, key by `receipt_id` or use a composite key.

---

# Part 4 — Interview Q&A

### 1. Kafka vs RabbitMQ: when do you choose which?
RabbitMQ is a broker: messages are deleted once acknowledged, it has rich routing (exchanges, priorities, delays) and low latency, and it's great for work queues. Kafka is a retained, partitioned log: many independent consumer groups, replay, per-key ordering and huge throughput, and it's great for event streams that several systems consume. Rule of thumb: *a task to do → RabbitMQ/Celery; a fact that happened → Kafka.*

### 2. Explain partitions, keys and consumer groups.
A topic is split into partitions, each an ordered log. The producer's key picks the partition (`hash % N`), so the same key always goes to the same partition and stays in order. A consumer group shares the partitions: each partition is read by exactly one member, so parallelism is capped at the partition count. Different groups each read everything independently.

### 3. How does Kafka guarantee ordering?
Only **within a partition**. There's no global order across a topic. If order matters for an entity (a member, an account), use that entity as the key. A trade-off: a very active key creates a hot partition (we saw this).

### 4. What happens when a consumer dies?
It stops heart-beating, and after the session timeout the group coordinator **rebalances**, giving its partitions to the surviving members. They resume from the **last committed offset**, so uncommitted work is redelivered. In our demo, w1 was killed while processing receipt 17 and, 12s later, w2 processed it.

### 5. At-least-once, at-most-once, exactly-once?
It depends on when you commit: before processing gives at-most-once (can lose messages); after gives at-least-once (can duplicate). Exactly-once exists for Kafka→Kafka pipelines (transactions + idempotent producer) but not for external side effects. In practice: at-least-once plus idempotent consumers, which gives effectively-once results.

### 6. How do you make a consumer idempotent?
Make processing a no-op the second time: check state first (we skip receipts that aren't `pending`), use conditional or optimistic updates (`UPDATE … WHERE status='pending'` and check the rowcount), upserts with unique constraints, or a processed-`event_id` table. For external calls (payments, emails), pass an idempotency key to the provider.

### 7. What is the dual-write problem? How does the outbox pattern solve it?
Writing to the DB and publishing to Kafka are two systems with no shared transaction, so a crash between them leaves them inconsistent. Outbox: write the event to an `outbox` table **in the same DB transaction** as the business change; a relay (polling with `SKIP LOCKED`, or CDC with Debezium) publishes it and marks it sent. It's at-least-once, so consumers stay idempotent. Bonus: the API doesn't depend on Kafka being up.

### 8. How do you handle a message that always fails?
Separate **poison** messages (bad payload, missing entity: they'll never succeed) from **transient** failures (DB blip). Poison goes straight to a dead-letter topic; transient errors get bounded retries with backoff, then the DLQ. Include error and source metadata in headers, alert on DLQ growth, and have a replay tool. Never retry forever, because one message would block its whole partition.

### 9. How do you scale consumers? What limits it?
Add instances to the group, up to the number of partitions; beyond that they sit idle. Plan partitions for peak (adding them later changes the key→partition mapping). Watch **consumer lag** as the main health and autoscaling signal (e.g. KEDA in Kubernetes). If processing is slow (like our LLM calls), you can also process concurrently inside one consumer while still committing in order.

### 10. What is consumer lag, and why monitor it?
Log-end offset minus committed offset per partition: the backlog. Growing lag means consumers can't keep up (too slow, too few, stuck on a poison message). It's the key SLO metric for async pipelines, e.g. "lag under 1,000 and time-to-validate p95 under 60s".

### 11. How do you evolve event schemas safely?
Treat events as public APIs: add a version, make backward-compatible changes (add optional fields, never rename or remove), use a schema registry (Avro, Protobuf or JSON Schema with compatibility checks), and for breaking changes publish v2 alongside v1 until consumers migrate. Thin events reduce coupling.

### 12. Fat events or thin events?
Fat events carry full state, so consumers need no lookups and the history is self-contained, but they're bigger, more coupled, and risk overwriting newer data with stale data on replay. Thin events carry ids plus what changed, so consumers read current state from the source. That's simpler and safer for us, at the cost of a DB read.

### 13. Why `acks=all` and an idempotent producer?
`acks=all`: the write is acknowledged only after all in-sync replicas have it, so losing the leader doesn't lose acknowledged messages (with `min.insync.replicas=2`, replication 3). Idempotent producer: the broker de-duplicates the producer's own retries, so network blips don't create duplicates.

### 14. What changed in Kafka with KRaft?
Kafka used to depend on ZooKeeper for cluster metadata. KRaft moves that into Kafka itself (a Raft quorum of controllers): one system to run, faster failover, more partitions. ZooKeeper mode is removed in Kafka 4.x, and we run 4.1 in KRaft mode.

---

# Part 5 — Self-check (answer before opening)

<details><summary>1. Why did we add Kafka at all, instead of keeping BackgroundTasks?</summary>
BackgroundTasks live in the API process's memory: a crash or deploy loses them and the receipt stays <code>pending</code> forever. Kafka plus the outbox makes the work durable (events survive restarts), retryable, independently scalable, and visible (lag).
</details>

<details><summary>2. We run 2 validators on a 3-partition topic. What happens if we start a 4th validator?</summary>
After the rebalance, three members get one partition each and the fourth sits idle, because a partition is consumed by only one member of a group. To use more workers you need more partitions.
</details>

<details><summary>3. The validator crashes after the LLM call and DB update, but before consumer.commit(). What happens next?</summary>
The offset wasn't committed, so after the rebalance the message is redelivered. <code>handle()</code> finds the receipt no longer <code>pending</code> and returns "skip". No second LLM call, no second event. That's at-least-once delivery made safe by idempotency.
</details>

<details><summary>4. Kafka is down for 10 minutes. Can members still submit receipts?</summary>
Yes. The API only writes to Postgres (receipt + outbox row in one transaction) and never talks to Kafka. The relay logs errors and retries; when Kafka returns it publishes the backlog in order.
</details>

<details><summary>5. Why does the worker NOT hold a DB transaction open while calling the LLM?</summary>
The LLM call can take seconds. Holding a transaction or row lock that long ties up a pooled connection and blocks other writers. Read, release, call the LLM, then a short conditional update; the <code>WHERE status='pending'</code> check handles the race.
</details>

<details><summary>6. The notifier and validator both consume receipt events. How does Kafka deliver to both? How would RabbitMQ?</summary>
Kafka: different consumer group ids, each with its own offsets on the same retained log; adding a consumer needs no producer change and can even replay history. RabbitMQ: a fanout or topic exchange with one queue per consumer type; messages are copied to each queue and deleted on ack, so there's no replay.
</details>

<details><summary>7. All "validated" events went to one partition in the demo. Why, and does it matter?</summary>
They're keyed by <code>member_id</code> and all demo receipts belong to one member, so one hash gives one partition. In production a few heavy members would create hot partitions that one consumer must handle. It's a trade-off: per-member ordering versus parallelism.
</details>

<details><summary>8. Why is a malformed message sent to the DLQ instead of being retried?</summary>
It will never succeed, so retrying wastes time and, worse, blocks every message behind it in that partition. Dead-letter it with context, keep the partition flowing, alert, then fix and replay.
</details>

---

## Exercises (30–45 min)
1. **Scale and watch rebalances.** With `./scripts/dev_stack.sh` running, start a third validator (`uv run python -m app.workers.validator --name w3`) and watch `.stack-logs/w*.log`: who gets which partition? Start a 4th: what does it get?
2. **Kafka outage.** Run `docker compose stop kafka`, POST 3 receipts (they return `201 pending`), check the `outbox` table in psql (`published_at IS NULL`), then `docker compose start kafka` and watch the relay catch up.
3. **Replay.** Stop the notifier, reset its offsets (`kafka-consumer-groups.sh --bootstrap-server localhost:9092 --group member-notifier --reset-offsets --to-earliest --topic receipts.validated --execute`), start it again, and watch it re-send every notification. Then explain why a real notifier must dedupe on `event_id`.
4. **Fix the hot partition.** Change the `validated` key to `receipt_id` and re-run. What do you gain, and what ordering guarantee do you lose?
5. **Build a DLQ replay script** that reads `receipts.submitted.dlq`, prints each error header, and republishes the selected messages to `receipts.submitted`.
