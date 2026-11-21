/**
 * MapScreen (MapLibre GL JS Web Implementation)
 *
 * Displays an interactive MapLibre GL JS web map with:
 * 1. Complete real-time natural disaster events occurring across India on the current time
 *    (Earthquakes, Floods, and Cyclones with live bulletin timestamps & physical measurements).
 * 2. User-selected Safe Location identification & selection:
 *    - User can select any verified emergency shelter or candidate facility via direct map click
 *      or the top safe location selector dropdown.
 *    - Selected safe location is highlighted with a distinctive golden marker, status badge,
 *      and dedicated destination overview card.
 *    - Automatically calculates and renders the road evacuation route from user to destination.
 * 3. User GPS fix from HTML5 Browser Geolocation API, validated with POST /api/v1/location/validate.
 * 4. Affected area Polygon/MultiPolygon GeoJSON overlays.
 * 5. Destination suitability assessment via POST /api/v1/recommendations/assess.
 * 6. Evacuation route calculation and hazard avoidance via POST /api/v1/routes/calculate.
 * 7. Unified disaster recommendations via POST /api/v1/recommendations/unified.
 * 8. Real-time disaster alerts, notification history, preferences, and manual sync.
 * 9. Pinned Disaster Safety Disclaimer.
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
  SafeLocationItem,
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
    if (ev.affected_area.type === 'MultiPolygon' && coords[0]?.[0]?.[0]) {
      return [coords[0][0][0][0], coords[0][0][0][1]];
    }
    if (coords[0]?.[0]) {
      return [coords[0][0][0], coords[0][0][1]];
    }
  }
  return null;
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

  // Safe Locations across India
  const [safeLocations, setSafeLocations] = useState<SafeLocationItem[]>([]);
  const [selectedSafeLocation, setSelectedSafeLocation] = useState<SafeLocationItem | null>(null);
  const [userSelectedSafeLocation, setUserSelectedSafeLocation] = useState<SafeLocationItem | null>(null);
  const [suitabilityMap, setSuitabilityMap] = useState<Record<string, LocationSuitabilityAssessmentItem>>({});
  const [evaluatingSuitability, setEvaluatingSuitability] = useState(false);

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
          'fill-opacity': 0.25,
        },
      });

      map.addLayer({
        id: 'affected-areas-outline',
        type: 'line',
        source: 'affected-areas',
        paint: {
          'line-color': ['get', 'strokeColor'],
          'line-width': 2,
        },
      });

      // Add GeoJSON source and layer for Evacuation Route
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
        },
      });

      mapRef.current = map;
    });

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
  // Requirement 2: Load All Safe Locations Across India
  // -------------------------------------------------------------
  const loadSafeLocations = useCallback(async () => {
    try {
      const res = await fetchAllSafeLocations({ limit: 100 });
      if (res.success) {
        setSafeLocations(res.locations);
      }
    } catch (err) {
      console.warn('Failed to fetch safe locations:', err);
    }
  }, []);

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

  // Load disasters and safe locations across India immediately on mount and filter changes
  useEffect(() => {
    loadDisasters();
    loadSafeLocations();
    loadAlerts();
  }, [loadDisasters, loadSafeLocations, loadAlerts]);

  // Update Disaster Markers on Map
  useEffect(() => {
    if (!mapRef.current) return;

    // Remove existing disaster markers
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

  // Update Safe Location Markers on Map with Special Selected Marker Styling
  useEffect(() => {
    if (!mapRef.current) return;

    safeLocationMarkersRef.current.forEach((m) => m.remove());
    safeLocationMarkersRef.current = [];

    safeLocations.forEach((loc) => {
      if (!loc.location || !loc.location.coordinates) return;
      const [lng, lat] = loc.location.coordinates;

      const isSelected = userSelectedSafeLocation?.id === loc.id;
      const isOfficial = loc.designation_status === 'OFFICIAL';
      const bgColor = isSelected ? '#15803D' : isOfficial ? '#16A34A' : '#D97706';

      const el = document.createElement('div');
      el.className = `custom-maplibre-marker ${isSelected ? 'selected-safe-marker' : ''}`;
      el.innerHTML = `
        <div class="marker-inner" style="background-color: ${bgColor}; width: ${isSelected ? '36px' : '28px'}; height: ${isSelected ? '36px' : '28px'}; font-size: ${isSelected ? '18px' : '14px'}; border-color: ${isSelected ? '#F59E0B' : '#FFFFFF'};">
          ${isSelected ? '★' : '🛡️'}
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
  }, [safeLocations, userSelectedSafeLocation]);

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

    // Fit map bounds to route coordinates
    if (activeRoute.route_geometry?.coordinates?.length) {
      const bounds = new maplibregl.LngLatBounds();
      activeRoute.route_geometry.coordinates.forEach((coord) => bounds.extend(coord as [number, number]));
      if (userLoc.location) {
        bounds.extend([userLoc.location.longitude, userLoc.location.latitude]);
      }
      mapRef.current.fitBounds(bounds, { padding: 80, duration: 800 });
    }
  }, [activeRoute, userLoc.location]);

  // Assess Destination Suitability Action
  const handleAssessSuitability = useCallback(
    async (safeLocationId: string, disasterType: DisasterType) => {
      const lat = userLoc.location?.latitude ?? 20.5937;
      const lng = userLoc.location?.longitude ?? 78.9629;
      setEvaluatingSuitability(true);
      try {
        const res = await assessSafeLocationsSuitability({
          latitude: lat,
          longitude: lng,
          disaster_type: disasterType,
          search_radius_km: 1000,
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
      const lat = userLoc.location?.latitude ?? 20.5937;
      const lng = userLoc.location?.longitude ?? 78.9629;
      setCalculatingRoute(true);
      try {
        // First ensure candidate is assessed to retrieve assessment ID
        let assessmentId = suitabilityMap[safeLocationId]?.assessment_id;
        if (!assessmentId) {
          const suitRes = await assessSafeLocationsSuitability({
            latitude: lat,
            longitude: lng,
            disaster_type: disasterType,
            search_radius_km: 1000,
            limit: 20,
          });
          const match = suitRes.assessed_locations.find((l) => l.location_id === safeLocationId);
          assessmentId = match?.assessment_id;
        }

        if (!assessmentId) {
          alert('Could not generate location assessment context for routing.');
          return;
        }

        const res = await calculateEvacuationRoute({
          origin_latitude: lat,
          origin_longitude: lng,
          location_assessment_id: assessmentId,
          allow_alternatives: true,
        });

        if (res.success && res.primary_route) {
          setActiveRoute(res.primary_route);
        } else {
          alert('No safe evacuation route could be calculated.');
        }
      } catch (err) {
        alert(err instanceof Error ? err.message : 'Route calculation failed.');
      } finally {
        setCalculatingRoute(false);
      }
    },
    [userLoc.location, suitabilityMap],
  );

  // Requirement 2: Selection of Safe Location by User
  const handleSelectSafeLocationByUser = useCallback((loc: SafeLocationItem) => {
    setUserSelectedSafeLocation(loc);
    setSelectedSafeLocation(loc);

    // Center map on the selected safe location
    if (loc.location?.coordinates && mapRef.current) {
      const [lng, lat] = loc.location.coordinates;
      mapRef.current.flyTo({
        center: [lng, lat],
        zoom: 13,
        essential: true,
      });
    }

    // Automatically trigger evacuation route calculation to this selected safe destination
    if (userLoc.location) {
      handleCalculateRoute(loc.id, activeFilter === 'ALL' ? 'FLOOD' : (activeFilter as DisasterType));
    }
  }, [userLoc.location, activeFilter, handleCalculateRoute]);

  // Unified Recommendation Workflow
  const handleGetUnifiedRecommendations = useCallback(
    async (disasterType: DisasterType) => {
      const lat = userLoc.location?.latitude ?? 20.5937;
      const lng = userLoc.location?.longitude ?? 78.9629;
      setLoadingUnifiedRecs(true);
      try {
        const res = await fetchUnifiedRecommendations({
          latitude: lat,
          longitude: lng,
          disaster_type: disasterType,
          search_radius_km: 500,
          max_destinations: 5,
        });

        setUnifiedRecs(res);
        setShowUnifiedModal(true);

        // Highlight top recommended route if available
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

  // Helper counters for current India disaster summary
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
          {/* Requirement 2: User Safe Location Selector Dropdown */}
          <select
            className="action-btn-pill"
            style={{
              padding: '6px 12px',
              outline: 'none',
              maxWidth: '260px',
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
            <option value="">🛡️ Select Safe Location...</option>
            {safeLocations.map((loc) => (
              <option key={loc.id} value={loc.id}>
                {loc.name || 'Shelter'} ({loc.category})
              </option>
            ))}
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
            onClick={handleAcquireLocation}
            title="Recenter GPS"
          >
            📍 My GPS
          </button>
        </div>
      </div>

      {/* Main Map Canvas */}
      <div className="map-container" ref={mapContainerRef} />

      {/* Requirement 2: Dedicated User-Selected Safe Location Floating Banner */}
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

      {/* Safe Location Information Side Panel */}
      {selectedSafeLocation && (
        <div className="side-panel">
          <div className="side-panel-header">
            <span className="side-panel-title">🛡️ Safe Destination</span>
            <button className="close-btn" onClick={() => setSelectedSafeLocation(null)}>✕</button>
          </div>
          <div className="side-panel-body">
            <h3 style={{ fontSize: '1.05rem', fontWeight: 700, color: 'var(--text-primary)', marginBottom: '4px' }}>
              {selectedSafeLocation.name || 'Emergency Evacuation Site'}
            </h3>
            <p style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', marginBottom: '12px' }}>
              Category: {selectedSafeLocation.category} • Distance: {selectedSafeLocation.distance_km != null ? `${selectedSafeLocation.distance_km.toFixed(1)} km` : 'Regional shelter'}
            </p>

            <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap', marginBottom: '16px' }}>
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

            {/* Suitability Assessment Result if performed */}
            {suitabilityMap[selectedSafeLocation.id] && (
              <div style={{ background: '#F0FDF4', border: '1px solid #BBF7D0', borderRadius: '8px', padding: '12px', marginBottom: '16px' }}>
                <div style={{ fontWeight: 700, color: '#166534', fontSize: '0.85rem', marginBottom: '4px' }}>
                  Suitability: {suitabilityMap[selectedSafeLocation.id].suitability_classification}
                </div>
                {suitabilityMap[selectedSafeLocation.id].supporting_evidence.length > 0 && (
                  <div style={{ fontSize: '0.8rem', color: '#14532D', lineHeight: 1.4 }}>
                    ✓ {suitabilityMap[selectedSafeLocation.id].supporting_evidence[0]}
                  </div>
                )}
              </div>
            )}

            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {/* Requirement 2: Select by User Action Button */}
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
              <span className={`badge ${activeRoute.route_status === 'ROUTE_AVAILABLE' ? 'badge-route-available' : activeRoute.route_status === 'ROUTE_WITH_WARNINGS' ? 'badge-route-warnings' : 'badge-route-excluded'}`}>
                {activeRoute.route_status}
              </span>
              <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                Provider: {activeRoute.routing_provider}
              </span>
            </div>

            <div className="data-table-card" style={{ marginBottom: '16px' }}>
              <div className="data-row">
                <span className="data-label">Distance</span>
                <span className="data-value">{activeRoute.distance_km.toFixed(1)} km</span>
              </div>
              <div className="data-row">
                <span className="data-label">Estimated Time</span>
                <span className="data-value">{activeRoute.estimated_time_minutes.toFixed(0)} mins</span>
              </div>
              <div className="data-row">
                <span className="data-label">Hazard Exposure</span>
                <span className="data-value">{activeRoute.hazard_exposure_level}</span>
              </div>
              <div className="data-row">
                <span className="data-label">Hazard Intersections</span>
                <span className="data-value">{activeRoute.hazard_intersections_count}</span>
              </div>
            </div>

            {activeRoute.warnings.length > 0 && (
              <div style={{ background: '#FFFBEB', border: '1px solid #FDE68A', borderRadius: '8px', padding: '10px', marginBottom: '12px' }}>
                <span style={{ fontWeight: 700, color: '#92400E', fontSize: '0.8rem' }}>⚠️ Route Warnings:</span>
                <ul style={{ paddingLeft: '16px', fontSize: '0.8rem', color: '#B45309', marginTop: '4px' }}>
                  {activeRoute.warnings.map((w, idx) => (
                    <li key={idx}>{w}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Unified Recommendations Drawer / Modal */}
      {showUnifiedModal && unifiedRecs && (
        <div className="side-panel" style={{ width: '460px', maxHeight: 'calc(100% - 80px)' }}>
          <div className="side-panel-header">
            <span className="side-panel-title">⚡ Unified Recommendations</span>
            <button className="close-btn" onClick={() => setShowUnifiedModal(false)}>✕</button>
          </div>
          <div className="side-panel-body">
            <div style={{ marginBottom: '12px' }}>
              <span className="badge badge-official">
                Status: {unifiedRecs.status}
              </span>
            </div>

            {/* Disaster Hazard Summary */}
            <div style={{ background: '#F8FAFC', border: '1px solid #E2E8F0', borderRadius: '8px', padding: '12px', marginBottom: '16px' }}>
              <div style={{ fontWeight: 700, fontSize: '0.85rem', color: 'var(--text-primary)', marginBottom: '4px' }}>
                Disaster Hazard Context ({unifiedRecs.hazard_assessment.disaster_type})
              </div>
              <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                {unifiedRecs.hazard_assessment.summary}
              </p>
            </div>

            {/* Ranked Destinations */}
            <h4 style={{ fontSize: '0.9rem', fontWeight: 700, color: 'var(--text-primary)', marginBottom: '8px' }}>
              Ranked Safe Destinations ({unifiedRecs.recommendations.length})
            </h4>

            {unifiedRecs.recommendations.length === 0 ? (
              <div style={{ padding: '16px', textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                No suitable safe destinations identified within search parameters.
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                {unifiedRecs.recommendations.map((rec, idx) => (
                  <div
                    key={rec.destination_id}
                    style={{
                      border: '1px solid #E2E8F0',
                      borderRadius: '8px',
                      padding: '12px',
                      background: '#FFFFFF',
                      cursor: 'pointer',
                      transition: 'border-color 0.15s ease',
                    }}
                    onClick={() => {
                      if (rec.route) {
                        setActiveRoute(rec.route);
                      }
                      const match = safeLocations.find((s) => s.id === rec.destination_id);
                      if (match) {
                        handleSelectSafeLocationByUser(match);
                      }
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                      <span style={{ fontWeight: 700, fontSize: '0.9rem', color: 'var(--text-primary)' }}>
                        #{idx + 1} {rec.name || 'Shelter Site'}
                      </span>
                      <span className="badge badge-verified">
                        Score: {rec.ranking_score != null ? rec.ranking_score.toFixed(1) : 'N/A'}
                      </span>
                    </div>
                    <div style={{ fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '6px' }}>
                      Category: {rec.category} • Suitability: {rec.suitability_classification}
                    </div>
                    {rec.route && (
                      <div style={{ fontSize: '0.8rem', color: '#0369A1' }}>
                        🚗 Route: {rec.route.distance_km.toFixed(1)} km (~{rec.route.estimated_time_minutes.toFixed(0)} mins)
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Alert Notification Center Drawer */}
      {showAlertCenter && (
        <div className="side-panel" style={{ width: '420px', left: 'auto', right: '12px' }}>
          <div className="side-panel-header">
            <span className="side-panel-title">🔔 Alert & Notification Center</span>
            <button className="close-btn" onClick={() => setShowAlertCenter(false)}>✕</button>
          </div>

          <div style={{ display: 'flex', borderBottom: '1px solid #E2E8F0', background: '#F8FAFC' }}>
            <button
              className={`nav-tab-btn ${activeAlertTab === 'alerts' ? 'active' : ''}`}
              style={{ flex: 1, borderRadius: 0 }}
              onClick={() => setActiveAlertTab('alerts')}
            >
              Alerts ({alerts.length})
            </button>
            <button
              className={`nav-tab-btn ${activeAlertTab === 'history' ? 'active' : ''}`}
              style={{ flex: 1, borderRadius: 0 }}
              onClick={() => setActiveAlertTab('history')}
            >
              History ({notificationHistory.length})
            </button>
            <button
              className={`nav-tab-btn ${activeAlertTab === 'preferences' ? 'active' : ''}`}
              style={{ flex: 1, borderRadius: 0 }}
              onClick={() => setActiveAlertTab('preferences')}
            >
              Settings
            </button>
          </div>

          <div className="side-panel-body">
            {activeAlertTab === 'alerts' && (
              <div>
                <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: '12px' }}>
                  <button
                    className="action-btn-pill"
                    style={{ fontSize: '0.75rem', padding: '4px 10px' }}
                    onClick={handleManualSync}
                    disabled={syncingAlerts}
                  >
                    {syncingAlerts ? 'Syncing...' : '↻ Trigger Feed Sync'}
                  </button>
                </div>

                {alerts.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                    No active emergency alerts reported in this radius.
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {alerts.map((al) => (
                      <div key={al.alert_id} style={{ border: '1px solid #FECACA', background: '#FEF2F2', borderRadius: '8px', padding: '10px' }}>
                        <div style={{ fontWeight: 700, color: '#991B1B', fontSize: '0.85rem', marginBottom: '2px' }}>
                          {al.title}
                        </div>
                        <div style={{ fontSize: '0.8rem', color: '#7F1D1D', marginBottom: '6px' }}>
                          {al.body}
                        </div>
                        <div style={{ fontSize: '0.75rem', color: '#B91C1C' }}>
                          Authority: {al.issuing_authority || 'NDMA / IMD / NCS'} • {al.disaster_type}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {activeAlertTab === 'history' && (
              <div>
                {notificationHistory.length === 0 ? (
                  <div style={{ textAlign: 'center', padding: '24px', color: 'var(--text-muted)', fontSize: '0.85rem' }}>
                    No notification history found for this device.
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {notificationHistory.map((item) => (
                      <div key={item.notification_id} style={{ border: '1px solid #E2E8F0', borderRadius: '8px', padding: '10px', background: item.is_read ? '#F8FAFC' : '#FFFFFF' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '4px' }}>
                          <span style={{ fontWeight: 600, fontSize: '0.85rem', color: 'var(--text-primary)' }}>
                            {item.title}
                          </span>
                          {!item.is_read && (
                            <button
                              style={{ background: 'transparent', border: 'none', color: 'var(--accent)', fontSize: '0.75rem', cursor: 'pointer' }}
                              onClick={async () => {
                                await markNotificationRead(item.notification_id);
                                loadAlerts();
                              }}
                            >
                              Mark Read
                            </button>
                          )}
                        </div>
                        <p style={{ fontSize: '0.8rem', color: 'var(--text-secondary)' }}>{item.body}</p>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {activeAlertTab === 'preferences' && devicePrefs && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <span style={{ fontWeight: 700, fontSize: '0.85rem', color: 'var(--text-primary)' }}>
                  Device Alert Subscriptions
                </span>

                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.85rem' }}>
                  <input
                    type="checkbox"
                    checked={devicePrefs.alert_earthquake}
                    onChange={async (e) => {
                      const updated = await updateDevicePreferences({
                        device_id: getDeviceId(),
                        alert_earthquake: e.target.checked,
                      });
                      setDevicePrefs(updated);
                    }}
                  />
                  Earthquake Alerts (NCS)
                </label>

                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.85rem' }}>
                  <input
                    type="checkbox"
                    checked={devicePrefs.alert_flood}
                    onChange={async (e) => {
                      const updated = await updateDevicePreferences({
                        device_id: getDeviceId(),
                        alert_flood: e.target.checked,
                      });
                      setDevicePrefs(updated);
                    }}
                  />
                  Flood Alerts (CWC)
                </label>

                <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.85rem' }}>
                  <input
                    type="checkbox"
                    checked={devicePrefs.alert_cyclone}
                    onChange={async (e) => {
                      const updated = await updateDevicePreferences({
                        device_id: getDeviceId(),
                        alert_cyclone: e.target.checked,
                      });
                      setDevicePrefs(updated);
                    }}
                  />
                  Cyclone Alerts (IMD / INCOIS)
                </label>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Mandatory Safety Disclaimer Banner */}
      <div className="disclaimer-banner">
        <span>⚠️</span>
        <span>
          <strong>Emergency Warning</strong>: System hazard assessments reflect software model evaluations and reported observations only. Absence of detected evidence does not guarantee absence of danger. Strictly follow official directives from NDMA, SDMAs, IMD, and local emergency personnel.
        </span>
      </div>
    </div>
  );
}
