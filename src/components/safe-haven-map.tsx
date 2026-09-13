"use client";

import Map, { Marker, Popup } from "react-map-gl/mapbox";
import { useEffect, useState } from "react";
import type { CrisisType } from "@prisma/client";

type Location = { latitude: number; longitude: number };
type Haven = Location & { id: string; name: string; address: string };

export function SafeHavenMap({
  crisisType,
  userLocation,
}: {
  crisisType: CrisisType;
  userLocation: Location | null;
}) {
  const [havens, setHavens] = useState<Haven[]>([]);
  const [selected, setSelected] = useState<Haven | null>(null);

  useEffect(() => {
    if (!userLocation) return;

    const params = new URLSearchParams({
      crisisType,
      latitude: String(userLocation.latitude),
      longitude: String(userLocation.longitude),
    });

    fetch(`/api/safe-havens?${params}`)
      .then((response) => response.json())
      .then((data) => setHavens(data.havens ?? []))
      .catch(() => setHavens([]));
  }, [crisisType, userLocation]);

  if (!userLocation) {
    return (
      <div className="rounded-xl bg-slate-800 p-5 text-slate-100">
        Enable location to see nearby safe places.
      </div>
    );
  }

  return (
    <section aria-label="Nearby safe places" className="overflow-hidden rounded-xl">
      <Map
        initialViewState={{
          longitude: userLocation.longitude,
          latitude: userLocation.latitude,
          zoom: 13,
        }}
        mapboxAccessToken={process.env.NEXT_PUBLIC_MAPBOX_TOKEN}
        mapStyle="mapbox://styles/mapbox/dark-v11"
        style={{ height: 360, width: "100%" }}
      >
        <Marker longitude={userLocation.longitude} latitude={userLocation.latitude}>
          <span className="text-3xl" aria-label="Your location">📍</span>
        </Marker>

        {havens.map((haven) => (
          <Marker
            key={haven.id}
            longitude={haven.longitude}
            latitude={haven.latitude}
            onClick={(event) => {
              event.originalEvent.stopPropagation();
              setSelected(haven);
            }}
          >
            <button
              className="rounded-full bg-emerald-500 px-2 py-1 text-sm font-black text-black"
              aria-label={`Safe place: ${haven.name}`}
            >
              SAFE
            </button>
          </Marker>
        ))}

        {selected && (
          <Popup
            longitude={selected.longitude}
            latitude={selected.latitude}
            onClose={() => setSelected(null)}
          >
            <strong>{selected.name}</strong>
            <p>{selected.address}</p>
            <a
              href={`https://www.google.com/maps/dir/?api=1&destination=${selected.latitude},${selected.longitude}`}
              target="_blank"
              rel="noreferrer"
            >
              Open directions
            </a>
          </Popup>
        )}
      </Map>
    </section>
  );
}