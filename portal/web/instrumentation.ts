/**
 * Next.js instrumentation hook: the broadcast fanout consumer.
 *
 * The /api/broadcast route only stores the note and queues a
 * `broadcast.requested` event on the portal's Redis stream. This worker owns
 * the Matrix/mail leg via `fanoutBroadcast`, using a consumer group so a
 * crash before XACK leaves the event pending for XAUTOCLAIM (5 min).
 * Fanout is best-effort; payloads carry only the operator's text.
 */
import type Redis from "ioredis";

const GROUP = "portal-web-broadcast";
const CONSUMER = "broadcast-worker";
const CLAIM_AFTER_MS = 5 * 60 * 1000;

type ClaimResult = [string, Array<[string, string[]]>, string[]];

async function startBroadcastWorker() {
  const Redis = (await import("ioredis")).default;
  const { fanoutBroadcast } = await import("./src/lib/broadcast");
  const stream = process.env.EVENTS_STREAM || "opensme:events";
  const redis = new Redis(process.env.REDIS_URL || "redis://localhost:6379", {
    lazyConnect: true,
    maxRetriesPerRequest: null,
  });

  try {
    await redis.connect();
    try {
      await redis.xgroup("CREATE", stream, GROUP, "0", "MKSTREAM");
    } catch (err) {
      if (!(err instanceof Error) || !err.message.includes("BUSYGROUP")) throw err;
    }

    // Recover events claimed by a worker that died before XACK.
    const rc = redis as unknown as {
      xautoclaim: (...args: unknown[]) => Promise<ClaimResult>;
    };
    const claimed = await rc.xautoclaim(
      stream, GROUP, CONSUMER, CLAIM_AFTER_MS, "0-0", "COUNT", 10,
    );
    for (const [id, fields] of claimed[1] ?? []) {
      await deliver(id, fields, redis, stream, fanoutBroadcast);
    }

    // eslint-disable-next-line no-constant-condition
    while (true) {
      const replies = (await redis.xreadgroup(
        "GROUP", GROUP, CONSUMER, "COUNT", 10, "BLOCK", 5000,
        "STREAMS", stream, ">",
      )) as Array<[string, Array<[string, string[]]>]> | null;
      for (const [, entries] of replies ?? []) {
        for (const [id, fields] of entries) {
          await deliver(id, fields, redis, stream, fanoutBroadcast);
        }
      }
    }
  } catch (err) {
    console.warn("[broadcast-consumer] worker stopped:", err instanceof Error ? err.message : "unknown error");
  } finally {
    redis.disconnect();
  }
}

async function deliver(
  id: string,
  fields: string[],
  redis: Pick<Redis, "xack">,
  stream: string,
  fanout: (text: string) => Promise<unknown>,
) {
  const data: Record<string, string> = {};
  for (let i = 0; i + 1 < fields.length; i += 2) data[fields[i]] = fields[i + 1];
  if (data.kind !== "broadcast.requested") return;
  try {
    const payload = JSON.parse(data.payload ?? "{}") as { text?: string };
    if (typeof payload.text !== "string" || !payload.text.trim()) return;
    await fanout(payload.text);
    await redis.xack(stream, GROUP, id);
  } catch (err) {
    // No XACK: the pending event is picked up by XAUTOCLAIM on the next start.
    console.warn("[broadcast-consumer] delivery failed for", id, err instanceof Error ? err.message : "unknown error");
  }
}

export async function register() {
  if (process.env.NEXT_RUNTIME !== "nodejs") return;
  if (!["1", "true"].includes((process.env.EVENTS_ENABLED || "").toLowerCase())) return;
  void startBroadcastWorker();
}
