import { CrisisSessionStatus } from "@prisma/client";
import crypto from "node:crypto";
import { prisma } from "@/lib/prisma";
import { sendEmergencyAlert } from "@/lib/notifications";

const STALE_PING_THRESHOLD_MS = 60_000;
const BATCH_SIZE = 100;

export async function runDeadManSwitch() {
  const cutoff = new Date(Date.now() - STALE_PING_THRESHOLD_MS);

  const staleSessions = await prisma.activeCrisisSession.findMany({
    where: {
      status: CrisisSessionStatus.ACTIVE,
      alertTriggeredAt: null,
      last_ping_timestamp: { lt: cutoff },
    },
    include: {
      user: {
        include: {
          trustedContacts: {
            where: { isActive: true },
            orderBy: { priority: "asc" },
          },
        },
      },
    },
    take: BATCH_SIZE,
  });

  for (const session of staleSessions) {
    const claimToken = crypto.randomUUID();

    // Conditional update acts as a concurrency-safe lock:
    // only one worker can claim this session for alert delivery.
    const claim = await prisma.activeCrisisSession.updateMany({
      where: {
        id: session.id,
        status: CrisisSessionStatus.ACTIVE,
        alertTriggeredAt: null,
        last_ping_timestamp: { lt: cutoff },
      },
      data: {
        alertClaimToken: claimToken,
        alertTriggeredAt: new Date(),
        alertReason: `No location ping received for over ${
          STALE_PING_THRESHOLD_MS / 1000
        } seconds.`,
        status: CrisisSessionStatus.ALERT_TRIGGERED,
      },
    });

    if (claim.count === 0) continue;

    const userName = session.user.fullName || session.user.email;
    const latitude = session.last_location_lat.toString();
    const longitude = session.last_location_lng.toString();

    const deliveries = await Promise.allSettled(
      session.user.trustedContacts.map((recipient) =>
        sendEmergencyAlert({
          recipient,
          userName,
          crisisType: session.crisisType,
          latitude,
          longitude,
          lastPingAt: session.last_ping_timestamp,
        }),
      ),
    );

    console.info("Dead-man alert processed", {
      sessionId: session.id,
      contactCount: session.user.trustedContacts.length,
      deliveries: deliveries.map((result) => result.status),
    });
  }

  return { scanned: staleSessions.length };
}