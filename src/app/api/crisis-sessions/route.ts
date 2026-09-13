import { CrisisSessionStatus, CrisisType } from "@prisma/client";
import { NextRequest, NextResponse } from "next/server";
import { z } from "zod";
import { getAuthenticatedUserId } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

const createSessionSchema = z.object({
  crisisType: z.nativeEnum(CrisisType),
});

export async function POST(request: NextRequest) {
  const userId = await getAuthenticatedUserId(request);

  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const parsed = createSessionSchema.safeParse(await request.json());

  if (!parsed.success) {
    return NextResponse.json({ error: "Invalid crisis type." }, { status: 400 });
  }

  // End any earlier active session so one person has one watchdog at a time.
  await prisma.activeCrisisSession.updateMany({
    where: { userId, status: CrisisSessionStatus.ACTIVE },
    data: { status: CrisisSessionStatus.ENDED, endedAt: new Date() },
  });

  // Initial coordinates are overwritten by the first browser GPS ping.
  const session = await prisma.activeCrisisSession.create({
    data: {
      userId,
      crisisType: parsed.data.crisisType,
      status: CrisisSessionStatus.ACTIVE,
      last_location_lat: 0,
      last_location_lng: 0,
      last_ping_timestamp: new Date(),
    },
    select: { id: true, crisisType: true, startedAt: true },
  });

  return NextResponse.json(session, { status: 201 });
}