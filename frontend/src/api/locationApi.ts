/**
 * Centralized API client for the Disaster Safety backend.
 *
 * Base URL is read from the EXPO_PUBLIC_API_BASE_URL environment variable so
 * it can be set per-environment without touching source code.
 *
 * Android emulator default  : EXPO_PUBLIC_API_BASE_URL=http://10.0.2.2:8000
 * Physical device (LAN)     : EXPO_PUBLIC_API_BASE_URL=http://192.168.x.x:8000
 * iOS simulator             : EXPO_PUBLIC_API_BASE_URL=http://localhost:8000
 */

const BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ??
  'http://localhost:8000';

// ---------------------------------------------------------------------------
// Request / response shapes – must match backend schemas exactly
// ---------------------------------------------------------------------------

export interface LocationRequest {
  latitude: number;
  longitude: number;
  accuracy_m: number | null;
  altitude_m: number | null;
  /** ISO 8601 string with timezone offset, e.g. "2024-01-01T12:00:00+00:00" */
  captured_at: string;
}

export interface GeoJSONPoint {
  type: 'Point';
  coordinates: [number, number]; // [longitude, latitude]
}

export interface LocationData {
  latitude: number;
  longitude: number;
  accuracy_m: number | null;
  altitude_m: number | null;
  captured_at: string;
  geometry: GeoJSONPoint;
}

export interface LocationValidationResponse {
  success: boolean;
  location: LocationData;
}

export interface ApiValidationError {
  type: string;
  loc: (string | number)[];
  msg: string;
  url?: string;
}

// ---------------------------------------------------------------------------
// API call
// ---------------------------------------------------------------------------

export type ValidateLocationResult =
  | { ok: true; data: LocationValidationResponse }
  | { ok: false; kind: 'validation'; errors: ApiValidationError[] }
  | { ok: false; kind: 'network'; message: string }
  | { ok: false; kind: 'server'; status: number; message: string };

export async function validateLocation(
  request: LocationRequest,
): Promise<ValidateLocationResult> {
  let response: Response;

  try {
    response = await fetch(`${BASE_URL}/api/v1/location/validate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
  } catch (error) {
    const message =
      error instanceof Error ? error.message : 'Unknown network error';
    return { ok: false, kind: 'network', message };
  }

  if (response.status === 422) {
    try {
      const body = await response.json();
      return {
        ok: false,
        kind: 'validation',
        errors: body.detail ?? [],
      };
    } catch {
      return {
        ok: false,
        kind: 'server',
        status: 422,
        message: 'Unprocessable response from server.',
      };
    }
  }

  if (!response.ok) {
    return {
      ok: false,
      kind: 'server',
      status: response.status,
      message: `Server error ${response.status}: ${response.statusText}`,
    };
  }

  try {
    const data: LocationValidationResponse = await response.json();
    return { ok: true, data };
  } catch {
    return {
      ok: false,
      kind: 'server',
      status: response.status,
      message: 'Could not parse server response.',
    };
  }
}
