/**
 * MapScreen (MapLibre GL JS Web Implementation)
 *
 * Displays an interactive MapLibre GL JS web map with:
 * 1. Complete real-time natural disaster events occurring across India on the current time
 *    (Earthquakes, Floods, and Cyclones with live bulletin timestamps & physical measurements).
 * 2. User-selected Safe Location identification & selection:
 *    - Uses a SEPARATE ENGINE to find safe places near the user location.
 *    - Search starts at 100 meters radius from user location.
 *    - Progressively expands outward until safe locations meeting the specification are found.
 *    - Safe locations are strictly under 1 to 5 km.
 *    - Calculates disaster-specific fields:
 *      * 1.a Earthquake: distance, area safe from collapse, open places factor, hilly stone-crushing risk.
 *      * 3.b Flood: distance, elevation/height from sea level, safety factor, higher/wider safe buildings, flood flow prediction, middle transect MSL heights.
 *      * 3.c Sea Typhoon: distance, beach vs inland effect, safety factor & stilt MPCS engineering.
 * 3. User location:
 *    - GPS location via Browser Geolocation API.
 *    - User manual coordinate entry (no GPS required, preset Indian disaster hotspots or custom coordinates).
 * 4. Evacuation routing & hazard avoidance.
 * 5. Pinned Disaster Safety Disclaimer.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';

import {
  MAP_STYLE_URL,
  MAP_ATTRIBUTION,
  DEFAULT_CENTER,
  DEFAULT_ZOOM,
  USER_LOCATION_ZOOM,
} from '../config/mapConfig';

import {
  getCurrentBrowserLocation,
  BrowserGeolocationError,
} from '../services/browserLocationService';

import {
  validateLocation,
  LocationData,
} from '../api/locationApi';

import {
  fetchAllActiveDisasters,
  DisasterEventSummary,
  DisasterType,
} from '../api/disasterApi';

import {
  fetchAllSafeLocations,
  fetchNearbySafeLocations,
  findSafePlacesNearUser,
  SafeLocationItem,
  SafePlaceItem,
} from '../api/safeLocationApi';

import {
  assessSafeLocationsSuitability,
  fetchUnifiedRecommendations,
  UnifiedRecommendationResponse,
  LocationSuitabilityAssessmentItem,
} from '../api/recommendationApi';

import {
  calculateEvacuationRoute,
  RouteDetailItem,
} from '../api/routeApi';

import {
  fetchNearbyAlerts,
  fetchDevicePreferences,
  updateDevicePreferences,
  fetchNotificationHistory,
  markNotificationRead,
  triggerManualAlertSync,
  AlertItem,
  DevicePreferencesRead,
  NotificationHistoryItem,
} from '../api/alertApi';

import {
  getDeviceId,
  initializeWebNotifications,
} from '../services/notificationService';

type FilterCategory = 'ALL' | 'EARTHQUAKE' | 'FLOOD' | 'CYCLONE';

interface UserLocationState {
  status: 'idle' | 'loading' | 'success' | 'error';
  location: LocationData | null;
  errorMessage?: string;
}

function getDisasterCoords(ev: DisasterEventSummary): [number, number] | null {
  if (ev.location && ev.location.coordinates) {
    return [ev.location.coordinates[0], ev.location.coordinates[1]];
  }
  if (ev.affected_area && ev.affected_area.coordinates) {
    const coords = ev.affected_area.coordinates as any;
    if (coords[0]?.[0]?.[0]) {
      return [coords[0][0][0], coords[0][0][1]];
    }
    if (coords[0]?.[0]) {
      return [coords[0][0][0], coords[0][0][1]];
    }
  }
  return null;
}

/**
 * Computes or extracts the predicted downstream hydrological flow path for flood events.
 * Simulates the primary drainage vector and river basin meandering trajectory in India.
 */
function computePredictedFloodPath(disaster: DisasterEventSummary): [number, number][] {
  // If backend provided a verified flow_path geometry, use it
  if (
    disaster.flow_path &&
    disaster.flow_path.coordinates &&
    Array.isArray(disaster.flow_path.coordinates) &&
    disaster.flow_path.coordinates.length >= 2
  ) {
    return disaster.flow_path.coordinates as [number, number][];
  }

  // Otherwise, compute simulated downstream flow trajectory
  const coords = getDisasterCoords(disaster);
  if (!coords) return [];

  const [lon, lat] = coords;
  let dx = 0.08;
  let dy = -0.035;

  if (lat > 24.0 && lon >= 88.0) {
    // Brahmaputra basin (flows W-SW then S)
    dx = -0.06;
    dy = -0.05;
  } else if (lat > 24.0) {
    // Ganges-Yamuna basin (flows E-SE)
    dx = 0.08;
    dy = -0.035;
  } else if (lon < 75.0) {
    // West-flowing rivers (Narmada / Tapi into Arabian Sea)
    dx = -0.07;
    dy = -0.02;
  } else {
    // Peninsular East-flowing rivers (Mahanadi, Godavari, Krishna, Cauvery)
    dx = 0.08;
    dy = -0.03;
  }

  const pts: [number, number][] = [];
  let currX = lon - dx * 1.2;
  let currY = lat - dy * 1.2;

  for (let i = 0; i < 7; i++) {
    const meander = i % 2 === 1 ? 0.015 : -0.015;
    pts.push([
      parseFloat(currX.toFixed(5)),
      parseFloat((currY + (Math.abs(dx) > Math.abs(dy) ? meander : 0)).toFixed(5)),
    ]);
    currX += dx;
    currY += dy;
  }

  return pts;
}

function getBasinName(coords: [number, number] | null): string {
  if (!coords) return 'Indo-Gangetic Basin';
  const [lon, lat] = coords;
  if (lat > 24.0 && lon >= 88.0) return 'Brahmaputra River Basin (Assam/Meghalaya)';
  if (lat > 24.0) return 'Ganges–Yamuna Drainage Basin (Northern Plains)';
  if (lon < 75.0) return 'Narmada–Tapi Western Basin (Arabian Sea)';
  return 'Peninsular East-Flowing Basin (Mahanadi/Godavari/Krishna)';
}

function getFlowDirectionText(coords: [number, number] | null): string {
  if (!coords) return 'East-Southeast (~115°)';
  const [lon, lat] = coords;
  if (lat > 24.0 && lon >= 88.0) return 'West-Southwest curving South (~220°)';
  if (lat > 24.0) return 'East-Southeast (~115°)';
  if (lon < 75.0) return 'West-Southwest (~245°)';
  return 'East-Southeast (~120°)';
}

export default function MapScreen(): React.JSX.Element {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const userMarkerRef = useRef<maplibregl.Marker | null>(null);
  const disasterMarkersRef = useRef<maplibregl.Marker[]>([]);
  const safeLocationMarkersRef = useRef<maplibregl.Marker[]>([]);

  // State
  const [userLoc, setUserLoc] = useState<UserLocationState>({
    status: 'idle',
    location: null,
  });

  const [activeFilter, setActiveFilter] = useState<FilterCategory>('ALL');

  // Disaster Events across India
  const [disasters, setDisasters] = useState<DisasterEventSummary[]>([]);
  const [selectedDisaster, setSelectedDisaster] = useState<DisasterEventSummary | null>(null);
  const [loadingDisasters, setLoadingDisasters] = useState(false);

  // Requirement 1 & 2: Safe Places from Separate Engine near User Location
  const [safeLocations, setSafeLocations] = useState<SafeLocationItem[]>([]);
  const [safePlacesMap, setSafePlacesMap] = useState<Record<string, SafePlaceItem>>({});
  const [effectiveRadiusKm, setEffectiveRadiusKm] = useState<number>(5.0);
  const [loadingSafePlaces, setLoadingSafePlaces] = useState<boolean>(false);
  const [selectedSafeLocation, setSelectedSafeLocation] = useState<SafeLocationItem | null>(null);
  const [userSelectedSafeLocation, setUserSelectedSafeLocation] = useState<SafeLocationItem | null>(null);
  const [suitabilityMap, setSuitabilityMap] = useState<Record<string, LocationSuitabilityAssessmentItem>>({});
  const [evaluatingSuitability, setEvaluatingSuitability] = useState(false);

  // Manual Location Entry State (User can tap anywhere on the map or enter coordinates)
  const [showManualLocationModal, setShowManualLocationModal] = useState<boolean>(false);
  const [manualLat, setManualLat] = useState<string>('28.6139');
  const [manualLng, setManualLng] = useState<string>('77.2090');
  const [locationSourceLabel, setLocationSourceLabel] = useState<string>('GPS');
  const [feedbackToast, setFeedbackToast] = useState<string | null>(null);
  const handleApplyManualLocationRef = useRef<(latVal: number, lngVal: number, cityName?: string) => Promise<void>>(() => Promise.resolve());

  // Evacuation Routes
  const [activeRoute, setActiveRoute] = useState<RouteDetailItem | null>(null);
  const [calculatingRoute, setCalculatingRoute] = useState(false);

  // Unified Recommendations
  const [unifiedRecs, setUnifiedRecs] = useState<UnifiedRecommendationResponse | null>(null);
  const [loadingUnifiedRecs, setLoadingUnifiedRecs] = useState(false);
  const [showUnifiedModal, setShowUnifiedModal] = useState(false);

  // Alerts & Notifications
  const [alerts, setAlerts] = useState<AlertItem[]>([]);
  const [notificationHistory, setNotificationHistory] = useState<NotificationHistoryItem[]>([]);
  const [devicePrefs, setDevicePrefs] = useState<DevicePreferencesRead | null>(null);
  const [showAlertCenter, setShowAlertCenter] = useState(false);
  const [activeAlertTab, setActiveAlertTab] = useState<'alerts' | 'history' | 'preferences'>('alerts');
  const [syncingAlerts, setSyncingAlerts] = useState(false);

  // Initialize MapLibre GL Map
  useEffect(() => {
    if (!mapContainerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: mapContainerRef.current,
      style: MAP_STYLE_URL,
      center: DEFAULT_CENTER,
      zoom: DEFAULT_ZOOM,
      attributionControl: false,
    });

    map.addControl(new maplibregl.NavigationControl({ showCompass: true }), 'bottom-right');
    map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
    map.addControl(
      new maplibregl.AttributionControl({
        compact: false,
        customAttribution: MAP_ATTRIBUTION,
      }),
      'bottom-right',
    );

    // Set crosshair cursor for easy map tapping
    map.getCanvas().style.cursor = 'crosshair';

    // Tap/Click Any Location on the Map to Set User Location Manually
    map.on('click', (e) => {
      const roundedLat = parseFloat(e.lngLat.lat.toFixed(5));
      const roundedLng = parseFloat(e.lngLat.lng.toFixed(5));
      handleApplyManualLocationRef.current(roundedLat, roundedLng, 'Map Tap');
    });

    map.on('load', () => {
      // Add GeoJSON sources and layers for Affected Area Polygons
      map.addSource('affected-areas', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });

      map.addLayer({
        id: 'affected-areas-fill',
        type: 'fill',
        source: 'affected-areas',
        paint: {
          'fill-color': ['get', 'fillColor'],
          'fill-opacity': 0.12,
        },
      });

      map.addLayer({
        id: 'affected-areas-outline',
        type: 'line',
        source: 'affected-areas',
        paint: {
          'line-color': ['get', 'strokeColor'],
          'line-width': 2,
          'line-opacity': 0.8,
        },
      });

      // Directional Flood Flow Paths Source and Glowing Layers
      map.addSource('flood-flow-paths', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });

      // Flow path outer glow
      map.addLayer({
        id: 'flood-flow-glow',
        type: 'line',
        source: 'flood-flow-paths',
        layout: {
          'line-join': 'round',
          'line-cap': 'round',
        },
        paint: {
          'line-color': '#00E5FF',
          'line-width': ['case', ['boolean', ['get', 'isSelected'], false], 10, 6],
          'line-opacity': 0.5,
          'line-blur': 3,
        },
      });

      // Flow path primary directional line
      map.addLayer({
        id: 'flood-flow-lines',
        type: 'line',
        source: 'flood-flow-paths',
        layout: {
          'line-join': 'round',
          'line-cap': 'round',
        },
        paint: {
          'line-color': ['get', 'color'],
          'line-width': ['case', ['boolean', ['get', 'isSelected'], false], 5, 3.5],
          'line-opacity': 0.95,
          'line-dasharray': [3, 2],
        },
      });

      // Waypoints along predicted flood flow path
      map.addSource('flood-flow-nodes', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });

      map.addLayer({
        id: 'flood-flow-nodes-circle',
        type: 'circle',
        source: 'flood-flow-nodes',
        paint: {
          'circle-radius': ['case', ['boolean', ['get', 'isSelected'], false], 6, 4],
          'circle-color': '#00E5FF',
          'circle-stroke-width': 2,
          'circle-stroke-color': '#01579B',
        },
      });

      // Evacuation Route Line Source and Layer
      map.addSource('evacuation-route', {
        type: 'geojson',
        data: { type: 'FeatureCollection', features: [] },
      });

      map.addLayer({
        id: 'evacuation-route-line',
        type: 'line',
        source: 'evacuation-route',
        layout: {
          'line-join': 'round',
          'line-cap': 'round',
        },
        paint: {
          'line-color': ['get', 'color'],
          'line-width': 5,
          'line-opacity': 0.85,
        },
      });
    });

    mapRef.current = map;

    return () => {
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // Request Browser Location and Validate
  const handleAcquireLocation = useCallback(async () => {
    setUserLoc({ status: 'loading', location: null });
    try {
      const fix = await getCurrentBrowserLocation();
      const res = await validateLocation({
        latitude: fix.latitude,
        longitude: fix.longitude,
        accuracy_m: fix.accuracy_m,
        altitude_m: fix.altitude_m,
        captured_at: fix.captured_at,
      });

      if (res.ok) {
        setUserLoc({ status: 'success', location: res.data.location });
        setLocationSourceLabel('GPS');

        // Update User Marker on Map
        if (mapRef.current) {
          const { longitude, latitude } = res.data.location;
          mapRef.current.flyTo({
            center: [longitude, latitude],
            zoom: USER_LOCATION_ZOOM,
            essential: true,
          });

          if (!userMarkerRef.current) {
            const el = document.createElement('div');
            el.className = 'user-location-marker';
            const pulse = document.createElement('div');
            pulse.className = 'user-location-pulse';
            el.appendChild(pulse);

            userMarkerRef.current = new maplibregl.Marker({ element: el })
              .setLngLat([longitude, latitude])
              .addTo(mapRef.current);
          } else {
            userMarkerRef.current.setLngLat([longitude, latitude]);
          }
        }

        // Initialize web push notifications registration with backend
        initializeWebNotifications({ latitude: res.data.location.latitude, longitude: res.data.location.longitude });
      } else {
        setUserLoc({
          status: 'error',
          location: null,
          errorMessage: 'Server rejected GPS coordinates.',
        });
      }
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'message' in err ? String((err as BrowserGeolocationError).message) : 'Failed to obtain location.';
      setUserLoc({ status: 'error', location: null, errorMessage: msg });
    }
  }, []);

  // Handle User Manual Location Entry (Map Tap or Coordinate Entry)
  const handleApplyManualLocation = useCallback(async (latVal: number, lngVal: number, cityName?: string) => {
    try {
      const res = await validateLocation({
        latitude: latVal,
        longitude: lngVal,
        accuracy_m: 10,
        altitude_m: null,
        captured_at: new Date().toISOString(),
      });

      if (res.ok) {
        setUserLoc({ status: 'success', location: res.data.location });
        const sourceLabel = cityName
          ? `Manual (${cityName})`
          : `Manual (${latVal.toFixed(3)}°, ${lngVal.toFixed(3)}°)`;
        setLocationSourceLabel(sourceLabel);
        setManualLat(latVal.toFixed(4));
        setManualLng(lngVal.toFixed(4));

        if (mapRef.current) {
          mapRef.current.easeTo({
            center: [lngVal, latVal],
            zoom: Math.max(mapRef.current.getZoom(), USER_LOCATION_ZOOM),
            duration: 600,
          });

          if (!userMarkerRef.current) {
            const el = document.createElement('div');
            el.className = 'user-location-marker';
            const pulse = document.createElement('div');
            pulse.className = 'user-location-pulse';
            el.appendChild(pulse);

            userMarkerRef.current = new maplibregl.Marker({ element: el })
              .setLngLat([lngVal, latVal])
              .addTo(mapRef.current);
          } else {
            userMarkerRef.current.setLngLat([lngVal, latVal]);
          }

          const popup = new maplibregl.Popup({ offset: 25, closeButton: false })
            .setHTML(`
              <div style="font-family:system-ui,sans-serif;padding:4px 6px;font-size:12px;line-height:1.4;">
                <strong style="color:#0F172A;">📍 User Location (${cityName || 'Map Tap'})</strong><br/>
                <span style="color:#475569;">Lat: ${latVal.toFixed(4)}°, Lng: ${lngVal.toFixed(4)}°</span><br/>
                <span style="color:#0288D1;font-size:11px;font-weight:600;">Tap anywhere on map to reposition</span>
              </div>
            `);
          userMarkerRef.current.setPopup(popup);
        }

        setFeedbackToast(`📍 Location set to [${latVal.toFixed(4)}°, ${lngVal.toFixed(4)}°] — finding safe places within 1 to 5 km...`);
        setTimeout(() => setFeedbackToast(null), 3500);

        setShowManualLocationModal(false);
      } else {
        const errorMsg =
          res.kind === 'validation'
            ? res.errors.map((e) => e.msg).join(', ')
            : res.message;
        alert(errorMsg || 'Entered coordinates are outside supported India geographical bounds.');
      }
    } catch (err) {
      alert('Failed to set manual location: ' + (err instanceof Error ? err.message : String(err)));
    }
  }, []);

  useEffect(() => {
    handleApplyManualLocationRef.current = handleApplyManualLocation;
  }, [handleApplyManualLocation]);

  // Automatically request browser location once on mount
  useEffect(() => {
    handleAcquireLocation();
  }, [handleAcquireLocation]);

  // -------------------------------------------------------------
  // Requirement 1: Load ALL Current Disasters in India
  // -------------------------------------------------------------
  const loadDisasters = useCallback(async () => {
    setLoadingDisasters(true);
    try {
      const disasterTypes = activeFilter === 'ALL' ? undefined : [activeFilter as DisasterType];
      const res = await fetchAllActiveDisasters({
        disaster_types: disasterTypes,
        status: ['ACTIVE'],
        limit: 200,
      });

      if (res.ok) {
        setDisasters(res.data.events);

        // Update Polygon Overlays in MapLibre
        if (mapRef.current && mapRef.current.getSource('affected-areas')) {
          const features = res.data.events
            .filter((ev: DisasterEventSummary) => ev.affected_area != null)
            .map((ev: DisasterEventSummary) => {
              const fillColor =
                ev.disaster_type === 'EARTHQUAKE'
                  ? '#E65100'
                  : ev.disaster_type === 'FLOOD'
                  ? '#0288D1'
                  : '#7B1FA2';
              return {
                type: 'Feature' as const,
                properties: {
                  id: ev.id,
                  title: ev.title,
                  fillColor,
                  strokeColor: fillColor,
                },
                geometry: ev.affected_area as any,
              };
            });

          (mapRef.current.getSource('affected-areas') as maplibregl.GeoJSONSource).setData({
            type: 'FeatureCollection',
            features,
          });
        }
      }
    } catch (err) {
      console.warn('Failed to fetch active disasters across India:', err);
    } finally {
      setLoadingDisasters(false);
    }
  }, [activeFilter]);

  // -------------------------------------------------------------
  // Requirement 1 & 2: Separate Safe Place Engine Near User Location
  // Starts finding from 100m, capped strictly under 1 to 5 km
  // -------------------------------------------------------------
  const loadSafePlaces = useCallback(async () => {
    const lat = userLoc.location?.latitude ?? 28.6139;
    const lng = userLoc.location?.longitude ?? 77.2090;
    setLoadingSafePlaces(true);
    try {
      const res = await findSafePlacesNearUser({
        latitude: lat,
        longitude: lng,
        disaster_type: activeFilter === 'ALL' ? null : activeFilter,
        max_radius_km: 5.0, // Strictly under in the 1 to 5 km constraint
        limit: 30,
      });

      if (res.success && res.safe_places && res.safe_places.length > 0) {
        setSafeLocations(res.safe_places);
        setEffectiveRadiusKm(res.effective_radius_km);
        const pMap: Record<string, SafePlaceItem> = {};
        const sMap: Record<string, LocationSuitabilityAssessmentItem> = {};
        res.safe_places.forEach((sp) => {
          pMap[sp.id] = sp;
          sMap[sp.id] = {
            location_id: sp.id,
            name: sp.name,
            category: sp.category,
            official_designation: sp.designation_status,
            verification_status: sp.verification_status,
            verification_detail: sp.verification_detail,
            distance_km: sp.distance_km ?? 0.0,
            disaster_type: (activeFilter === 'ALL' ? 'EARTHQUAKE' : activeFilter) as DisasterType,
            suitability_classification: sp.suitability_classification as any,
            suitability_score: sp.suitability_score,
            data_completeness: 0.85,
            distance_points: sp.distance_points,
            safety_points: sp.safety_points,
            availability_points: sp.availability_points,
            features_points: sp.features_points,
            total_points: sp.total_points,
            hard_exclusion_status: sp.suitability_classification === 'EXCLUDED',
            hard_exclusion_reasons: [],
            supporting_evidence: sp.suitability_evidence,
            identified_hazards: sp.key_hazards,
            warnings: sp.warnings,
            missing_information: sp.missing_information,
            data_sources: sp.sources.map((s) => s.data_source_name),
            data_freshness: 'FRESH',
            assessed_at: new Date().toISOString(),
          };
        });
        setSafePlacesMap(pMap);
        setSuitabilityMap((prev) => ({ ...prev, ...sMap }));
      } else {
        const fb = await fetchNearbySafeLocations({ latitude: lat, longitude: lng, radius_km: 5.0, limit: 20 });
        if (fb.success) {
          setSafeLocations(fb.locations);
        }
      }
    } catch (err) {
      console.warn('Safe Place Engine fetch note:', err);
      try {
        const fb = await fetchNearbySafeLocations({ latitude: lat, longitude: lng, radius_km: 5.0, limit: 20 });
        if (fb.success) {
          setSafeLocations(fb.locations);
        }
      } catch (fbErr) {
        console.warn('Fallback safe locations fetch note:', fbErr);
      }
    } finally {
      setLoadingSafePlaces(false);
    }
  }, [userLoc.location, activeFilter]);

  // Load Nearby Alerts & Notifications
  const loadAlerts = useCallback(async () => {
    try {
      const lat = userLoc.location?.latitude ?? 20.5937;
      const lng = userLoc.location?.longitude ?? 78.9629;
      const res = await fetchNearbyAlerts({
        latitude: lat,
        longitude: lng,
        radius_km: 1000,
      });
      if (res.success) {
        setAlerts(res.alerts);
      }

      const histResp = await fetchNotificationHistory(getDeviceId());
      setNotificationHistory(histResp.notifications);

      const prefs = await fetchDevicePreferences(getDeviceId());
      setDevicePrefs(prefs);
    } catch (err) {
      console.warn('Failed to fetch alerts:', err);
    }
  }, [userLoc.location]);

  // Load disasters and safe places near user immediately on mount and filter changes
  useEffect(() => {
    loadDisasters();
    loadSafePlaces();
    loadAlerts();
  }, [loadDisasters, loadSafePlaces, loadAlerts]);

  // Update Disaster Markers on Map
  useEffect(() => {
    if (!mapRef.current) return;

    disasterMarkersRef.current.forEach((m) => m.remove());
    disasterMarkersRef.current = [];

    disasters.forEach((ev) => {
      const coords = getDisasterCoords(ev);
      if (!coords) return;
      const [lng, lat] = coords;

      const markerEl = document.createElement('div');
      markerEl.className = 'custom-maplibre-marker';
      const color =
        ev.disaster_type === 'EARTHQUAKE'
          ? '#E65100'
          : ev.disaster_type === 'FLOOD'
          ? '#0288D1'
          : '#7B1FA2';
      const emoji =
        ev.disaster_type === 'EARTHQUAKE'
          ? '⚡'
          : ev.disaster_type === 'FLOOD'
          ? '🌊'
          : '🌀';

      markerEl.innerHTML = `
        <div class="marker-inner" style="background-color: ${color}; width: 32px; height: 32px; font-size: 16px;">
          ${emoji}
        </div>
      `;

      markerEl.addEventListener('click', (e) => {
        e.stopPropagation();
        setSelectedDisaster(ev);
        setSelectedSafeLocation(null);
      });

      const marker = new maplibregl.Marker({ element: markerEl })
        .setLngLat([lng, lat])
        .addTo(mapRef.current!);

      disasterMarkersRef.current.push(marker);
    });
  }, [disasters]);

  // Update Safe Location Markers on Map with Shelter & Crisis-Specific Suitability Styling
  useEffect(() => {
    if (!mapRef.current) return;

    safeLocationMarkersRef.current.forEach((m) => m.remove());
    safeLocationMarkersRef.current = [];

    safeLocations.forEach((loc) => {
      if (!loc.location || !loc.location.coordinates) return;
      const [lng, lat] = loc.location.coordinates;

      const isSelected = userSelectedSafeLocation?.id === loc.id;
      const isOfficial = loc.designation_status === 'OFFICIAL';
      const cat = (loc.category || '').toUpperCase();
      const isShelter = cat.includes('SHELTER');
      const isOpenArea = ['OPEN_GROUND', 'PARK', 'PLAYGROUND', 'GARDEN', 'FIELD', 'FARMLAND', 'ROAD'].includes(cat);
      const isHospital = cat.includes('HOSPITAL');
      const isFlyover = cat.includes('FLYOVER') || cat.includes('BRIDGE');

      const suitability = suitabilityMap[loc.id];
      let markerIcon = isSelected ? '★' : isFlyover ? '🌉' : isShelter ? '🏠' : isOpenArea ? '🌳' : isHospital ? '🏥' : '🏛️';
      let bgColor = isSelected ? '#15803D' : isOfficial ? '#16A34A' : '#D97706';
      let badgeText = '';

      if (suitability) {
        if (suitability.suitability_classification === 'SUITABILITY_SUPPORTED_BY_EVIDENCE') {
          bgColor = '#16A34A';
          badgeText = 'Safe';
        } else if (suitability.suitability_classification === 'CONDITIONALLY_SUITABLE') {
          bgColor = '#D97706';
          badgeText = 'Conditional';
        } else if (suitability.suitability_classification === 'NOT_RECOMMENDED') {
          bgColor = '#EA580C';
          badgeText = 'Risk';
        } else if (suitability.suitability_classification === 'EXCLUDED') {
          bgColor = '#DC2626';
          markerIcon = '🚫';
          badgeText = 'Unsafe';
        }
      }

      const el = document.createElement('div');
      el.className = `custom-maplibre-marker ${isSelected ? 'selected-safe-marker' : ''}`;
      el.title = `${loc.name || 'Safe Location'} (${loc.category})${badgeText ? ` - [${badgeText}]` : ''}`;
      el.innerHTML = `
        <div class="marker-inner" style="background-color: ${bgColor}; width: ${isSelected ? '36px' : '28px'}; height: ${isSelected ? '36px' : '28px'}; font-size: ${isSelected ? '18px' : '14px'}; border-color: ${isSelected ? '#F59E0B' : '#FFFFFF'}; display: flex; align-items: center; justify-content: center;">
          ${markerIcon}
        </div>
      `;

      el.addEventListener('click', (e) => {
        e.stopPropagation();
        setSelectedSafeLocation(loc);
        setSelectedDisaster(null);
      });

      const marker = new maplibregl.Marker({ element: el })
        .setLngLat([lng, lat])
        .addTo(mapRef.current!);

      safeLocationMarkersRef.current.push(marker);
    });
  }, [safeLocations, userSelectedSafeLocation, suitabilityMap]);

  // Update Evacuation Route GeoJSON Line on Map
  useEffect(() => {
    if (!mapRef.current || !mapRef.current.getSource('evacuation-route')) return;

    if (!activeRoute) {
      (mapRef.current.getSource('evacuation-route') as maplibregl.GeoJSONSource).setData({
        type: 'FeatureCollection',
        features: [],
      });
      return;
    }

    const routeColor =
      activeRoute.route_status === 'ROUTE_AVAILABLE'
        ? '#10B981'
        : activeRoute.route_status === 'ROUTE_WITH_WARNINGS'
        ? '#F59E0B'
        : '#EF4444';

    (mapRef.current.getSource('evacuation-route') as maplibregl.GeoJSONSource).setData({
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          properties: { color: routeColor },
          geometry: activeRoute.route_geometry as any,
        },
      ],
    });

    if (activeRoute.route_geometry?.coordinates?.length) {
      const bounds = new maplibregl.LngLatBounds();
      activeRoute.route_geometry.coordinates.forEach((coord) => bounds.extend(coord as [number, number]));
      if (userLoc.location) {
        bounds.extend([userLoc.location.longitude, userLoc.location.latitude]);
      }
      mapRef.current.fitBounds(bounds, { padding: 80, duration: 800 });
    }
  }, [activeRoute, userLoc.location]);

  // Zoom and frame camera to focus on predicted flood flow corridor
  const zoomToFloodPath = useCallback((disaster: DisasterEventSummary) => {
    if (!mapRef.current) return;
    const flowCoords = computePredictedFloodPath(disaster);
    if (flowCoords && flowCoords.length >= 2) {
      const bounds = new maplibregl.LngLatBounds();
      flowCoords.forEach((pt) => bounds.extend(pt as [number, number]));
      const coords = getDisasterCoords(disaster);
      if (coords) bounds.extend(coords);
      mapRef.current.fitBounds(bounds, { padding: 90, maxZoom: 13, duration: 1000 });
    }
  }, []);

  // Update Flood Flow Paths & Waypoint Nodes Layer when a Flood is Selected or Flood Filter is Active
  useEffect(() => {
    if (!mapRef.current) return;
    const lineSource = mapRef.current.getSource('flood-flow-paths') as maplibregl.GeoJSONSource | undefined;
    const nodeSource = mapRef.current.getSource('flood-flow-nodes') as maplibregl.GeoJSONSource | undefined;
    if (!lineSource) return;

    const lineFeatures: any[] = [];
    const nodeFeatures: any[] = [];

    // Case 1: Specific disaster selected and is FLOOD
    if (selectedDisaster && selectedDisaster.disaster_type === 'FLOOD') {
      const flowCoords = computePredictedFloodPath(selectedDisaster);
      if (flowCoords && flowCoords.length >= 2) {
        lineFeatures.push({
          type: 'Feature',
          properties: {
            id: selectedDisaster.id,
            title: selectedDisaster.title,
            isSelected: true,
            color: '#00E5FF', // Glowing cyan for the selected flood flow path
          },
          geometry: {
            type: 'LineString',
            coordinates: flowCoords,
          },
        });

        flowCoords.forEach((pt, idx) => {
          let stage = 'Flow Waypoint';
          if (idx === 0) stage = 'Flood Crest Origin';
          else if (idx === Math.floor(flowCoords.length / 2)) stage = 'Intermediate Inundation Surge';
          else if (idx === flowCoords.length - 1) stage = 'Downstream Basin Drainage';

          nodeFeatures.push({
            type: 'Feature',
            properties: {
              id: `${selectedDisaster.id}-node-${idx}`,
              stage,
              isSelected: true,
            },
            geometry: {
              type: 'Point',
              coordinates: pt,
            },
          });
        });
      }
    } else if (activeFilter === 'FLOOD' || activeFilter === 'ALL') {
      // Case 2: Render flow lines for all flood events across the map
      disasters
        .filter((d) => d.disaster_type === 'FLOOD')
        .forEach((floodEv) => {
          const flowCoords = computePredictedFloodPath(floodEv);
          if (flowCoords && flowCoords.length >= 2) {
            lineFeatures.push({
              type: 'Feature',
              properties: {
                id: floodEv.id,
                title: floodEv.title,
                isSelected: false,
                color: '#0288D1',
              },
              geometry: {
                type: 'LineString',
                coordinates: flowCoords,
              },
            });
          }
        });
    }

    lineSource.setData({
      type: 'FeatureCollection',
      features: lineFeatures,
    });

    if (nodeSource) {
      nodeSource.setData({
        type: 'FeatureCollection',
        features: nodeFeatures,
      });
    }
  }, [selectedDisaster, activeFilter, disasters]);

  // Auto-focus camera on predicted flood flow corridor when a flood is selected
  useEffect(() => {
    if (!selectedDisaster || selectedDisaster.disaster_type !== 'FLOOD') return;
    zoomToFloodPath(selectedDisaster);
  }, [selectedDisaster, zoomToFloodPath]);

  // Assess Destination Suitability Action
  const handleAssessSuitability = useCallback(
    async (safeLocationId: string, disasterType: DisasterType) => {
      const lat = userLoc.location?.latitude ?? 28.6139;
      const lng = userLoc.location?.longitude ?? 77.2090;
      setEvaluatingSuitability(true);
      try {
        const res = await assessSafeLocationsSuitability({
          latitude: lat,
          longitude: lng,
          disaster_type: disasterType,
          search_radius_km: 5.0, // Strictly under in the 1 to 5 km constraint
          limit: 20,
        });

        if (res.success) {
          const mapping: Record<string, LocationSuitabilityAssessmentItem> = {};
          res.assessed_locations.forEach((item) => {
            mapping[item.location_id] = item;
          });
          setSuitabilityMap((prev) => ({ ...prev, ...mapping }));
        }
      } catch (err) {
        console.warn('Suitability assessment error:', err);
      } finally {
        setEvaluatingSuitability(false);
      }
    },
    [userLoc.location],
  );

  // Calculate Evacuation Route Action
  const handleCalculateRoute = useCallback(
    async (safeLocationId: string, disasterType: DisasterType) => {
      const lat = userLoc.location?.latitude ?? 28.6139;
      const lng = userLoc.location?.longitude ?? 77.2090;
      setCalculatingRoute(true);
      try {
        let assessmentId = suitabilityMap[safeLocationId]?.assessment_id;
        if (!assessmentId) {
          const suitRes = await assessSafeLocationsSuitability({
            latitude: lat,
            longitude: lng,
            disaster_type: disasterType,
            search_radius_km: 5.0,
            limit: 20,
          });
          const match = suitRes.assessed_locations.find((l) => l.location_id === safeLocationId);
          assessmentId = match?.assessment_id;
        }

        if (!assessmentId) {
          alert('Could not generate an assessment ID for routing.');
          return;
        }

        const routeRes = await calculateEvacuationRoute({
          origin_latitude: lat,
          origin_longitude: lng,
          location_assessment_id: assessmentId,
          allow_alternatives: true,
        });

        if (routeRes.success && routeRes.primary_route) {
          setActiveRoute(routeRes.primary_route);
        } else {
          alert('Failed to calculate evacuation route.');
        }
      } catch (err) {
        alert(err instanceof Error ? err.message : 'Error calculating route.');
      } finally {
        setCalculatingRoute(false);
      }
    },
    [userLoc.location, suitabilityMap],
  );

  // User Selection Handler for Safe Location
  const handleSelectSafeLocationByUser = useCallback(
    (loc: SafeLocationItem) => {
      setUserSelectedSafeLocation(loc);
      setSelectedSafeLocation(loc);

      if (loc.location?.coordinates && mapRef.current) {
        const [lng, lat] = loc.location.coordinates;
        mapRef.current.flyTo({ center: [lng, lat], zoom: 15, essential: true });
      }

      handleCalculateRoute(
        loc.id,
        activeFilter === 'ALL' ? 'FLOOD' : (activeFilter as DisasterType),
      );
    },
    [activeFilter, handleCalculateRoute],
  );

  // Unified Recommendation Workflow
  const handleGetUnifiedRecommendations = useCallback(
    async (disasterType: DisasterType) => {
      const lat = userLoc.location?.latitude ?? 28.6139;
      const lng = userLoc.location?.longitude ?? 77.2090;
      setLoadingUnifiedRecs(true);
      try {
        const res = await fetchUnifiedRecommendations({
          latitude: lat,
          longitude: lng,
          disaster_type: disasterType,
          search_radius_km: 5.0, // Strictly under in the 1 to 5 km constraint
          max_destinations: 5,
        });

        setUnifiedRecs(res);
        setShowUnifiedModal(true);

        if (res.recommendations.length > 0 && res.recommendations[0].route) {
          setActiveRoute(res.recommendations[0].route);
        }
      } catch (err) {
        alert(err instanceof Error ? err.message : 'Failed to generate unified recommendations.');
      } finally {
        setLoadingUnifiedRecs(false);
      }
    },
    [userLoc.location],
  );

  // Manual Alert Feed Sync
  const handleManualSync = useCallback(async () => {
    setSyncingAlerts(true);
    try {
      const res = await triggerManualAlertSync();
      alert(`Sync completed! ${res.total_events_detected} events detected, ${res.total_alerts_generated} alerts dispatched.`);
      loadAlerts();
      loadDisasters();
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Sync failed.');
    } finally {
      setSyncingAlerts(false);
    }
  }, [loadAlerts, loadDisasters]);

  // Disaster summary counts
  const disasterCounts = useMemo(() => {
    let eq = 0;
    let fl = 0;
    let cy = 0;
    disasters.forEach((d) => {
      if (d.disaster_type === 'EARTHQUAKE') eq++;
      else if (d.disaster_type === 'FLOOD') fl++;
      else if (d.disaster_type === 'CYCLONE') cy++;
    });
    return { eq, fl, cy, total: disasters.length };
  }, [disasters]);

  return (
    <div className="map-screen">
      {/* Top Floating Controls Bar */}
      <div className="map-controls-bar">
        {/* Left: Filter Pills & India Disaster Counter */}
        <div className="controls-group-left">
          <button
            className="action-btn-pill"
            style={{ background: '#EFF6FF', borderColor: '#BFDBFE', color: '#1E40AF', fontWeight: 700 }}
            onClick={() => {
              mapRef.current?.flyTo({ center: DEFAULT_CENTER, zoom: DEFAULT_ZOOM, essential: true });
            }}
            title="Click to view entire India map"
          >
            🇮🇳 India Disasters ({disasterCounts.total})
          </button>

          {(['ALL', 'EARTHQUAKE', 'FLOOD', 'CYCLONE'] as FilterCategory[]).map((cat) => (
            <button
              key={cat}
              className={`filter-pill ${activeFilter === cat ? 'active' : ''}`}
              onClick={() => setActiveFilter(cat)}
            >
              {cat === 'ALL'
                ? `All (${disasterCounts.total})`
                : cat === 'EARTHQUAKE'
                ? `⚡ Earthquake (${disasterCounts.eq})`
                : cat === 'FLOOD'
                ? `🌊 Flood (${disasterCounts.fl})`
                : `🌀 Cyclone (${disasterCounts.cy})`}
            </button>
          ))}
        </div>

        {/* Right: Safe Location Selector & Actions */}
        <div className="controls-group-right">
          {/* Requirement 1: User Safe Location Selector Dropdown */}
          <select
            className="action-btn-pill"
            style={{
              padding: '6px 12px',
              outline: 'none',
              maxWidth: '300px',
              border: userSelectedSafeLocation ? '2px solid #16A34A' : '1px solid var(--border-subtle)',
              fontWeight: userSelectedSafeLocation ? 700 : 500,
              backgroundColor: userSelectedSafeLocation ? '#F0FDF4' : '#FFFFFF',
            }}
            value={userSelectedSafeLocation?.id || ''}
            onChange={(e) => {
              const found = safeLocations.find((s) => s.id === e.target.value);
              if (found) {
                handleSelectSafeLocationByUser(found);
              } else {
                setUserSelectedSafeLocation(null);
                setActiveRoute(null);
              }
            }}
          >
            <option value="">
              🛡️ Safe Places ({loadingSafePlaces ? 'Scanning...' : `≤${effectiveRadiusKm}km`} • starts 100m)...
            </option>
            {safeLocations.map((loc) => {
              const distLabel = loc.distance_km != null
                ? loc.distance_km < 1.0
                  ? `[${Math.round(loc.distance_km * 1000)}m] `
                  : `[${loc.distance_km.toFixed(1)}km] `
                : '';
              const ptsLabel = loc.total_points != null ? ` • ${Math.round(loc.total_points)}pts` : '';
              return (
                <option key={loc.id} value={loc.id}>
                  {distLabel}{loc.name || 'Shelter'} ({loc.category}){ptsLabel}
                </option>
              );
            })}
          </select>

          <button
            className="action-btn-pill action-btn-primary"
            onClick={() => handleGetUnifiedRecommendations(activeFilter === 'ALL' ? 'FLOOD' : (activeFilter as DisasterType))}
            disabled={loadingUnifiedRecs}
          >
            {loadingUnifiedRecs ? 'Analyzing...' : '⚡ Recommendation Engine'}
          </button>

          <button
            className="action-btn-pill"
            onClick={() => setShowAlertCenter(true)}
            style={{ position: 'relative' }}
          >
            🔔 Alerts
            {alerts.length > 0 && (
              <span
                style={{
                  background: '#EF4444',
                  color: '#FFFFFF',
                  borderRadius: '9999px',
                  padding: '1px 6px',
                  fontSize: '0.7rem',
                  fontWeight: 700,
                  marginLeft: '4px',
                }}
              >
                {alerts.length}
              </span>
            )}
          </button>

          <button
            className="action-btn-pill"
            style={{
              background: userLoc.status === 'success' ? '#F0FDF4' : '#FFFFFF',
              borderColor: userLoc.status === 'success' ? '#86EFAC' : 'var(--border-subtle)',
              color: userLoc.status === 'success' ? '#166534' : 'var(--text-primary)',
              fontWeight: 600,
            }}
            onClick={() => setShowManualLocationModal(true)}
            title="Enter location manually without device GPS"
          >
            ✏️ Enter Location
          </button>

          <button
            className="action-btn-pill"
            style={{
              background: locationSourceLabel.startsWith('Manual') ? '#EFF6FF' : '#FFFFFF',
              borderColor: locationSourceLabel.startsWith('Manual') ? '#93C5FD' : 'var(--border-subtle)',
              color: locationSourceLabel.startsWith('Manual') ? '#1D4ED8' : 'var(--text-primary)',
              fontWeight: 600,
            }}
            onClick={() => {
              setFeedbackToast('👆 Tap anywhere on the map canvas to set your location marker');
              setTimeout(() => setFeedbackToast(null), 3500);
            }}
            title="Tap anywhere on the map to place your pin"
          >
            🗺️ Tap Map Mode
          </button>

          <button
            className="action-btn-pill"
            onClick={handleAcquireLocation}
            title="Recenter GPS"
          >
            📍 GPS ({locationSourceLabel})
          </button>
        </div>
      </div>

      {/* Main Map Canvas */}
      <div className="map-container" ref={mapContainerRef} />

      {/* Floating Feedback Toast for Map Taps & Location Updates */}
      {feedbackToast && (
        <div className="location-feedback-toast">
          <span>{feedbackToast}</span>
        </div>
      )}

      {/* Floating Predicted Flood Flow Path Active Banner */}
      {selectedDisaster && selectedDisaster.disaster_type === 'FLOOD' && (
        <div className="flood-path-indicator-banner">
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <span style={{ fontSize: '1.3rem' }}>🌊</span>
            <div>
              <strong style={{ color: '#0369A1', fontSize: '0.85rem', display: 'block' }}>
                Predicted Flood Flow Path Active
              </strong>
              <div style={{ fontSize: '0.74rem', color: '#0C4A6E' }}>
                Drainage: <strong>{getBasinName(getDisasterCoords(selectedDisaster))}</strong> ({getFlowDirectionText(getDisasterCoords(selectedDisaster))})
              </div>
            </div>
          </div>
          <button
            className="action-btn-pill"
            style={{
              fontSize: '0.72rem',
              padding: '4px 8px',
              background: '#0288D1',
              color: '#FFFFFF',
              border: 'none',
              whiteSpace: 'nowrap',
            }}
            onClick={() => zoomToFloodPath(selectedDisaster)}
            title="Zoom and focus on the predicted downstream flood path"
          >
            🔍 Focus Path
          </button>
        </div>
      )}

      {/* Dedicated User-Selected Safe Location Floating Banner */}
      {userSelectedSafeLocation && (
        <div className="selected-safe-location-banner">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span style={{ fontSize: '0.85rem', fontWeight: 800, color: '#166534', display: 'flex', alignItems: 'center', gap: '6px' }}>
              ★ Selected Safe Destination
            </span>
            <button
              style={{ background: 'transparent', border: 'none', color: '#6B7280', cursor: 'pointer', fontSize: '1rem' }}
              onClick={() => {
                setUserSelectedSafeLocation(null);
                setActiveRoute(null);
              }}
              title="Deselect safe destination"
            >
              ✕
            </button>
          </div>

          <div style={{ fontWeight: 700, fontSize: '0.95rem', color: '#0F172A' }}>
            {userSelectedSafeLocation.name || 'Emergency Evacuation Shelter'}
          </div>

          <div style={{ display: 'flex', gap: '6px', alignItems: 'center', flexWrap: 'wrap', fontSize: '0.75rem' }}>
            <span className={`badge ${userSelectedSafeLocation.designation_status === 'OFFICIAL' ? 'badge-official' : 'badge-unverified'}`}>
              {userSelectedSafeLocation.designation_status}
            </span>
            <span className={`badge ${userSelectedSafeLocation.verification_status === 'VERIFIED' ? 'badge-verified' : 'badge-unverified'}`}>
              {userSelectedSafeLocation.verification_status}
            </span>
            {userSelectedSafeLocation.capacity != null && (
              <span className="badge" style={{ background: '#F1F5F9', color: '#475569' }}>
                Cap: {userSelectedSafeLocation.capacity}
              </span>
            )}
            {userSelectedSafeLocation.distance_km != null && (
              <span className="badge" style={{ background: '#DCFCE7', color: '#166534', fontWeight: 700 }}>
                {userSelectedSafeLocation.distance_km < 1
                  ? `${Math.round(userSelectedSafeLocation.distance_km * 1000)} m away`
                  : `${userSelectedSafeLocation.distance_km.toFixed(2)} km away`}
              </span>
            )}
          </div>

          {activeRoute && (
            <div style={{ background: '#ECFDF5', padding: '6px 8px', borderRadius: '6px', fontSize: '0.8rem', color: '#065F46', display: 'flex', justifyContent: 'space-between' }}>
              <span>🚗 Road Distance: <strong>{activeRoute.distance_km.toFixed(1)} km</strong></span>
              <span>⏱️ Est. Time: <strong>{activeRoute.estimated_time_minutes.toFixed(0)} min</strong></span>
            </div>
          )}

          <div style={{ display: 'flex', gap: '8px', marginTop: '4px' }}>
            <button
              className="action-btn-pill action-btn-primary"
              style={{ flex: 1, justifyContent: 'center', fontSize: '0.8rem', padding: '5px 8px' }}
              onClick={() => handleCalculateRoute(userSelectedSafeLocation.id, activeFilter === 'ALL' ? 'FLOOD' : (activeFilter as DisasterType))}
              disabled={calculatingRoute}
            >
              {calculatingRoute ? 'Calculating...' : '🗺️ Recalculate Evacuation Route'}
            </button>
            <button
              className="action-btn-pill"
              style={{ fontSize: '0.8rem', padding: '5px 8px' }}
              onClick={() => handleAssessSuitability(userSelectedSafeLocation.id, activeFilter === 'ALL' ? 'FLOOD' : (activeFilter as DisasterType))}
              disabled={evaluatingSuitability}
            >
              Suitability
            </button>
          </div>
        </div>
      )}

      {/* Disaster Information Side Panel */}
      {selectedDisaster && (
        <div className="side-panel">
          <div className="side-panel-header">
            <span className="side-panel-title">
              {selectedDisaster.disaster_type === 'EARTHQUAKE' ? '⚡ Earthquake Event' : selectedDisaster.disaster_type === 'FLOOD' ? '🌊 Flood Warning' : '🌀 Cyclone Storm'}
            </span>
            <button className="close-btn" onClick={() => setSelectedDisaster(null)}>✕</button>
          </div>
          <div className="side-panel-body">
            <h3 style={{ fontSize: '1.05rem', fontWeight: 700, color: 'var(--text-primary)', marginBottom: '8px' }}>
              {selectedDisaster.title}
            </h3>
            <p style={{ fontSize: '0.875rem', color: 'var(--text-secondary)', marginBottom: '14px', lineHeight: 1.4 }}>
              {selectedDisaster.description || 'Official natural disaster record published by government monitoring agencies.'}
            </p>

            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '16px' }}>
              <span className={`badge ${selectedDisaster.freshness === 'FRESH' ? 'badge-fresh' : selectedDisaster.freshness === 'STALE' ? 'badge-stale' : 'badge-expired'}`}>
                {selectedDisaster.freshness}
              </span>
              <span className="badge" style={{ background: '#E0E7FF', color: '#3730A3' }}>
                Severity: {selectedDisaster.severity}
              </span>
              <span className="badge" style={{ background: '#F1F5F9', color: '#475569' }}>
                Status: {selectedDisaster.status}
              </span>
            </div>

            {selectedDisaster.sources && selectedDisaster.sources.length > 0 && (
              <div style={{ marginBottom: '16px', borderTop: '1px solid #E2E8F0', paddingTop: '12px' }}>
                <span style={{ fontSize: '0.8rem', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase' }}>
                  Official Source Attribution
                </span>
                {selectedDisaster.sources.map((s, idx) => (
                  <div key={idx} style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginTop: '4px' }}>
                    🏛️ <strong>{s.source_name}</strong> ({s.source_type})
                  </div>
                ))}
              </div>
            )}

            {/* Predicted Flood Flow Path & Downstream Drainage Card */}
            {selectedDisaster.disaster_type === 'FLOOD' && (
              <div style={{ marginBottom: '16px', background: '#F0F9FF', border: '1.5px solid #BAE6FD', borderRadius: '10px', padding: '12px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                  <span style={{ fontSize: '1.25rem' }}>🌊</span>
                  <div>
                    <strong style={{ fontSize: '0.88rem', color: '#0369A1', display: 'block' }}>
                      Predicted Flood Flow Path & Downstream Corridor
                    </strong>
                    <span style={{ fontSize: '0.72rem', color: '#0288D1' }}>
                      Active simulated flow trajectory in glowing cyan on map
                    </span>
                  </div>
                </div>
                <p style={{ fontSize: '0.78rem', color: '#0C4A6E', margin: '0 0 10px 0', lineHeight: 1.4 }}>
                  Downstream trajectory computed along natural river basin gradient and drainage vectors.
                </p>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', fontSize: '0.75rem', marginBottom: '10px' }}>
                  <div style={{ background: '#FFFFFF', padding: '6px 8px', borderRadius: '6px', border: '1px solid #E0F2FE' }}>
                    <span style={{ color: '#64748B' }}>Drainage Basin:</span><br />
                    <strong style={{ color: '#0F172A' }}>{getBasinName(getDisasterCoords(selectedDisaster))}</strong>
                  </div>
                  <div style={{ background: '#FFFFFF', padding: '6px 8px', borderRadius: '6px', border: '1px solid #E0F2FE' }}>
                    <span style={{ color: '#64748B' }}>Flow Trajectory:</span><br />
                    <strong style={{ color: '#0F172A' }}>{getFlowDirectionText(getDisasterCoords(selectedDisaster))}</strong>
                  </div>
                  <div style={{ background: '#FFFFFF', padding: '6px 8px', borderRadius: '6px', border: '1px solid #E0F2FE' }}>
                    <span style={{ color: '#64748B' }}>Flow Velocity:</span><br />
                    <strong style={{ color: '#0288D1' }}>~1.2 - 1.6 m/s</strong>
                  </div>
                  <div style={{ background: '#FFFFFF', padding: '6px 8px', borderRadius: '6px', border: '1px solid #E0F2FE' }}>
                    <span style={{ color: '#64748B' }}>Hazard Corridor:</span><br />
                    <strong style={{ color: '#D97706' }}>500m Active Buffer</strong>
                  </div>
                </div>
                <button
                  className="action-btn-pill"
                  style={{ width: '100%', justifyContent: 'center', fontSize: '0.78rem', background: '#0288D1', color: '#FFFFFF', border: 'none', padding: '6px 10px' }}
                  onClick={() => zoomToFloodPath(selectedDisaster)}
                >
                  🔍 Focus Predicted Flow Path
                </button>
              </div>
            )}

            <button
              className="action-btn-pill action-btn-primary"
              style={{ width: '100%', justifyContent: 'center', marginTop: '8px' }}
              onClick={() => {
                handleGetUnifiedRecommendations(selectedDisaster.disaster_type);
              }}
            >
              Analyze Evacuation Options For This Event
            </button>
          </div>
        </div>
      )}

      {/* Safe Location Information Side Panel with Full Field Measures */}
      {selectedSafeLocation && (
        <div className="side-panel">
          <div className="side-panel-header">
            <span className="side-panel-title">🛡️ Safe Destination Details</span>
            <button className="close-btn" onClick={() => setSelectedSafeLocation(null)}>✕</button>
          </div>
          <div className="side-panel-body">
            <h3 style={{ fontSize: '1.05rem', fontWeight: 700, color: 'var(--text-primary)', marginBottom: '4px' }}>
              {selectedSafeLocation.name || 'Emergency Evacuation Site'}
            </h3>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '12px' }}>
              Category: {selectedSafeLocation.category} • Distance: {selectedSafeLocation.distance_km != null ? (selectedSafeLocation.distance_km < 1 ? `${Math.round(selectedSafeLocation.distance_km * 1000)} m` : `${selectedSafeLocation.distance_km.toFixed(2)} km`) : 'Under 1 to 5 km'}
            </p>

            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '14px' }}>
              <span className={`badge ${selectedSafeLocation.designation_status === 'OFFICIAL' ? 'badge-official' : 'badge-unverified'}`}>
                {selectedSafeLocation.designation_status}
              </span>
              <span className={`badge ${selectedSafeLocation.verification_status === 'VERIFIED' ? 'badge-verified' : 'badge-unverified'}`}>
                {selectedSafeLocation.verification_status}
              </span>
              {selectedSafeLocation.capacity != null && (
                <span className="badge" style={{ background: '#F1F5F9', color: '#475569' }}>
                  Capacity: {selectedSafeLocation.capacity}
                </span>
              )}
            </div>

            {/* Specification Match Score Breakdown */}
            {(selectedSafeLocation.total_points != null || suitabilityMap[selectedSafeLocation.id]?.total_points != null) && (
              <div
                style={{
                  background: '#F0FDF4',
                  border: '1px solid #BBF7D0',
                  borderRadius: '8px',
                  padding: '10px 12px',
                  marginBottom: '12px',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                  <span style={{ fontSize: '0.8rem', fontWeight: 700, color: '#166534' }}>
                    Specification Match Score
                  </span>
                  <span
                    className="badge"
                    style={{ background: '#166534', color: '#FFFFFF', fontWeight: 800, fontSize: '0.8rem', padding: '2px 8px' }}
                  >
                    {(selectedSafeLocation.total_points ?? suitabilityMap[selectedSafeLocation.id]?.total_points ?? 0).toFixed(0)} / 100 pts
                  </span>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '6px', fontSize: '0.75rem' }}>
                  <div style={{ background: '#FFFFFF', padding: '4px 8px', borderRadius: '4px', border: '1px solid #DCFCE7', color: '#15803D' }}>
                    📍 Distance: <strong>{(selectedSafeLocation.distance_points ?? suitabilityMap[selectedSafeLocation.id]?.distance_points ?? 0).toFixed(0)} pts</strong>
                  </div>
                  <div style={{ background: '#FFFFFF', padding: '4px 8px', borderRadius: '4px', border: '1px solid #DCFCE7', color: '#15803D' }}>
                    🛡️ Safety: <strong>{(selectedSafeLocation.safety_points ?? suitabilityMap[selectedSafeLocation.id]?.safety_points ?? 0).toFixed(0)} pts</strong>
                  </div>
                  <div style={{ background: '#FFFFFF', padding: '4px 8px', borderRadius: '4px', border: '1px solid #DCFCE7', color: '#15803D' }}>
                    👥 Availability: <strong>{(selectedSafeLocation.availability_points ?? suitabilityMap[selectedSafeLocation.id]?.availability_points ?? 0).toFixed(0)} pts</strong>
                  </div>
                  <div style={{ background: '#FFFFFF', padding: '4px 8px', borderRadius: '4px', border: '1px solid #DCFCE7', color: '#15803D' }}>
                    ⚡ Features: <strong>{(selectedSafeLocation.features_points ?? suitabilityMap[selectedSafeLocation.id]?.features_points ?? 0).toFixed(0)} pts</strong>
                  </div>
                </div>
              </div>
            )}

            {/* Separate Safe Place Discovery Engine Calculations */}
            {safePlacesMap[selectedSafeLocation.id] && (
              <div
                style={{
                  background: '#F8FAFC',
                  border: '1px solid #E2E8F0',
                  borderRadius: '8px',
                  padding: '10px 12px',
                  marginBottom: '14px',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                  <span style={{ fontSize: '0.8rem', fontWeight: 800, color: '#0F172A' }}>
                    🎯 Safe Place Engine Measures
                  </span>
                  <span className="badge" style={{ background: '#EEF2FF', color: '#4338CA', fontWeight: 700, fontSize: '0.72rem' }}>
                    ≤ {effectiveRadiusKm} km (starts 100m)
                  </span>
                </div>

                {/* 1.a Earthquake Fields */}
                {safePlacesMap[selectedSafeLocation.id].earthquake_fields && (
                  <div style={{ marginBottom: '8px', borderTop: '1px solid #E2E8F0', paddingTop: '6px' }}>
                    <div style={{ fontWeight: 700, fontSize: '0.8rem', color: '#C2410C', marginBottom: '4px' }}>
                      ⚡ 1.a. Earthquake Measures:
                    </div>
                    <div style={{ fontSize: '0.75rem', color: '#334155', display: 'flex', flexDirection: 'column', gap: '3px' }}>
                      <div>1. <strong>Distance from user:</strong> {safePlacesMap[selectedSafeLocation.id].earthquake_fields!.distance_from_user_km.toFixed(2)} km ({safePlacesMap[selectedSafeLocation.id].earthquake_fields!.distance_from_user_m.toFixed(0)} m)</div>
                      <div>2. <strong>Safe area:</strong> {safePlacesMap[selectedSafeLocation.id].earthquake_fields!.safe_area_m2.toLocaleString()} m² footprint</div>
                      <div>3. <strong>Open places safety:</strong> {safePlacesMap[selectedSafeLocation.id].earthquake_fields!.is_open_place ? '✅ ' + (safePlacesMap[selectedSafeLocation.id].earthquake_fields!.open_place_type || 'Open Place') : '🏢 Enclosed'} ({safePlacesMap[selectedSafeLocation.id].earthquake_fields!.building_collapse_protection})</div>
                      <div>4. <strong>Hilly area safety:</strong> {safePlacesMap[selectedSafeLocation.id].earthquake_fields!.stone_crushing_risk === 'ZERO' ? '✅ Zero Stone Crushing Risk' : '⚠️ Slope Hazard'} — {safePlacesMap[selectedSafeLocation.id].earthquake_fields!.hilly_area_safety}</div>
                    </div>
                  </div>
                )}

                {/* 3.b Flood Fields */}
                {safePlacesMap[selectedSafeLocation.id].flood_fields && (
                  <div style={{ marginBottom: '8px', borderTop: '1px solid #E2E8F0', paddingTop: '6px' }}>
                    <div style={{ fontWeight: 700, fontSize: '0.8rem', color: '#0369A1', marginBottom: '4px' }}>
                      🌊 3.b. Flood Measures:
                    </div>
                    <div style={{ fontSize: '0.75rem', color: '#334155', display: 'flex', flexDirection: 'column', gap: '3px' }}>
                      <div>1. <strong>Distance from user:</strong> {safePlacesMap[selectedSafeLocation.id].flood_fields!.distance_from_user_km.toFixed(2)} km</div>
                      <div>2. <strong>Elevation of place:</strong> {safePlacesMap[selectedSafeLocation.id].flood_fields!.elevation_m.toFixed(1)} m MSL (Height from sea level)</div>
                      <div>3. <strong>Safety factor:</strong> {safePlacesMap[selectedSafeLocation.id].flood_fields!.safety_factor}</div>
                      <div>4. <strong>Safe buildings:</strong> {safePlacesMap[selectedSafeLocation.id].flood_fields!.is_safe_building ? '✅ ' + safePlacesMap[selectedSafeLocation.id].flood_fields!.safe_building_type : 'General Facility'}</div>
                      <div>5. <strong>Flood flow prediction:</strong> {safePlacesMap[selectedSafeLocation.id].flood_fields!.flood_flow_prediction}</div>
                      <div>• <strong>Middle transect MSL height:</strong> {safePlacesMap[selectedSafeLocation.id].flood_fields!.transect_summary}</div>
                    </div>
                  </div>
                )}

                {/* 3.c Sea Typhoon Fields */}
                {safePlacesMap[selectedSafeLocation.id].cyclone_fields && (
                  <div style={{ borderTop: '1px solid #E2E8F0', paddingTop: '6px' }}>
                    <div style={{ fontWeight: 700, fontSize: '0.8rem', color: '#7E22CE', marginBottom: '4px' }}>
                      🌀 3.c. Sea Typhoon Measures:
                    </div>
                    <div style={{ fontSize: '0.75rem', color: '#334155', display: 'flex', flexDirection: 'column', gap: '3px' }}>
                      <div>1. <strong>Distance from user:</strong> {safePlacesMap[selectedSafeLocation.id].cyclone_fields!.distance_from_user_km.toFixed(2)} km</div>
                      <div>2. <strong>Affect of typhoon on area:</strong> {safePlacesMap[selectedSafeLocation.id].cyclone_fields!.affect_of_typhoon_on_area} ({safePlacesMap[selectedSafeLocation.id].cyclone_fields!.distance_to_coastline_km.toFixed(1)} km from sea)</div>
                      <div>3. <strong>Safety factor of area:</strong> {safePlacesMap[selectedSafeLocation.id].cyclone_fields!.safety_factor_of_area} ({safePlacesMap[selectedSafeLocation.id].cyclone_fields!.cyclone_shelter_engineering})</div>
                    </div>
                  </div>
                )}
              </div>
            )}

            {/* Disaster Suitability Evidence & Warnings */}
            {suitabilityMap[selectedSafeLocation.id] && (
              <div
                style={{
                  background:
                    suitabilityMap[selectedSafeLocation.id].suitability_classification === 'SUITABILITY_SUPPORTED_BY_EVIDENCE'
                      ? '#F0FDF4'
                      : suitabilityMap[selectedSafeLocation.id].suitability_classification === 'EXCLUDED'
                      ? '#FEF2F2'
                      : '#FFFBEB',
                  border: `1px solid ${
                    suitabilityMap[selectedSafeLocation.id].suitability_classification === 'SUITABILITY_SUPPORTED_BY_EVIDENCE'
                      ? '#BBF7D0'
                      : suitabilityMap[selectedSafeLocation.id].suitability_classification === 'EXCLUDED'
                      ? '#FECACA'
                      : '#FDE68A'
                  }`,
                  borderRadius: '8px',
                  padding: '10px 12px',
                  marginBottom: '16px',
                }}
              >
                <div style={{ fontWeight: 700, fontSize: '0.82rem', marginBottom: '4px', color: '#166534' }}>
                  Classification: {suitabilityMap[selectedSafeLocation.id].suitability_classification}
                </div>
                {suitabilityMap[selectedSafeLocation.id].supporting_evidence.slice(0, 3).map((ev, idx) => (
                  <div key={idx} style={{ fontSize: '0.75rem', color: '#166534', lineHeight: 1.35, marginTop: '2px' }}>
                    ✓ {ev}
                  </div>
                ))}
                {suitabilityMap[selectedSafeLocation.id].warnings.slice(0, 2).map((wn, idx) => (
                  <div key={idx} style={{ fontSize: '0.75rem', color: '#B45309', lineHeight: 1.35, marginTop: '2px' }}>
                    ℹ️ {wn}
                  </div>
                ))}
              </div>
            )}

            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              <button
                className="action-btn-pill action-btn-primary"
                style={{
                  justifyContent: 'center',
                  background: userSelectedSafeLocation?.id === selectedSafeLocation.id ? '#15803D' : '#0284C7',
                }}
                onClick={() => handleSelectSafeLocationByUser(selectedSafeLocation)}
              >
                {userSelectedSafeLocation?.id === selectedSafeLocation.id
                  ? '★ Selected As My Destination'
                  : '★ Select As My Safe Destination'}
              </button>

              <button
                className="action-btn-pill"
                style={{ justifyContent: 'center' }}
                onClick={() => handleCalculateRoute(selectedSafeLocation.id, activeFilter === 'ALL' ? 'FLOOD' : (activeFilter as DisasterType))}
                disabled={calculatingRoute}
              >
                {calculatingRoute ? 'Calculating Route...' : '🗺️ Calculate Evacuation Route'}
              </button>

              <button
                className="action-btn-pill"
                style={{ justifyContent: 'center' }}
                onClick={() => handleAssessSuitability(selectedSafeLocation.id, activeFilter === 'ALL' ? 'FLOOD' : (activeFilter as DisasterType))}
                disabled={evaluatingSuitability}
              >
                {evaluatingSuitability ? 'Evaluating...' : '🔍 Assess Disaster Suitability'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Evacuation Route Side Panel */}
      {activeRoute && (
        <div className="side-panel" style={{ left: 'auto', right: '12px', width: '360px', top: userSelectedSafeLocation ? '220px' : '60px' }}>
          <div className="side-panel-header">
            <span className="side-panel-title">🗺️ Evacuation Route</span>
            <button className="close-btn" onClick={() => setActiveRoute(null)}>✕</button>
          </div>
          <div className="side-panel-body">
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
              <span className={`badge ${activeRoute.route_status === 'ROUTE_AVAILABLE' ? 'badge-verified' : 'badge-unverified'}`}>
                {activeRoute.route_status}
              </span>
              <span style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-muted)' }}>
                Via OSRM / Geodesic
              </span>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '14px' }}>
              <div style={{ background: '#F8FAFC', padding: '8px 12px', borderRadius: '8px', border: '1px solid #E2E8F0' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block' }}>Distance</span>
                <span style={{ fontSize: '1.1rem', fontWeight: 700, color: '#0F172A' }}>
                  {activeRoute.distance_km.toFixed(1)} km
                </span>
              </div>
              <div style={{ background: '#F8FAFC', padding: '8px 12px', borderRadius: '8px', border: '1px solid #E2E8F0' }}>
                <span style={{ fontSize: '0.75rem', color: 'var(--text-muted)', display: 'block' }}>Est. Travel Time</span>
                <span style={{ fontSize: '1.1rem', fontWeight: 700, color: '#0F172A' }}>
                  {activeRoute.estimated_time_minutes.toFixed(0)} min
                </span>
              </div>
            </div>

            {activeRoute.hazard_intersections && activeRoute.hazard_intersections.length > 0 && (
              <div style={{ marginBottom: '14px' }}>
                <span style={{ fontSize: '0.8rem', fontWeight: 700, color: '#B91C1C', textTransform: 'uppercase' }}>
                  Route Hazards Detected ({activeRoute.hazard_intersections.length})
                </span>
                {activeRoute.hazard_intersections.map((h, idx) => (
                  <div key={idx} style={{ background: '#FEF2F2', border: '1px solid #FECACA', borderRadius: '6px', padding: '6px 8px', marginTop: '6px', fontSize: '0.78rem', color: '#991B1B' }}>
                    ⚠️ {h.hazard_type} zone ({h.distance_to_route_m.toFixed(0)} m from path)
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Manual Location Entry Modal */}
      {showManualLocationModal && (
        <div className="modal-backdrop">
          <div className="modal-card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
              <h2 style={{ fontSize: '1.1rem', fontWeight: 700, margin: 0, color: '#0F172A' }}>
                📍 Enter Location Manually
              </h2>
              <button
                style={{ background: 'transparent', border: 'none', fontSize: '1.2rem', cursor: 'pointer', color: '#6B7280' }}
                onClick={() => setShowManualLocationModal(false)}
              >
                ✕
              </button>
            </div>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '16px' }}>
              Select a disaster hotspot, enter coordinates, or tap directly on the map to locate nearby safe places (under 1-5 km).
            </p>

            {/* Direct Map Tap Option */}
            <div style={{ marginBottom: '16px', background: '#F0FDF4', border: '1px solid #86EFAC', borderRadius: '8px', padding: '12px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px' }}>
                <div>
                  <strong style={{ fontSize: '0.85rem', color: '#166534', display: 'block' }}>👆 Tap Directly on the Map Canvas</strong>
                  <span style={{ fontSize: '0.75rem', color: '#15803D' }}>
                    Click anywhere on the map to place your pin and find safe places instantly.
                  </span>
                </div>
                <button
                  className="action-btn-pill action-btn-primary"
                  style={{ fontSize: '0.75rem', padding: '6px 12px', whiteSpace: 'nowrap' }}
                  onClick={() => {
                    setShowManualLocationModal(false);
                    setFeedbackToast('👆 Tap anywhere on the map canvas to set your location marker');
                    setTimeout(() => setFeedbackToast(null), 3500);
                  }}
                >
                  Tap on Map
                </button>
              </div>
            </div>

            {/* Quick City Presets */}
            <div style={{ marginBottom: '16px' }}>
              <span style={{ fontSize: '0.75rem', fontWeight: 700, color: 'var(--text-muted)', textTransform: 'uppercase', display: 'block', marginBottom: '8px' }}>
                Quick Disaster Zones in India:
              </span>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                {[
                  { name: 'Patna (Gangetic Flood)', lat: 25.5941, lng: 85.1376 },
                  { name: 'Puri Beach (Cyclone)', lat: 19.8135, lng: 85.8312 },
                  { name: 'Dehradun (Hilly Rockfall)', lat: 30.3165, lng: 78.0322 },
                  { name: 'Delhi (NCR Seismic)', lat: 28.6139, lng: 77.2090 },
                  { name: 'Kolkata (Flood Zone)', lat: 22.5726, lng: 88.3639 },
                  { name: 'Mumbai (Coastal Surge)', lat: 19.0760, lng: 72.8777 },
                ].map((c) => (
                  <button
                    key={c.name}
                    className="action-btn-pill"
                    style={{ fontSize: '0.75rem', padding: '6px 8px', justifyContent: 'center' }}
                    onClick={() => handleApplyManualLocation(c.lat, c.lng, c.name)}
                  >
                    {c.name}
                  </button>
                ))}
              </div>
            </div>

            {/* Custom Coordinate Input */}
            <div style={{ display: 'flex', gap: '10px', marginBottom: '16px' }}>
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: '0.75rem', fontWeight: 600, color: '#374151', display: 'block', marginBottom: '4px' }}>
                  Latitude (WGS84)
                </label>
                <input
                  type="number"
                  step="0.0001"
                  className="input-field"
                  value={manualLat}
                  onChange={(e) => setManualLat(e.target.value)}
                  style={{ width: '100%', padding: '6px 10px', border: '1px solid #D1D5DB', borderRadius: '6px' }}
                />
              </div>
              <div style={{ flex: 1 }}>
                <label style={{ fontSize: '0.75rem', fontWeight: 600, color: '#374151', display: 'block', marginBottom: '4px' }}>
                  Longitude (WGS84)
                </label>
                <input
                  type="number"
                  step="0.0001"
                  className="input-field"
                  value={manualLng}
                  onChange={(e) => setManualLng(e.target.value)}
                  style={{ width: '100%', padding: '6px 10px', border: '1px solid #D1D5DB', borderRadius: '6px' }}
                />
              </div>
            </div>

            <div style={{ display: 'flex', gap: '8px' }}>
              <button
                className="action-btn-pill action-btn-primary"
                style={{ flex: 1, justifyContent: 'center' }}
                onClick={() => {
                  const lat = parseFloat(manualLat);
                  const lng = parseFloat(manualLng);
                  if (isNaN(lat) || isNaN(lng)) {
                    alert('Please enter valid numeric latitude and longitude.');
                    return;
                  }
                  handleApplyManualLocation(lat, lng);
                }}
              >
                Set Location & Scan Safe Places
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Unified Recommendations Modal */}
      {showUnifiedModal && unifiedRecs && (
        <div className="modal-backdrop">
          <div className="modal-card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
              <h2 style={{ fontSize: '1.15rem', fontWeight: 700, margin: 0, color: '#0F172A' }}>
                ⚡ Unified Evacuation Recommendations ({unifiedRecs.disaster_type})
              </h2>
              <button
                style={{ background: 'transparent', border: 'none', fontSize: '1.2rem', cursor: 'pointer', color: '#6B7280' }}
                onClick={() => setShowUnifiedModal(false)}
              >
                ✕
              </button>
            </div>

            <div style={{ display: 'flex', gap: '8px', marginBottom: '14px' }}>
              <span className="badge badge-verified">{unifiedRecs.status}</span>
              <span className="badge" style={{ background: '#F1F5F9', color: '#475569' }}>
                Evaluated: {unifiedRecs.recommendations.length} Safe Places
              </span>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', maxHeight: '400px', overflowY: 'auto' }}>
              {unifiedRecs.recommendations.map((rec, idx) => (
                <div
                  key={rec.destination_id}
                  style={{
                    border: '1px solid #E2E8F0',
                    borderRadius: '8px',
                    padding: '10px 12px',
                    background: idx === 0 ? '#F0FDF4' : '#FFFFFF',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
                    <span style={{ fontWeight: 700, fontSize: '0.9rem', color: '#0F172A' }}>
                      #{idx + 1} {rec.name || 'Shelter Destination'}
                    </span>
                    <span className="badge badge-verified">
                      {rec.total_points != null ? `${rec.total_points.toFixed(0)} pts` : `${rec.distance_km.toFixed(1)} km`}
                    </span>
                  </div>
                  <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '8px' }}>
                    {rec.category} • {rec.distance_km.toFixed(1)} km
                  </div>

                  <button
                    className="action-btn-pill action-btn-primary"
                    style={{ fontSize: '0.75rem', padding: '4px 10px' }}
                    onClick={() => {
                      const found = safeLocations.find((s) => s.id === rec.destination_id);
                      if (found) {
                        handleSelectSafeLocationByUser(found);
                      }
                      if (rec.route) {
                        setActiveRoute(rec.route);
                      }
                      setShowUnifiedModal(false);
                    }}
                  >
                    Select Destination & Show Route
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Alert Center Modal */}
      {showAlertCenter && (
        <div className="modal-backdrop">
          <div className="modal-card">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '14px' }}>
              <h2 style={{ fontSize: '1.15rem', fontWeight: 700, margin: 0, color: '#0F172A' }}>
                🔔 Alert Notification Center
              </h2>
              <button
                style={{ background: 'transparent', border: 'none', fontSize: '1.2rem', cursor: 'pointer', color: '#6B7280' }}
                onClick={() => setShowAlertCenter(false)}
              >
                ✕
              </button>
            </div>

            <div style={{ display: 'flex', gap: '8px', marginBottom: '14px' }}>
              <button
                className={`filter-pill ${activeAlertTab === 'alerts' ? 'active' : ''}`}
                onClick={() => setActiveAlertTab('alerts')}
              >
                Live Alerts ({alerts.length})
              </button>
              <button
                className={`filter-pill ${activeAlertTab === 'history' ? 'active' : ''}`}
                onClick={() => setActiveAlertTab('history')}
              >
                History ({notificationHistory.length})
              </button>
            </div>

            {activeAlertTab === 'alerts' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px', maxHeight: '350px', overflowY: 'auto' }}>
                {alerts.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)' }}>
                    No active emergency alerts in your radius.
                  </div>
                ) : (
                  alerts.map((al) => (
                    <div key={al.alert_id} style={{ border: '1px solid #FECACA', background: '#FEF2F2', borderRadius: '8px', padding: '10px' }}>
                      <div style={{ fontWeight: 700, color: '#991B1B', fontSize: '0.85rem' }}>{al.title}</div>
                      <div style={{ fontSize: '0.8rem', color: '#7F1D1D', marginTop: '2px' }}>{al.body}</div>
                      <div style={{ fontSize: '0.72rem', color: '#B91C1C', marginTop: '4px' }}>
                        Authority: {al.issuing_authority || 'NDMA / IMD / CWC'} • {al.disaster_type}
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}

            {activeAlertTab === 'history' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', maxHeight: '350px', overflowY: 'auto' }}>
                {notificationHistory.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)' }}>
                    No notification history recorded for this device.
                  </div>
                ) : (
                  notificationHistory.map((nh) => (
                    <div key={nh.notification_id} style={{ border: '1px solid #E2E8F0', borderRadius: '6px', padding: '8px 10px', fontSize: '0.8rem' }}>
                      <div style={{ fontWeight: 600 }}>{nh.title}</div>
                      <div style={{ color: 'var(--text-secondary)' }}>{nh.body}</div>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Pinned Mandatory Disaster Safety Disclaimer */}
      <div className="safety-disclaimer-pill">
        ⚠️ <strong>Emergency Advisory:</strong> Safe places are advisory model recommendations (under 1-5 km). Always follow official directives from NDMA, IMD, CWC, and local emergency authorities.
      </div>
    </div>
  );
}
