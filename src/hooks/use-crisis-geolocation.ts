"use client";

import { useEffect, useRef, useState } from "react";

type Location = {
  latitude: number;
  longitude: number;
  accuracyMeters?: number;
};

type Options = {
  sessionId: string | null;
  isCrisisModeActive: boolean;
  onLocation?: (location: Location) => void;
};

const PING_INTERVAL_MS = 10_000; // Keep well below the 60-second server threshold.

export function useCrisisGeolocation({
  sessionId,
  isCrisisModeActive,
  onLocation,
}: Options) {
  const watchIdRef = useRef<number | null>(null);
  const latestLocationRef = useRef<Location | null>(null);

  const [status, setStatus] = useState<
    "idle" | "locating" | "tracking" | "gps-error"
  >("idle");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isCrisisModeActive || !sessionId) {
      if (watchIdRef.current !== null) {
        navigator.geolocation.clearWatch(watchIdRef.current);
        watchIdRef.current = null;
      }
      setStatus("idle");
      return;
    }

    if (!navigator.geolocation) {
      setStatus("gps-error");
      setError("This device does not support location services.");
      return;
    }

    const syncLatestLocation = async () => {
      const location = latestLocationRef.current;
      if (!location) return; // Wait until GPS provides an initial location.

      try {
        const response = await fetch("/api/sync-location", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({
            sessionId,
            latitude: location.latitude,
            longitude: location.longitude,
            accuracyMeters: location.accuracyMeters,
            source: "gps",
          }),
        });

        if (!response.ok) {
          throw new Error("The safety service could not receive your location.");
        }
      } catch {
        // Do not stop tracking: a later retry may succeed after a temporary outage.
        setError("Location is being tracked locally; server sync is unavailable.");
      }
    };

    setStatus("locating");
    setError(null);

    watchIdRef.current = navigator.geolocation.watchPosition(
      (position) => {
        const location = {
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracyMeters: position.coords.accuracy,
        };

        latestLocationRef.current = location;
        setStatus("tracking");
        setError(null);
        onLocation?.(location);

        // Send immediately instead of waiting for the next heartbeat.
        void syncLatestLocation();
      },
      (geoError) => {
        setStatus("gps-error");

        const message =
          geoError.code === geoError.PERMISSION_DENIED
            ? "Location permission was denied. Crisis Mode cannot protect you without it."
            : geoError.code === geoError.POSITION_UNAVAILABLE
              ? "GPS signal is unavailable. Move to an open area if safe."
              : "Location request timed out. Retrying automatically.";

        setError(message);
      },
      {
        enableHighAccuracy: true,
        maximumAge: 5_000,
        timeout: 15_000,
      },
    );

    // Browsers may not emit watchPosition events on a predictable cadence.
    // This timer re-sends the most recent valid position as a heartbeat.
    const heartbeat = window.setInterval(() => {
      void syncLatestLocation();
    }, PING_INTERVAL_MS);

    return () => {
      window.clearInterval(heartbeat);

      if (watchIdRef.current !== null) {
        navigator.geolocation.clearWatch(watchIdRef.current);
        watchIdRef.current = null;
      }
    };
  }, [sessionId, isCrisisModeActive, onLocation]);

  return { status, error };
}
