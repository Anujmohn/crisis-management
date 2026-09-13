import { NextRequest, NextResponse } from "next/server";
import { CrisisType } from "@prisma/client";
import { z } from "zod";
import { CRISIS_CONFIG } from "@/lib/crisis-config";

const inputSchema = z.object({
  crisisType: z.nativeEnum(CrisisType),
  latitude: z.number().min(-90).max(90),
  longitude: z.number().min(-180).max(180),
});

export async function GET(request: NextRequest) {
  const input = inputSchema.safeParse({
    crisisType: request.nextUrl.searchParams.get("crisisType"),
    latitude: Number(request.nextUrl.searchParams.get("latitude")),
    longitude: Number(request.nextUrl.searchParams.get("longitude")),
  });

  if (!input.success) {
    return NextResponse.json({ error: "Invalid search parameters." }, { status: 400 });
  }

  const { crisisType, latitude, longitude } = input.data;
  const query = CRISIS_CONFIG[crisisType].safeHavenQuery;

  const params = new URLSearchParams({
    q: query,
    proximity: `${longitude},${latitude}`,
    limit: "8",
    access_token: process.env.MAPBOX_SECRET_TOKEN!,
  });

  const response = await fetch(
    `https://api.mapbox.com/search/searchbox/v1/forward?${params}`,
    { next: { revalidate: 60 } },
  );

  if (!response.ok) {
    return NextResponse.json({ error: "Safe-haven search is unavailable." }, { status: 502 });
  }

  const data = await response.json();

  const havens = (data.features ?? []).map((feature: any) => ({
    id: feature.properties?.mapbox_id ?? crypto.randomUUID(),
    name: feature.properties?.name ?? feature.properties?.full_address ?? "Safe location",
    address: feature.properties?.full_address ?? "",
    longitude: feature.geometry.coordinates[0],
    latitude: feature.geometry.coordinates[1],
  }));

  return NextResponse.json({ havens, source: "Mapbox" });
}