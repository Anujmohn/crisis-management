"use client";

import { CrisisType } from "@prisma/client";
import { useState } from "react";
import { CRISIS_CONFIG } from "@/lib/crisis-config";
import { useCrisisGeolocation } from "@/hooks/use-crisis-geolocation";
import { SafeHavenMap } from "@/components/safe-haven-map";

export function CrisisDashboard() {
  const [crisisType, setCrisisType] = useState<CrisisType>(CrisisType.FLOOD);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [isCrisisModeActive, setIsCrisisModeActive] = useState(false);
  const [userLocation, setUserLocation] = useState<{
    latitude: number;
    longitude: number;
  } | null>(null);

  const { status, error } = useCrisisGeolocation({
    sessionId,
    isCrisisModeActive,
    onLocation: setUserLocation,
  });

  const config = CRISIS_CONFIG[crisisType];

  async function toggleCrisisMode() {
    if (isCrisisModeActive) {
      await fetch(`/api/crisis-sessions/${sessionId}/end`, { method: "POST" });
      setIsCrisisModeActive(false);
      setSessionId(null);
      return;
    }

    // This route creates an ActiveCrisisSession with the selected crisis type.
    const response = await fetch("/api/crisis-sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ crisisType }),
    });

    if (!response.ok) {
      alert("Unable to start Crisis Mode. Please try again.");
      return;
    }

    const session = await response.json();
    setSessionId(session.id);
    setIsCrisisModeActive(true);
  }

  return (
    <main className="min-h-screen bg-slate-950 p-4 text-white">
      <div className="mx-auto max-w-lg space-y-5">
        <header>
          <p className="text-sm font-bold tracking-widest text-amber-300">PERSONAL SAFETY</p>
          <h1 className="text-3xl font-black">Crisis Dashboard</h1>
        </header>

        <label className="block text-lg font-bold">
          Crisis type
          <select
            className="mt-2 w-full rounded-lg border-2 border-white bg-slate-900 p-4 text-lg"
            value={crisisType}
            disabled={isCrisisModeActive}
            onChange={(event) => setCrisisType(event.target.value as CrisisType)}
          >
            {Object.entries(CRISIS_CONFIG).map(([value, item]) => (
              <option key={value} value={value}>{item.label}</option>
            ))}
          </select>
        </label>

        <button
          onClick={toggleCrisisMode}
          className={`w-full rounded-xl p-6 text-xl font-black ${
            isCrisisModeActive
              ? "bg-red-600 text-white"
              : "bg-emerald-400 text-slate-950"
          }`}
        >
          {isCrisisModeActive ? "END CRISIS MODE" : "ACTIVATE CRISIS MODE"}
        </button>

        {isCrisisModeActive && (
          <div className="rounded-xl border-2 border-amber-300 bg-amber-100 p-4 text-slate-950">
            <strong>Status: {status}</strong>
            {error ? <p className="mt-1">{error}</p> : <p className="mt-1">Your trusted contacts will be alerted if pings stop.</p>}
          </div>
        )}

        <section className="rounded-xl border border-slate-600 bg-slate-900 p-5">
          <h2 className="text-xl font-black">{config.label}: immediate actions</h2>
          <p className="mt-2 font-semibold text-amber-200">{config.warning}</p>
          <ol className="mt-4 space-y-3">
            {config.tips.map((tip) => (
              <li key={tip.title}>
                <strong>{tip.title}: </strong>{tip.instruction}
              </li>
            ))}
          </ol>
        </section>

        <section>
          <h2 className="mb-2 text-xl font-black">Nearby safe places</h2>
          <SafeHavenMap crisisType={crisisType} userLocation={userLocation} />
        </section>
      </div>
    </main>
  );
}