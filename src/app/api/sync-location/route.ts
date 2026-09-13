import { CrisisSessionStatus } from "@prisma/client";
import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUserId } from "@/lib/auth";
import { prisma } from "@/lib/prisma";
import { locationSyncSchema } from "@/lib/validators";

export async function POST(request: NextRequest) {
  const userId = await getAuthenticatedUserId(request);

  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const body = locationSyncSchema.parse(await request.json());

    // Ownership and ACTIVE status are included in the update condition.
    // This prevents a user from updating another person's session, or
    // accidentally reactivating an ended/alerted session.
    const result = await prisma.activeCrisisSession.updateMany({
      where: {
        id: body.sessionId,
        userId,
        status: CrisisSessionStatus.ACTIVE,
        alertTriggeredAt: null,
      },
      data: {
        last_location_lat: body.latitude,
        last_location_lng: body.longitude,
        last_ping_timestamp: new Date(),
        locationAccuracyM: body.accuracyMeters,
        lastPingSource: body.source,
      },
    });

    if (result.count === 0) {
      return NextResponse.json(
        { error: "No active crisis session was found." },
        { status: 404 },
      );
    }

    return NextResponse.json({
      ok: true,
      serverTimestamp: new Date().toISOString(),
    });
  } catch (error) {
    console.error("Location sync failed:", error);

    return NextResponse.json(
      { error: "Invalid location payload." },
      { status: 400 },
    );
  }
}