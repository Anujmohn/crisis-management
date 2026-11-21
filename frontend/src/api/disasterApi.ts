/**
 * Disaster Search API client for the Disaster Safety application.
 *
 * Connects to GET /api/v1/disasters/nearby to retrieve nearby verified
 * earthquake, flood, and cyclone disaster events with PostGIS distance calculations.
 */

const BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ??
  'http://localhost:8000';

export type DisasterType = 'EARTHQUAKE' | 'FLOOD' | 'CYCLONE';
export type CrisisStatus = 'ACTIVE' | 'EXPIRED' | 'CANCELLED' | 'UNKNOWN';
export type SeverityLevel = 'LOW' | 'MODERATE' | 'HIGH' | 'SEVERE' | 'EXTREME';
export type DataFreshness = 'FRESH' | 'STALE' | 'EXPIRED' | 'UNKNOWN';

export interface GeoJSONPoint {
  type: 'Point';
  coordinates: [number, number]; // [longitude, latitude]
}

export interface GeoJSONMultiPolygon {
  type: 'MultiPolygon';
  coordinates: number[][][][]; // [ [ [ [lon, lat], ... ] ] ]
}

export interface GeoJSONLineString {
  type: 'LineString';
  coordinates: number[][]; // [ [lon, lat], ... ]
}

export interface DisasterSourceSummary {
  source_name: string;
  source_type: string;
  source_url: string | null;
  external_event_id: string | null;
  issued_at: string | null;
  fetched_at: string;
  valid_until: string | null;
}

export interface DisasterObservationSummary {
  id: string;
  observation_type: string;
  value_numeric: number | null;
  value_text: string | null;
  unit: string | null;
  geometry: GeoJSONPoint | GeoJSONMultiPolygon | null;
  observed_at: string;
  properties: Record<string, unknown> | null;
}

export interface DisasterEventSummary {
  id: string;
  disaster_type: DisasterType;
  title: string | null;
  description: string | null;
  severity: SeverityLevel | null;
  status: CrisisStatus;
  location: GeoJSONPoint | null;
  affected_area: GeoJSONMultiPolygon | null;
  flow_path?: { type: string; coordinates: any } | null;
  distance_km: number | null;
  started_at: string | null;
  expires_at: string | null;
  first_seen_at: string;
  last_seen_at: string;
  freshness: DataFreshness;
  sources: DisasterSourceSummary[];
  observations: DisasterObservationSummary[];
}

export interface NearbyDisastersResponse {
  success: boolean;
  user_location: {
    latitude: number;
    longitude: number;
  };
  search_radius_km: number;
  total_events: number;
  events: DisasterEventSummary[];
}

export interface FetchDisastersParams {
  latitude: number;
  longitude: number;
  radius_km?: number;
  disaster_types?: DisasterType[];
  status?: CrisisStatus[];
  limit?: number;
}

export type FetchNearbyDisastersResult =
  | { ok: true; data: NearbyDisastersResponse }
  | { ok: false; kind: 'network'; message: string }
  | { ok: false; kind: 'server'; status: number; message: string };

export interface FetchAllActiveDisastersParams {
  disaster_types?: DisasterType[];
  status?: string[];
  limit?: number;
}

export async function fetchAllActiveDisasters(
  params?: FetchAllActiveDisastersParams,
): Promise<FetchNearbyDisastersResult> {
  const queryParams = new URLSearchParams();
  if (params?.disaster_types && params.disaster_types.length > 0) {
    queryParams.append('disaster_types', params.disaster_types.join(','));
  }
  if (params?.status && params.status.length > 0) {
    queryParams.append('status', params.status.join(','));
  }
  if (params?.limit != null) {
    queryParams.append('limit', params.limit.toString());
  }

  const endpoint = `${BASE_URL}/api/v1/disasters/active?${queryParams.toString()}`;

  let response: Response;
  try {
    response = await fetch(endpoint, {
      method: 'GET',
      headers: {
        Accept: 'application/json',
      },
    });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Failed to connect to disaster service';
    return { ok: false, kind: 'network', message };
  }

  if (!response.ok) {
    return { ok: false, kind: 'server', status: response.status, message: `Server error ${response.status}` };
  }

  try {
    const data: NearbyDisastersResponse = await response.json();
    return { ok: true, data };
  } catch {
    return { ok: false, kind: 'server', status: response.status, message: 'Invalid response payload' };
  }
}

export async function fetchNearbyDisasters(
  params: FetchDisastersParams,
): Promise<FetchNearbyDisastersResult> {
  const queryParams = new URLSearchParams();
  queryParams.append('latitude', params.latitude.toString());
  queryParams.append('longitude', params.longitude.toString());

  if (params.radius_km != null) {
    queryParams.append('radius_km', params.radius_km.toString());
  }
  if (params.disaster_types && params.disaster_types.length > 0) {
    queryParams.append('disaster_types', params.disaster_types.join(','));
  }
  if (params.status && params.status.length > 0) {
    queryParams.append('status', params.status.join(','));
  }
  if (params.limit != null) {
    queryParams.append('limit', params.limit.toString());
  }

  const endpoint = `${BASE_URL}/api/v1/disasters/nearby?${queryParams.toString()}`;

  let response: Response;
  try {
    response = await fetch(endpoint, {
      method: 'GET',
      headers: {
        Accept: 'application/json',
      },
    });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Failed to connect to disaster service';
    return { ok: false, kind: 'network', message };
  }

  if (!response.ok) {
    let message = `Server error ${response.status}`;
    try {
      const errBody = await response.json();
      if (errBody.detail) {
        message = typeof errBody.detail === 'string' ? errBody.detail : JSON.stringify(errBody.detail);
      }
    } catch {
      // Keep default message
    }
    return { ok: false, kind: 'server', status: response.status, message };
  }

  try {
    const data: NearbyDisastersResponse = await response.json();
    return { ok: true, data };
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Invalid JSON response from server';
    return { ok: false, kind: 'network', message };
  }
}
