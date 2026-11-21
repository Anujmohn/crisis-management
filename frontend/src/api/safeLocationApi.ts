/**
 * Safe Locations API client for the Disaster Safety Application.
 *
 * Connects to:
 *   - GET /api/v1/safe-locations/nearby
 *   - GET /api/v1/safe-locations/{location_id}
 *   - POST /api/v1/safe-locations/sync
 */

import { GeoJSONPoint } from './disasterApi';

const BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ??
  'http://localhost:8000';

export type DesignationStatus = 'OFFICIAL' | 'POTENTIAL' | 'UNKNOWN';
export type VerificationStatus = 'VERIFIED' | 'UNVERIFIED' | 'REJECTED';

export interface SafeLocationSourceSummary {
  data_source_id: string;
  data_source_code: string;
  data_source_name: string;
  external_feature_id: string | null;
  source_url: string | null;
  last_seen_at: string;
  verification_detail: string | null;
  properties: Record<string, unknown> | null;
}

export interface SafeLocationItem {
  id: string;
  name: string | null;
  category: string;
  designation_status: DesignationStatus;
  verification_status: VerificationStatus;
  verification_detail: string;
  location: GeoJSONPoint;
  boundary?: unknown | null;
  structural_condition?: string | null;
  accessibility_status?: string | null;
  capacity?: number | null;
  distance_km?: number | null;
  sources: SafeLocationSourceSummary[];
  missing_information: string[];
  created_at: string;
  updated_at: string;
}

export interface NearbySafeLocationsResponse {
  success: boolean;
  user_location: { latitude: number; longitude: number };
  search_radius_km: number;
  total_found: number;
  locations: SafeLocationItem[];
  official_shelters_count: number;
  candidate_locations_count: number;
  open_assembly_areas_count: number;
  data_sources_queried: string[];
  data_integrity_notice: string;
}

export interface SafeLocationDetailResponse {
  success: boolean;
  location: SafeLocationItem;
  verification_evidence: Record<string, unknown>;
  disaster_suitability_disclaimer: string;
}

export interface FetchNearbyLocationsParams {
  latitude: number;
  longitude: number;
  radius_km?: number;
  category?: string;
  verification_status?: VerificationStatus;
  limit?: number;
}

export async function fetchNearbySafeLocations(
  params: FetchNearbyLocationsParams,
): Promise<NearbySafeLocationsResponse> {
  const query = new URLSearchParams({
    latitude: String(params.latitude),
    longitude: String(params.longitude),
    radius_km: String(params.radius_km ?? 25),
    limit: String(params.limit ?? 50),
  });

  if (params.category) {
    query.set('category', params.category);
  }
  if (params.verification_status) {
    query.set('verification_status', params.verification_status);
  }

  const url = `${BASE_URL}/api/v1/safe-locations/nearby?${query.toString()}`;
  const response = await fetch(url, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch safe locations: ${response.status} ${response.statusText}`);
  }

  return response.json();
}

export async function fetchAllSafeLocations(
  params?: { category?: string; verification_status?: VerificationStatus; limit?: number },
): Promise<NearbySafeLocationsResponse> {
  const query = new URLSearchParams();
  if (params?.category) query.set('category', params.category);
  if (params?.verification_status) query.set('verification_status', params.verification_status);
  if (params?.limit) query.set('limit', String(params.limit));

  const url = `${BASE_URL}/api/v1/safe-locations/all?${query.toString()}`;
  const response = await fetch(url, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch all safe locations: ${response.status} ${response.statusText}`);
  }

  return response.json();
}

export async function fetchSafeLocationDetails(
  locationId: string,
): Promise<SafeLocationDetailResponse> {
  const url = `${BASE_URL}/api/v1/safe-locations/${locationId}`;
  const response = await fetch(url, {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });

  if (!response.ok) {
    throw new Error(`Failed to fetch location details: ${response.status} ${response.statusText}`);
  }

  return response.json();
}

export * from './recommendationApi';
export * from './routeApi';

