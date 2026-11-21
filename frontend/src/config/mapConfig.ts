/**
 * Web Map configuration constants.
 *
 * Uses OpenFreeMap's "liberty" style by default:
 *   - Zero API key required
 *   - Attribution: © OpenFreeMap © OpenMapTiles Data © OpenStreetMap contributors
 */

export const MAP_STYLE_URL =
  (import.meta.env.VITE_MAP_STYLE_URL as string | undefined) ??
  'https://tiles.openfreemap.org/styles/liberty';

export const MAP_ATTRIBUTION =
  (import.meta.env.VITE_MAP_ATTRIBUTION as string | undefined) ??
  '© OpenFreeMap © OpenMapTiles Data © OpenStreetMap contributors';

/** Default camera zoom level when centering on user location */
export const USER_LOCATION_ZOOM = 14;

/** Initial fallback camera position (India overview: lng 78.9629, lat 20.5937) */
export const DEFAULT_CENTER: [number, number] = [78.9629, 20.5937];
export const DEFAULT_ZOOM = 4.5;
