import { CrisisSessionStatus } from "@prisma/client";
import { NextRequest, NextResponse } from "next/server";
import { getAuthenticatedUserId } from "@/lib/auth";
import { prisma } from "@/lib/prisma";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ sessionId: string }> },
) {
  const userId = await getAuthenticatedUserId(request);
  const { sessionId } = await params;

  if (!userId) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  await prisma.activeCrisisSession.updateMany({
    where: {
      id: sessionId,
      userId,
      status: CrisisSessionStatus.ACTIVE,
    },
    data: {
      status: CrisisSessionStatus.ENDED,
      endedAt: new Date(),
    },
  });

  return NextResponse.json({ ok: true });
}