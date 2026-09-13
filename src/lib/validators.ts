import { z } from "zod";
import { CrisisType } from "@prisma/client";

export const locationSyncSchema = z.object({
  sessionId: z.string().cuid(),

  // Geographic coordinate bounds protect the database from invalid data.
  latitude: z.number().min(-90).max(90),
  longitude: z.number().min(-180).max(180),

  accuracyMeters: z.number().positive().max(100_000).optional(),
  source: z.enum(["gps", "network", "manual"]).default("gps"),
});

export type LocationSyncInput = z.infer<typeof locationSyncSchema>;

export { CrisisType };