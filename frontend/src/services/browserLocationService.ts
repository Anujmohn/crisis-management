/**
 * Browser Geolocation API Service
 *
 * Implements:
 * - Browser permission verification and request handling.
 * - Current location retrieval using navigator.geolocation.
 * - Accuracy and altitude measurement extraction.
 * - Verification of secure context (HTTPS / localhost requirement).
 * - Standardized error reporting with actionable messages.
 */

export interface BrowserLocationResult {
  latitude: number;
  longitude: number;
  accuracy_m: number | null;
  altitude_m: number | null;
  captured_at: string;
}

export type GeolocationErrorType =
  | 'UNSUPPORTED'
  | 'INSECURE_CONTEXT'
  | 'PERMISSION_DENIED'
  | 'POSITION_UNAVAILABLE'
  | 'TIMEOUT'
  | 'UNKNOWN';

export interface BrowserGeolocationError {
  type: GeolocationErrorType;
  message: string;
}

/**
 * Checks if geolocation is supported and running in a secure context.
 */
export function checkGeolocationAvailability(): { available: boolean; reason?: string } {
  if (typeof window === 'undefined' || !('geolocation' in navigator)) {
    return {
      available: false,
      reason: 'Geolocation API is not supported by this browser.',
    };
  }

  // Modern browsers restrict geolocation to secure contexts (HTTPS or localhost)
  if (typeof window !== 'undefined' && window.isSecureContext === false) {
    return {
      available: false,
      reason: 'Geolocation requires a secure context (HTTPS or localhost).',
    };
  }

  return { available: true };
}

/**
 * Retrieves the current GPS/device coordinates via the browser Geolocation API.
 * Uses high accuracy, a 15-second timeout, and max 10-second cache.
 */
export function getCurrentBrowserLocation(): Promise<BrowserLocationResult> {
  return new Promise((resolve, reject) => {
    const check = checkGeolocationAvailability();
    if (!check.available) {
      const err: BrowserGeolocationError = {
        type: typeof window !== 'undefined' && window.isSecureContext === false ? 'INSECURE_CONTEXT' : 'UNSUPPORTED',
        message: check.reason ?? 'Geolocation unavailable.',
      };
      return reject(err);
    }

    navigator.geolocation.getCurrentPosition(
      (position) => {
        const result: BrowserLocationResult = {
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
          accuracy_m: position.coords.accuracy ?? null,
          altitude_m: position.coords.altitude ?? null,
          captured_at: new Date(position.timestamp).toISOString(),
        };
        resolve(result);
      },
      (geoError) => {
        let type: GeolocationErrorType = 'UNKNOWN';
        let message = geoError.message || 'An unknown geolocation error occurred.';

        switch (geoError.code) {
          case geoError.PERMISSION_DENIED:
            type = 'PERMISSION_DENIED';
            message = 'Location access was denied. Please allow location permissions in your browser address bar.';
            break;
          case geoError.POSITION_UNAVAILABLE:
            type = 'POSITION_UNAVAILABLE';
            message = 'Location information is unavailable. Ensure your device GPS or system location services are enabled.';
            break;
          case geoError.TIMEOUT:
            type = 'TIMEOUT';
            message = 'The request to obtain your location timed out. Please try again.';
            break;
        }

        const err: BrowserGeolocationError = { type, message };
        reject(err);
      },
      {
        enableHighAccuracy: true,
        timeout: 15000,
        maximumAge: 10000,
      },
    );
  });
}
