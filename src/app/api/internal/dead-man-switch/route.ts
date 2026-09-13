import { NextRequest, NextResponse } from "next/server";
import { runDeadManSwitch } from "@/workers/dead-man-switch";

export async function GET(request: NextRequest) {
  // Prevent the endpoint from being called publicly.
  const authHeader = request.headers.get("authorization");
  const expectedHeader = `Bearer ${process.env.CRON_SECRET}`;

  if (!process.env.CRON_SECRET || authHeader !== expectedHeader) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  try {
    const result = await runDeadManSwitch();
    return NextResponse.json({ ok: true, ...result });
  } catch (error) {
    console.error("Dead-man switch failed:", error);
    return NextResponse.json(
      { error: "Dead-man switch execution failed." },
      { status: 500 },
    );
  }
}