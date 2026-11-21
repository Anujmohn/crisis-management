/**
 * LocationScreen (Web Version)
 *
 * Implements:
 * 1. Browser Geolocation API permission and fix retrieval via navigator.geolocation.
 * 2. POST to /api/v1/location/validate for PostGIS boundary and coordinate validation.
 * 3. Accuracy, altitude, timestamp, and coordinate rendering.
 * 4. Error states: permission denied, insecure context (HTTP), timeout, server errors.
 * 5. Explicit user-controlled location refresh.
 */

import React, { useState } from 'react';
import {
  getCurrentBrowserLocation,
  BrowserGeolocationError,
  checkGeolocationAvailability,
} from '../services/browserLocationService';
import {
  validateLocation,
  LocationData,
  ApiValidationError,
} from '../api/locationApi';

type ScreenState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'success'; location: LocationData }
  | { status: 'permission_denied' }
  | { status: 'insecure_context' }
  | { status: 'gps_error'; message: string }
  | { status: 'network_error'; message: string }
  | { status: 'server_error'; message: string }
  | { status: 'validation_error'; errors: ApiValidationError[] };

function formatCoord(value: number, decimals = 6): string {
  return value.toFixed(decimals);
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function LocationScreen(): React.JSX.Element {
  const [state, setState] = useState<ScreenState>({ status: 'idle' });

  async function handleGetLocation(): Promise<void> {
    setState({ status: 'loading' });

    const check = checkGeolocationAvailability();
    if (!check.available) {
      if (typeof window !== 'undefined' && window.isSecureContext === false) {
        setState({ status: 'insecure_context' });
      } else {
        setState({ status: 'gps_error', message: check.reason ?? 'Geolocation not available.' });
      }
      return;
    }

    try {
      const fix = await getCurrentBrowserLocation();

      // Validate fix with FastAPI backend
      const res = await validateLocation({
        latitude: fix.latitude,
        longitude: fix.longitude,
        accuracy_m: fix.accuracy_m,
        altitude_m: fix.altitude_m,
        captured_at: fix.captured_at,
      });

      if (res.ok) {
        setState({ status: 'success', location: res.data.location });
      } else if (res.kind === 'validation') {
        setState({ status: 'validation_error', errors: res.errors });
      } else if (res.kind === 'network') {
        setState({ status: 'network_error', message: res.message });
      } else {
        setState({ status: 'server_error', message: res.message });
      }
    } catch (err: unknown) {
      if (err && typeof err === 'object' && 'type' in err) {
        const geoErr = err as BrowserGeolocationError;
        if (geoErr.type === 'PERMISSION_DENIED') {
          setState({ status: 'permission_denied' });
          return;
        }
        if (geoErr.type === 'INSECURE_CONTEXT') {
          setState({ status: 'insecure_context' });
          return;
        }
        setState({ status: 'gps_error', message: geoErr.message });
        return;
      }

      const msg = err instanceof Error ? err.message : String(err);
      setState({ status: 'server_error', message: msg });
    }
  }

  const isLoading = state.status === 'loading';

  return (
    <div className="location-page-container">
      <div className="location-card">
        <h1 className="location-title">Device Location</h1>
        <p className="location-subtitle">Disaster Safety Verification Platform</p>

        {/* Status representations */}
        {state.status === 'idle' && (
          <p style={{ color: 'var(--text-secondary)', marginBottom: '24px', fontSize: '0.95rem' }}>
            Press the button below to acquire your browser's current GPS coordinates and validate them against the PostGIS database.
          </p>
        )}

        {state.status === 'loading' && (
          <div style={{ padding: '24px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '12px' }}>
            <div style={{
              width: '32px',
              height: '32px',
              border: '3px solid #E2E8F0',
              borderTopColor: 'var(--accent)',
              borderRadius: '50%',
              animation: 'spin 1s linear infinite'
            }}></div>
            <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', fontWeight: 500 }}>
              Acquiring GPS fix & validating with backend...
            </p>
          </div>
        )}

        {state.status === 'success' && (
          <div className="data-table-card" style={{ borderColor: '#BBF7D0', background: '#F0FDF4' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '12px' }}>
              <span style={{ fontWeight: 700, color: '#166534', fontSize: '0.95rem' }}>
                ✓ GPS Coordinates Validated
              </span>
              <span className="badge badge-verified">PostGIS GeoJSON</span>
            </div>

            <div className="data-row">
              <span className="data-label">Latitude</span>
              <span className="data-value">{formatCoord(state.location.latitude)}°</span>
            </div>
            <div className="data-row">
              <span className="data-label">Longitude</span>
              <span className="data-value">{formatCoord(state.location.longitude)}°</span>
            </div>
            <div className="data-row">
              <span className="data-label">Accuracy</span>
              <span className="data-value">
                {state.location.accuracy_m != null ? `${state.location.accuracy_m.toFixed(1)} m` : 'Unavailable'}
              </span>
            </div>
            <div className="data-row">
              <span className="data-label">Altitude</span>
              <span className="data-value">
                {state.location.altitude_m != null ? `${state.location.altitude_m.toFixed(1)} m` : 'Unavailable'}
              </span>
            </div>
            <div className="data-row">
              <span className="data-label">Captured At</span>
              <span className="data-value">{formatDate(state.location.captured_at)}</span>
            </div>

            <div style={{ marginTop: '12px', fontSize: '0.8rem', color: '#166534', textAlign: 'center' }}>
              🌐 Live Browser Geolocation (HTML5 W3C Standard)
            </div>
          </div>
        )}

        {state.status === 'permission_denied' && (
          <div style={{ background: '#FEF2F2', border: '1px solid #FECACA', borderRadius: '12px', padding: '16px', marginBottom: '24px' }}>
            <div style={{ fontSize: '2rem', marginBottom: '8px' }}>🔒</div>
            <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#991B1B', marginBottom: '6px' }}>Permission Denied</h3>
            <p style={{ fontSize: '0.875rem', color: '#7F1D1D', lineHeight: 1.4 }}>
              Location access was denied. Please click the permissions icon in your browser's address bar, allow location access, and try again.
            </p>
          </div>
        )}

        {state.status === 'insecure_context' && (
          <div style={{ background: '#FFFBEB', border: '1px solid #FDE68A', borderRadius: '12px', padding: '16px', marginBottom: '24px' }}>
            <div style={{ fontSize: '2rem', marginBottom: '8px' }}>⚠️</div>
            <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#92400E', marginBottom: '6px' }}>Insecure Context Detected</h3>
            <p style={{ fontSize: '0.875rem', color: '#B45309', lineHeight: 1.4 }}>
              Modern browsers restrict Geolocation API to HTTPS origins or localhost. Please access this application via HTTPS or http://localhost.
            </p>
          </div>
        )}

        {(state.status === 'gps_error' || state.status === 'network_error' || state.status === 'server_error') && (
          <div style={{ background: '#FEF2F2', border: '1px solid #FECACA', borderRadius: '12px', padding: '16px', marginBottom: '24px' }}>
            <div style={{ fontSize: '2rem', marginBottom: '8px' }}>⚠️</div>
            <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#991B1B', marginBottom: '6px' }}>
              {state.status === 'network_error' ? 'Network Connection Error' : 'Location Evaluation Error'}
            </h3>
            <p style={{ fontSize: '0.875rem', color: '#7F1D1D', lineHeight: 1.4 }}>
              {state.message}
            </p>
          </div>
        )}

        {state.status === 'validation_error' && (
          <div style={{ background: '#FEF2F2', border: '1px solid #FECACA', borderRadius: '12px', padding: '16px', marginBottom: '24px' }}>
            <div style={{ fontSize: '2rem', marginBottom: '8px' }}>❌</div>
            <h3 style={{ fontSize: '1rem', fontWeight: 700, color: '#991B1B', marginBottom: '6px' }}>Coordinates Rejected</h3>
            <p style={{ fontSize: '0.875rem', color: '#7F1D1D', lineHeight: 1.4 }}>
              {state.errors.map(e => e.msg).join('; ')}
            </p>
          </div>
        )}

        <button
          className="action-btn-pill action-btn-primary"
          style={{ width: '100%', justifyContent: 'center', padding: '12px 24px', fontSize: '1rem' }}
          onClick={handleGetLocation}
          disabled={isLoading}
        >
          {isLoading ? 'Verifying...' : state.status === 'success' ? '↻ Refresh GPS Coordinates' : '📍 Get My Location'}
        </button>
      </div>

      <style>{`
        @keyframes spin {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}
