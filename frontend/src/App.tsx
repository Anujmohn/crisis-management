import React, { useState, useEffect } from 'react';
import LocationScreen from './screens/LocationScreen';
import MapScreen from './screens/MapScreen';

type Tab = 'map' | 'location';

export default function App(): React.JSX.Element {
  const [activeTab, setActiveTab] = useState<Tab>('map');
  const [backendStatus, setBackendStatus] = useState<'checking' | 'connected' | 'disconnected'>('checking');

  useEffect(() => {
    // Check backend health
    fetch('/health')
      .then((res) => {
        if (res.ok) setBackendStatus('connected');
        else setBackendStatus('disconnected');
      })
      .catch(() => {
        // Fallback check direct localhost:8000
        fetch('http://localhost:8000/health')
          .then((res) => {
            if (res.ok) setBackendStatus('connected');
            else setBackendStatus('disconnected');
          })
          .catch(() => setBackendStatus('disconnected'));
      });
  }, []);

  return (
    <div id="root">
      {/* Main Web Application Header */}
      <header className="app-header">
        <div className="brand-title">
          <span>🛡️</span>
          <span>Disaster Safety Platform</span>
          <span
            className="badge"
            style={{
              fontSize: '0.7rem',
              backgroundColor: backendStatus === 'connected' ? '#DCFCE7' : backendStatus === 'checking' ? '#FEF9C3' : '#FEE2E2',
              color: backendStatus === 'connected' ? '#166534' : backendStatus === 'checking' ? '#854D0E' : '#991B1B',
              marginLeft: '8px',
            }}
          >
            {backendStatus === 'connected' ? '● Backend Live' : backendStatus === 'checking' ? '○ Connecting...' : '✕ Offline'}
          </span>
        </div>

        {/* Tab Navigation */}
        <nav className="nav-tabs" role="tablist">
          <button
            role="tab"
            aria-selected={activeTab === 'map'}
            className={`nav-tab-btn ${activeTab === 'map' ? 'active' : ''}`}
            onClick={() => setActiveTab('map')}
          >
            🗺️ Disaster Map
          </button>
          <button
            role="tab"
            aria-selected={activeTab === 'location'}
            className={`nav-tab-btn ${activeTab === 'location' ? 'active' : ''}`}
            onClick={() => setActiveTab('location')}
          >
            📍 GPS Location
          </button>
        </nav>
      </header>

      {/* Screen Container */}
      <main className="screen-container">
        {activeTab === 'map' ? <MapScreen /> : <LocationScreen />}
      </main>
    </div>
  );
}
