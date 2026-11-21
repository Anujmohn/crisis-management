from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from geoalchemy2 import Geography
from shapely.geometry import Point, shape
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.core import CrisisEvent, CrisisSourceReport, DataSource, HazardObservation
from app.models.enums import CrisisStatus, CrisisType, SeverityLevel
from app.schemas.cyclone_assessment import (
    CycloneAssessmentResponse,
    CycloneAssessmentSearchParameters,
    CycloneAssessmentStatus,
    CycloneCoastalHazard,
    CycloneEventSummary,
    CyclonePosition,
    CycloneRainfallFloodHazard,
    CycloneStormSurgeHazard,
    CycloneTerrainHazard,
    CycloneTrackWaypoint,
    CycloneWarningEvidence,
    CycloneWindHazard,
)
from app.schemas.disaster_search import DisasterSourceSummary, UserLocation
from app.schemas.flood_assessment import FloodAssessmentStatus, GeographicalEnvironment
from app.services.disaster_search import evaluate_freshness, safe_postgis_to_geojson
from app.services.flood_assessment import assess_flood_hazard, classify_geographical_environment

logger = logging.getLogger(__name__)

CYCLONE_SCIENTIFIC_LIMITATIONS = [
    (
        "Tropical cyclones span hundreds of kilometers across; proximity to the center or eye "
        "alone does not determine safety."
    ),
    (
        "Outer convective rainbands and gale-force winds can cause severe structural damage and tree fall "
        "hundreds of kilometers away from the center or landfall point."
    ),
    (
        "Inland locations shielded from coastal storm surges remain vulnerable to torrential precipitation, "
        "destructive squalls, inland river flooding, and flash floods."
    ),
    (
        "In hilly and mountainous terrain, cyclone-induced rainfall substantially escalates the threat "
        "of catastrophic landslides, mudslides, and debris flows."
    ),
    (
        "Forecast tracks and storm surge projections from IMD and INCOIS are probabilistic models; "
        "track changes, intensity fluctuations, and localized bathymetry can alter impacts. "
        "Absence of reported observations does not establish that the location is safe."
    ),
]

# Representative coastline coordinates of the Indian subcontinent (mainland + Andaman & Nicobar + Lakshadweep)
# Format: [longitude, latitude]
INDIAN_MAINLAND_COASTLINE: list[list[float]] = [
    # Gujarat: Kori Creek to Gulf of Khambhat
    [68.16, 23.70], [68.50, 23.25], [68.90, 22.80], [69.00, 22.30], [69.60, 22.45],
    [70.00, 22.75], [70.50, 22.90], [70.20, 22.40], [69.10, 22.25], [69.40, 21.60],
    [70.20, 21.00], [70.90, 20.70], [71.50, 20.80], [72.10, 21.20], [72.20, 21.75],
    [72.60, 22.20], [72.85, 21.60], [72.75, 21.10], [72.80, 20.40],
    # Maharashtra & Goa
    [72.75, 19.80], [72.82, 18.95], [72.95, 18.30], [73.20, 17.50], [73.30, 16.50],
    [73.55, 15.80], [73.80, 15.30], [74.00, 14.80],
    # Karnataka & Kerala
    [74.30, 14.30], [74.50, 13.80], [74.80, 13.00], [74.98, 12.85], [75.10, 12.20],
    [75.50, 11.75], [75.75, 11.25], [76.00, 10.75], [76.20, 10.00], [76.35, 9.50],
    [76.50, 9.00], [76.75, 8.50], [77.55, 8.08],  # Kanyakumari
    # Tamil Nadu & Andhra Pradesh
    [77.75, 8.35], [78.10, 8.75], [78.60, 9.20], [79.20, 9.30], [79.30, 9.80],
    [79.85, 10.30], [79.85, 10.75], [79.85, 11.50], [80.00, 12.25], [80.27, 13.08],
    [80.10, 13.80], [80.05, 14.50], [80.35, 15.40], [80.60, 15.80], [80.85, 16.10],
    [81.70, 16.45], [82.25, 16.90], [82.85, 17.30], [83.30, 17.70], [83.90, 18.25],
    [84.40, 18.60], [84.80, 19.00],
    # Odisha & West Bengal
    [85.00, 19.30], [85.70, 19.80], [86.70, 20.30], [87.00, 20.80], [87.20, 21.20],
    [87.50, 21.50], [88.00, 21.65], [88.30, 21.60], [88.80, 21.70], [89.10, 21.75],
]

ANDAMAN_NICOBAR_COASTLINE: list[list[float]] = [
    [92.80, 13.50], [92.95, 13.00], [92.75, 12.00], [92.70, 11.60],
    [92.60, 10.50], [92.80, 9.20], [93.50, 7.00], [93.80, 6.80],
]

LAKSHADWEEP_COASTLINE: list[list[float]] = [
    [72.60, 11.70], [72.75, 11.20], [72.60, 10.55], [73.00, 10.05], [73.05, 8.30],
]

COASTLINE_CHAINS = [
    INDIAN_MAINLAND_COASTLINE,
    ANDAMAN_NICOBAR_COASTLINE,
    LAKSHADWEEP_COASTLINE,
]


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates great-circle distance between two coordinates in kilometers."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return 6371.0 * c


def point_to_segment_distance_km(
    plat: float, plon: float, lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """
    Calculates the minimum distance from point P to line segment AB in kilometers
    using local equirectangular planar projection.
    """
    mid_lat = math.radians((lat1 + lat2 + plat) / 3.0)
    cos_lat = math.cos(mid_lat)

    # Convert coordinates to relative planar km
    px = (plon - lon1) * 111.32 * cos_lat
    py = (plat - lat1) * 110.57

    bx = (lon2 - lon1) * 111.32 * cos_lat
    by = (lat2 - lat1) * 110.57

    seg_len_sq = bx * bx + by * by
    if seg_len_sq < 1e-6:
        return math.sqrt(px * px + py * py)

    # Projection parameter t clamped to [0, 1]
    t = max(0.0, min(1.0, (px * bx + py * by) / seg_len_sq))
    proj_x = t * bx
    proj_y = t * by

    dx = px - proj_x
    dy = py - proj_y
    return math.sqrt(dx * dx + dy * dy)


def calculate_distance_to_indian_coastline_km(latitude: float, longitude: float) -> float:
    """
    Computes the shortest distance in kilometers from a geographic coordinate
    to the nearest coastline segment of the Indian subcontinent and island territories.
    """
    min_dist = float("inf")
    for chain in COASTLINE_CHAINS:
        for i in range(len(chain) - 1):
            p1 = chain[i]
            p2 = chain[i + 1]
            dist = point_to_segment_distance_km(latitude, longitude, p1[1], p1[0], p2[1], p2[0])
            if dist < min_dist:
                min_dist = dist

    return round(min_dist, 1)


def normalize_wind_speed_to_kmph(val: float | Decimal | None, unit: str | None = None) -> float | None:
    """
    Normalizes wind speeds reported in knots, m/s, or mph into km/h.
    Returns None when the input value is None. Missing values are never converted to 0.0.
    """
    if val is None:
        return None
    try:
        num = float(val)
    except (ValueError, TypeError):
        return None

    u = (unit or "").strip().lower()
    if "knot" in u or "kt" in u:
        return round(num * 1.852, 1)
    elif "m/s" in u or "mps" in u:
        return round(num * 3.6, 1)
    elif "km" in u:
        return round(num, 1)
    elif "mph" in u:
        return round(num * 1.60934, 1)
    return round(num, 1)


def classify_cyclone_intensity(kmph: float | None) -> str:
    """
    Classifies cyclone intensity according to the official India Meteorological Department (IMD) scale.
    """
    if kmph is None:
        return "UNKNOWN"
    if kmph < 31.0:
        return "LOW_PRESSURE_AREA"
    elif kmph < 50.0:
        return "DEPRESSION"
    elif kmph < 62.0:
        return "DEEP_DEPRESSION"
    elif kmph < 89.0:
        return "CYCLONIC_STORM"
    elif kmph < 118.0:
        return "SEVERE_CYCLONIC_STORM"
    elif kmph < 167.0:
        return "VERY_SEVERE_CYCLONIC_STORM"
    elif kmph < 222.0:
        return "EXTREMELY_SEVERE_CYCLONIC_STORM"
    else:
        return "SUPER_CYCLONIC_STORM"


def extract_cyclone_coordinates(event: CrisisEvent) -> tuple[float | None, float | None]:
    """Extracts latitude and longitude from a CrisisEvent's point or centroid geometry."""
    if event.location is not None:
        try:
            pt = shape(safe_postgis_to_geojson(event.location))
            return float(pt.y), float(pt.x)
        except Exception:
            pass

    if event.affected_area is not None:
        try:
            poly = shape(safe_postgis_to_geojson(event.affected_area))
            centroid = poly.centroid
            return float(centroid.y), float(centroid.x)
        except Exception:
            pass

    return None, None


def assess_cyclone_hazard(
    session: Session,
    latitude: float,
    longitude: float,
    search_radius_km: float = 500.0,
    lookback_days: int = 7,
) -> CycloneAssessmentResponse:
    """
    Assesses cyclone-related geographical hazards around a user's location.

    Independently evaluates:
    1. Strong winds (sustained winds, gusts, IMD intensity category, wind warnings).
    2. Storm surge (INCOIS coastal water levels, surge estimates, warnings).
    3. Coastal inundation (proximity to coastline vs inland, wave heights).
    4. Heavy rainfall & inland flooding (leveraging existing flood assessment engine).
    5. Associated landslide risks in steep terrain under heavy precipitation.
    6. Official cyclone and evacuation warnings (IMD, NDMA SACHET).

    Missing measurements are strictly preserved as None / null (never 0.0).
    When no events are found, a mandatory non-safety disclaimer is returned.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=lookback_days)
    search_radius_m = float(search_radius_km) * 1000.0

    contributing_factors: list[str] = []
    missing_information: list[str] = []
    assessment_statuses: list[CycloneAssessmentStatus] = []

    # 1. Geographic context: Distance to Indian coastline & terrain environment
    dist_coast_km = calculate_distance_to_indian_coastline_km(latitude, longitude)
    is_coastal = dist_coast_km <= 50.0

    geo_env, env_notes = classify_geographical_environment(latitude, longitude)

    coastal_notes: list[str] = []
    if is_coastal:
        coastal_notes.append(
            f"User is located in the coastal zone ({dist_coast_km:.1f} km from the nearest coastline). "
            "Exposed to potential storm surges, maritime high waves, and coastal flooding."
        )
    else:
        coastal_notes.append(
            f"User is located inland ({dist_coast_km:.1f} km from the nearest coastline). "
            "Direct marine storm surge inundation does not physically threaten this location, "
            "though severe wind gusts, heavy rainfall bands, and inland river flooding can occur."
        )

    # 2. Query Cyclone CrisisEvents and Source Reports
    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    user_point = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)

    event_distance = func.ST_Distance(CrisisEvent.location.cast(Geography), user_geog).label("dist_m")
    event_intersects = func.ST_Intersects(CrisisEvent.affected_area, user_point).label("intersects")

    event_stmt = (
        select(CrisisEvent, event_distance, event_intersects)
        .options(
            selectinload(CrisisEvent.source_reports).selectinload(CrisisSourceReport.data_source),
            selectinload(CrisisEvent.hazard_observations),
        )
        .where(
            CrisisEvent.crisis_type == CrisisType.CYCLONE,
            or_(
                CrisisEvent.last_seen_at >= cutoff,
                CrisisEvent.started_at >= cutoff,
            ),
            or_(
                func.ST_DWithin(CrisisEvent.location.cast(Geography), user_geog, search_radius_m),
                func.ST_DWithin(CrisisEvent.affected_area.cast(Geography), user_geog, 0.0),
            ),
        )
        .order_by(event_distance.asc().nullslast(), CrisisEvent.started_at.desc())
    )

    event_records = session.execute(event_stmt).all()

    # 3. Query Standalone HazardObservations (e.g. INCOIS ocean surge, wave height)
    obs_distance = func.ST_Distance(HazardObservation.geometry.cast(Geography), user_geog).label("dist_m")
    obs_stmt = (
        select(HazardObservation, obs_distance)
        .options(selectinload(HazardObservation.data_source))
        .where(
            HazardObservation.observation_type.in_([
                "CYCLONE_MAX_WIND_SPEED",
                "CYCLONE_WIND_SPEED",
                "CYCLONE_GUST_SPEED",
                "CYCLONE_POSITION",
                "STORM_SURGE_HEIGHT",
                "CYCLONE_WAVE_HEIGHT",
                "WAVE_HEIGHT",
                "TIDE_LEVEL",
            ]),
            HazardObservation.observed_at >= cutoff,
            func.ST_DWithin(HazardObservation.geometry.cast(Geography), user_geog, search_radius_m),
        )
        .order_by(obs_distance.asc(), HazardObservation.observed_at.desc())
    )
    obs_records = session.execute(obs_stmt).all()

    # 4. Integrate Flood & Heavy Rainfall Hazards via assess_flood_hazard
    flood_assessment = assess_flood_hazard(
        session=session,
        latitude=latitude,
        longitude=longitude,
        search_radius_km=min(search_radius_km, 100.0),
        lookback_days=lookback_days,
    )

    # Parse flood and rainfall indicators
    heavy_rain_warning = False
    inland_flood_hazard = False
    max_rain_rate: float | None = None
    rain_cat: str | None = None
    gauges_danger = 0
    gauges_warning = 0
    rain_notes: list[str] = []

    for rain_obs in flood_assessment.rainfall_information:
        if rain_obs.rainfall_intensity_mm_hr is not None:
            if max_rain_rate is None or rain_obs.rainfall_intensity_mm_hr > max_rain_rate:
                max_rain_rate = rain_obs.rainfall_intensity_mm_hr
                rain_cat = rain_obs.rainfall_category

        if rain_obs.rainfall_category in ("HEAVY", "VERY_HEAVY", "EXTREMELY_HEAVY"):
            heavy_rain_warning = True

    for river_obs in flood_assessment.river_observations:
        if river_obs.river_level_status in ("ABOVE_DANGER", "ABOVE_HFL"):
            gauges_danger += 1
            inland_flood_hazard = True
        elif river_obs.river_level_status == "ABOVE_WARNING":
            gauges_warning += 1
            inland_flood_hazard = True

    if flood_assessment.assessment_status in (
        FloodAssessmentStatus.OFFICIAL_FLOOD_WARNING,
        FloodAssessmentStatus.OBSERVED_FLOODING,
        FloodAssessmentStatus.ELEVATED_FLOOD_SUSCEPTIBILITY,
        FloodAssessmentStatus.HIGH_FLOW_PATH_EXPOSURE,
        FloodAssessmentStatus.VALLEY_FLOOD_CONCERN,
    ) or gauges_danger > 0:
        inland_flood_hazard = True

    if heavy_rain_warning:
        rain_notes.append("Heavy to extremely heavy precipitation reported or warned in the vicinity.")
    if inland_flood_hazard:
        rain_notes.append("Active river inundation or rivers exceeding warning/danger levels detected in the basin.")

    # 5. Terrain & Landslide Hazard
    landslide_concern = False
    debris_flow_concern = False
    terrain_notes = list(env_notes)

    if geo_env == GeographicalEnvironment.HILLY_OR_MOUNTAINOUS:
        if heavy_rain_warning or inland_flood_hazard:
            landslide_concern = True
            debris_flow_concern = True
            terrain_notes.append(
                "Steep terrain coupled with cyclone-induced heavy precipitation creates critical vulnerability "
                "to slope failure, mudslides, and debris flows."
            )
        else:
            terrain_notes.append(
                "Location is in hilly/mountainous terrain. Intense downpours may rapidly trigger slope failures."
            )

    # 6. Process Cyclone Events, Waypoints, Warnings, Winds, and Surges
    relevant_events: list[CycloneEventSummary] = []
    official_warnings: list[CycloneWarningEvidence] = []
    track_waypoints: list[CycloneTrackWaypoint] = []
    sources_dict: dict[str, DisasterSourceSummary] = {}

    nearest_event_dist_km: float | None = None
    latest_observed_pos: CyclonePosition | None = None

    max_sustained_wind: float | None = None
    max_gust_speed: float | None = None
    orig_wind_val: float | None = None
    orig_wind_unit: str | None = None
    wind_warning_desc: str | None = None

    max_surge_height_m: float | None = None
    max_wave_height_m: float | None = None
    max_tide_level_m: float | None = None
    surge_agency: str | None = None
    surge_notes: list[str] = []

    has_active_events = False
    is_all_stale = True

    for row in event_records:
        event: CrisisEvent = row[0]
        dist_m: float | None = row[1]
        intersects: bool = bool(row[2])

        dist_km = (
            round(dist_m / 1000.0, 1)
            if dist_m is not None
            else round(
                haversine_distance_km(
                    latitude,
                    longitude,
                    extract_cyclone_coordinates(event)[0] or latitude,
                    extract_cyclone_coordinates(event)[1] or longitude,
                ),
                1,
            )
        )

        if nearest_event_dist_km is None or dist_km < nearest_event_dist_km:
            nearest_event_dist_km = dist_km

        freshness_label = evaluate_freshness(event, now)
        if event.status == CrisisStatus.ACTIVE:
            has_active_events = True
            is_all_stale = False

        c_lat, c_lon = extract_cyclone_coordinates(event)

        # Look up source report details
        source_name = "Official Disaster Authority"
        report_payload: dict[str, Any] = {}
        forecast_pts_count = 0

        for rpt in event.source_reports:
            if rpt.data_source:
                s_code = rpt.data_source.code
                if s_code not in sources_dict:
                    sources_dict[s_code] = DisasterSourceSummary(
                        source_name=rpt.data_source.name,
                        source_type=getattr(rpt.data_source, "source_type", "API"),
                        source_url=getattr(rpt, "source_url", None),
                        external_event_id=getattr(rpt, "external_event_id", None),
                        issued_at=rpt.issued_at,
                        fetched_at=getattr(rpt, "fetched_at", None) or now,
                        valid_until=getattr(rpt, "valid_until", None),
                    )
                source_name = rpt.data_source.name

            if rpt.raw_payload and isinstance(rpt.raw_payload, dict):
                report_payload = rpt.raw_payload
                f_track = report_payload.get("forecast_track") or []
                forecast_pts_count = len(f_track)

                # Extract waypoints
                for wpt in f_track:
                    wpt_lat = wpt.get("latitude")
                    wpt_lon = wpt.get("longitude")
                    if wpt_lat is not None and wpt_lon is not None:
                        wpt_dist = round(haversine_distance_km(latitude, longitude, float(wpt_lat), float(wpt_lon)), 1)
                        track_waypoints.append(
                            CycloneTrackWaypoint(
                                waypoint_index=wpt.get("waypoint_index", len(track_waypoints)),
                                latitude=float(wpt_lat),
                                longitude=float(wpt_lon),
                                position_type=str(wpt.get("position_type", "FORECAST")).upper(),
                                forecast_timestamp=rpt.issued_at,
                                distance_km=wpt_dist,
                                estimated_max_wind_kmph=normalize_wind_speed_to_kmph(wpt.get("max_wind")),
                            )
                        )

                # Extract winds from report payload if present
                extracted_obs = report_payload.get("extracted_observations") or {}
                s_wind = extracted_obs.get("sustained_wind_speed")
                g_wind = extracted_obs.get("gust_speed")
                w_wave = extracted_obs.get("wave_height")

                if s_wind is not None:
                    norm_s = normalize_wind_speed_to_kmph(float(s_wind))
                    if norm_s is not None and (max_sustained_wind is None or norm_s > max_sustained_wind):
                        max_sustained_wind = norm_s
                        orig_wind_val = float(s_wind)
                        orig_wind_unit = "kmph"
                if g_wind is not None:
                    norm_g = normalize_wind_speed_to_kmph(float(g_wind))
                    if norm_g is not None and (max_gust_speed is None or norm_g > max_gust_speed):
                        max_gust_speed = norm_g
                if w_wave is not None:
                    w_num = float(w_wave)
                    if max_wave_height_m is None or w_num > max_wave_height_m:
                        max_wave_height_m = w_num

        # Observed position from event location
        if c_lat is not None and c_lon is not None and latest_observed_pos is None:
            latest_observed_pos = CyclonePosition(
                latitude=c_lat,
                longitude=c_lon,
                position_type="OBSERVED",
                observed_at=event.started_at,
                distance_km=dist_km,
                source_name=source_name,
            )

        # Official Warning Evidence
        if event.status == CrisisStatus.ACTIVE or intersects:
            official_warnings.append(
                CycloneWarningEvidence(
                    event_id=event.id,
                    title=event.title,
                    description=event.description,
                    severity=event.severity,
                    status=event.status,
                    issued_at=event.started_at,
                    valid_until=event.expires_at,
                    distance_km=dist_km,
                    intersects_user_location=intersects,
                    source_name=source_name,
                    headline=report_payload.get("headline"),
                    warning_type=report_payload.get("event"),
                )
            )

        # Relevant Cyclone Event Summary
        relevant_events.append(
            CycloneEventSummary(
                event_id=event.id,
                title=event.title,
                cyclone_name=report_payload.get("cyclone_name"),
                cyclone_id=report_payload.get("cyclone_id"),
                cyclone_type=report_payload.get("cyclone_type"),
                severity=event.severity,
                status=event.status,
                center_latitude=c_lat,
                center_longitude=c_lon,
                distance_km=dist_km,
                started_at=event.started_at,
                forecast_waypoints_count=forecast_pts_count,
                freshness=freshness_label,
            )
        )

        # Extract event's HazardObservations
        for obs in event.hazard_observations:
            val_num = float(obs.value_numeric) if obs.value_numeric is not None else None
            if obs.observation_type in ("CYCLONE_MAX_WIND_SPEED", "CYCLONE_WIND_SPEED"):
                norm_w = normalize_wind_speed_to_kmph(val_num, obs.unit)
                if norm_w is not None and (max_sustained_wind is None or norm_w > max_sustained_wind):
                    max_sustained_wind = norm_w
                    orig_wind_val = val_num
                    orig_wind_unit = obs.unit
            elif obs.observation_type == "CYCLONE_GUST_SPEED":
                norm_g = normalize_wind_speed_to_kmph(val_num, obs.unit)
                if norm_g is not None and (max_gust_speed is None or norm_g > max_gust_speed):
                    max_gust_speed = norm_g
            elif obs.observation_type in ("STORM_SURGE_HEIGHT", "SURGE_HEIGHT"):
                if val_num is not None and (max_surge_height_m is None or val_num > max_surge_height_m):
                    max_surge_height_m = val_num
            elif obs.observation_type in ("CYCLONE_WAVE_HEIGHT", "WAVE_HEIGHT"):
                if val_num is not None and (max_wave_height_m is None or val_num > max_wave_height_m):
                    max_wave_height_m = val_num

    # Process standalone coastal observations (e.g. INCOIS ERDDAP stations)
    for obs_row in obs_records:
        obs: HazardObservation = obs_row[0]
        val_num = float(obs.value_numeric) if obs.value_numeric is not None else None

        if obs.data_source:
            s_code = obs.data_source.code
            if s_code not in sources_dict:
                sources_dict[s_code] = DisasterSourceSummary(
                    source_name=obs.data_source.name,
                    source_type=getattr(obs.data_source, "source_type", "SENSOR_STATION"),
                    source_url=getattr(obs.data_source, "source_url", None),
                    external_event_id=None,
                    issued_at=obs.observed_at,
                    fetched_at=getattr(obs, "ingested_at", None) or now,
                    valid_until=None,
                )
            if surge_agency is None:
                surge_agency = obs.data_source.name

        if obs.observation_type in ("STORM_SURGE_HEIGHT", "SURGE_HEIGHT"):
            if val_num is not None and (max_surge_height_m is None or val_num > max_surge_height_m):
                max_surge_height_m = val_num
        elif obs.observation_type in ("CYCLONE_WAVE_HEIGHT", "WAVE_HEIGHT"):
            if val_num is not None and (max_wave_height_m is None or val_num > max_wave_height_m):
                max_wave_height_m = val_num
        elif obs.observation_type == "TIDE_LEVEL":
            if val_num is not None and (max_tide_level_m is None or val_num > max_tide_level_m):
                max_tide_level_m = val_num
        elif obs.observation_type in ("CYCLONE_MAX_WIND_SPEED", "CYCLONE_WIND_SPEED"):
            norm_w = normalize_wind_speed_to_kmph(val_num, obs.unit)
            if norm_w is not None and (max_sustained_wind is None or norm_w > max_sustained_wind):
                max_sustained_wind = norm_w
                orig_wind_val = val_num
                orig_wind_unit = obs.unit
        elif obs.observation_type == "CYCLONE_GUST_SPEED":
            norm_g = normalize_wind_speed_to_kmph(val_num, obs.unit)
            if norm_g is not None and (max_gust_speed is None or norm_g > max_gust_speed):
                max_gust_speed = norm_g

    # 7. Evaluate Hazard Sub-Models
    # Wind Hazard
    intensity_cat = classify_cyclone_intensity(max_sustained_wind)
    active_wind_hazard = False
    if max_sustained_wind is not None and max_sustained_wind >= 62.0:
        active_wind_hazard = True
        wind_warning_desc = f"{intensity_cat.replace('_', ' ').title()} wind speeds (sustained {max_sustained_wind:.1f} km/h)."
    elif len(official_warnings) > 0:
        for w in official_warnings:
            if w.intersects_user_location or w.distance_km <= 200.0:
                active_wind_hazard = True
                wind_warning_desc = "Gale/cyclone wind warning issued for this area."
                break

    wind_hazard = CycloneWindHazard(
        active_wind_hazard=active_wind_hazard,
        max_sustained_wind_kmph=max_sustained_wind,
        gust_speed_kmph=max_gust_speed,
        original_wind_value=orig_wind_val,
        original_unit=orig_wind_unit,
        cyclone_intensity_category=intensity_cat,
        is_forecast=False,
        warning_description=wind_warning_desc,
    )

    # Storm Surge Hazard
    surge_warning_active = False
    if is_coastal:
        if max_surge_height_m is not None and max_surge_height_m >= 1.0:
            surge_warning_active = True
            surge_notes.append(f"Storm surge of {max_surge_height_m:.1f}m observed/warned along the coast.")
        elif max_wave_height_m is not None and max_wave_height_m >= 2.5:
            surge_warning_active = True
            surge_notes.append(f"Rough seas with high wave heights ({max_wave_height_m:.1f}m) reported by INCOIS.")
        elif len(official_warnings) > 0:
            for w in official_warnings:
                if w.intersects_user_location or w.distance_km <= 100.0:
                    surge_warning_active = True
                    surge_notes.append("Official coastal alert issued; potential storm surge threat.")
                    break
    else:
        surge_notes.append("Inland location; direct marine storm surge not applicable.")

    storm_surge = CycloneStormSurgeHazard(
        surge_warning_active=surge_warning_active,
        estimated_surge_height_m=max_surge_height_m,
        peak_wave_height_m=max_wave_height_m,
        tide_level_m=max_tide_level_m,
        is_forecast=False,
        source_agency=surge_agency or "INCOIS / IMD",
        notes=surge_notes,
    )

    # Coastal Inundation Hazard
    coastal_inundation_concern = is_coastal and (surge_warning_active or (max_wave_height_m is not None and max_wave_height_m >= 2.0))
    if coastal_inundation_concern:
        coastal_notes.append("Risk of sea water inundation along low-lying coastal tracts.")

    coastal_hazard = CycloneCoastalHazard(
        is_coastal_zone=is_coastal,
        distance_to_coastline_km=dist_coast_km,
        coastal_inundation_concern=coastal_inundation_concern,
        notes=coastal_notes,
    )

    # Rainfall and Inland Flooding Hazard
    rainfall_flood_hazard = CycloneRainfallFloodHazard(
        heavy_rainfall_warning=heavy_rain_warning,
        inland_flood_concern=inland_flood_hazard,
        max_rainfall_intensity_mm_hr=max_rain_rate,
        rainfall_category=rain_cat,
        river_gauges_above_danger=gauges_danger,
        river_gauges_above_warning=gauges_warning,
        notes=rain_notes,
    )

    # Terrain and Landslide Hazard
    terrain_hazard = CycloneTerrainHazard(
        geographical_environment=geo_env,
        landslide_concern=landslide_concern,
        debris_flow_concern=debris_flow_concern,
        notes=terrain_notes,
    )

    # 8. Check Missing Information
    if max_sustained_wind is None:
        missing_information.append("Direct anemometer or sustained wind speed observation is unavailable.")
    if max_surge_height_m is None and is_coastal:
        missing_information.append("Direct sea-level gauge or storm surge height measurement is unavailable.")
    if max_wave_height_m is None and is_coastal:
        missing_information.append("Ocean wave rider buoy wave height measurement is unavailable.")
    if len(track_waypoints) == 0:
        missing_information.append("Numerical forecast track waypoints are not published for active systems.")

    # 9. Assign Multi-Statuses and Determine Primary Status
    if len(official_warnings) > 0:
        assessment_statuses.append(CycloneAssessmentStatus.OFFICIAL_CYCLONE_WARNING)
        contributing_factors.append(f"{len(official_warnings)} official cyclone/storm warning(s) active in the region.")

    if nearest_event_dist_km is not None and nearest_event_dist_km <= search_radius_km:
        assessment_statuses.append(CycloneAssessmentStatus.CYCLONE_POSITION_RELEVANT)
        contributing_factors.append(f"Cyclone system center located {nearest_event_dist_km:.1f} km from user location.")

    if active_wind_hazard:
        assessment_statuses.append(CycloneAssessmentStatus.WIND_HAZARD_CONCERN)
        contributing_factors.append(
            f"Damaging wind hazard active (intensity: {intensity_cat.replace('_', ' ').title()})."
        )

    if surge_warning_active:
        assessment_statuses.append(CycloneAssessmentStatus.STORM_SURGE_WARNING)
        contributing_factors.append("Marine storm surge or high-wave alert active along the coast.")

    if coastal_inundation_concern:
        assessment_statuses.append(CycloneAssessmentStatus.COASTAL_INUNDATION_CONCERN)
        contributing_factors.append("Low-lying coastal zone subject to tidal/surge inundation hazard.")

    if heavy_rain_warning:
        assessment_statuses.append(CycloneAssessmentStatus.HEAVY_RAINFALL_WARNING)
        contributing_factors.append("Heavy to extremely heavy precipitation reported or warned.")

    if inland_flood_hazard:
        assessment_statuses.append(CycloneAssessmentStatus.INLAND_FLOOD_CONCERN)
        contributing_factors.append("Inland river basin flooding or critical water levels detected.")

    if landslide_concern:
        assessment_statuses.append(CycloneAssessmentStatus.LANDSLIDE_RELATED_CONCERN)
        contributing_factors.append("Steep mountain terrain exposed to heavy rain-induced slope failure hazard.")

    # Freshness / Data Availability Evaluation
    if len(event_records) == 0 and len(obs_records) == 0:
        if not heavy_rain_warning and not inland_flood_hazard:
            assessment_statuses.append(CycloneAssessmentStatus.NO_RELEVANT_HAZARD_EVIDENCE)
    elif is_all_stale and not has_active_events:
        assessment_statuses.append(CycloneAssessmentStatus.DATA_STALE)
        contributing_factors.append("All retrieved cyclone records are past validity or older than the active window.")

    # Precedence order for primary_status:
    STATUS_PRECEDENCE = [
        CycloneAssessmentStatus.OFFICIAL_CYCLONE_WARNING,
        CycloneAssessmentStatus.STORM_SURGE_WARNING,
        CycloneAssessmentStatus.WIND_HAZARD_CONCERN,
        CycloneAssessmentStatus.COASTAL_INUNDATION_CONCERN,
        CycloneAssessmentStatus.HEAVY_RAINFALL_WARNING,
        CycloneAssessmentStatus.INLAND_FLOOD_CONCERN,
        CycloneAssessmentStatus.LANDSLIDE_RELATED_CONCERN,
        CycloneAssessmentStatus.DATA_STALE,
        CycloneAssessmentStatus.CYCLONE_POSITION_RELEVANT,
        CycloneAssessmentStatus.INSUFFICIENT_DATA,
        CycloneAssessmentStatus.NO_RELEVANT_HAZARD_EVIDENCE,
    ]

    primary_status = CycloneAssessmentStatus.NO_RELEVANT_HAZARD_EVIDENCE
    for st in STATUS_PRECEDENCE:
        if st in assessment_statuses:
            primary_status = st
            break

    # 10. Summary Message
    if primary_status == CycloneAssessmentStatus.NO_RELEVANT_HAZARD_EVIDENCE:
        summary_msg = (
            "No relevant cyclone event, official warning, or oceanic disturbance was found in the "
            f"available dataset within {search_radius_km:.0f} km. "
            "This does not establish that the location is safe."
        )
    elif primary_status == CycloneAssessmentStatus.DATA_STALE:
        summary_msg = (
            "Available cyclone reports within the search radius are historical or expired. "
            "No current active cyclone advisories are present in the ingested database."
        )
    else:
        parts: list[str] = []
        if len(official_warnings) > 0:
            parts.append(f"{len(official_warnings)} official cyclone advisory/warning(s) active")
        if nearest_event_dist_km is not None:
            parts.append(f"cyclone system {nearest_event_dist_km:.1f} km away")
        if active_wind_hazard:
            parts.append(f"damaging winds ({intensity_cat.replace('_', ' ').title()})")
        if coastal_inundation_concern:
            parts.append("coastal storm surge inundation threat")
        if heavy_rain_warning or inland_flood_hazard:
            parts.append("heavy rainfall and inland flooding hazard")
        if landslide_concern:
            parts.append("mountain landslide concern")

        summary_msg = f"Active cyclone hazard detected: {'; '.join(parts)}."

    # Determine data freshness
    data_freshness_label = "NO_DATA"
    if relevant_events:
        data_freshness_label = relevant_events[0].freshness

    return CycloneAssessmentResponse(
        success=True,
        user_location=UserLocation(latitude=latitude, longitude=longitude),
        assessment_timestamp=now,
        assessment_statuses=assessment_statuses,
        primary_status=primary_status,
        summary_message=summary_msg,
        search_parameters=CycloneAssessmentSearchParameters(
            lookback_days=lookback_days,
            search_radius_km=search_radius_km,
        ),
        distance_to_cyclone_center_km=nearest_event_dist_km,
        latest_observed_position=latest_observed_pos,
        forecast_track_waypoints=track_waypoints,
        wind_hazard=wind_hazard,
        storm_surge=storm_surge,
        coastal_inundation=coastal_hazard,
        rainfall_and_inland_flooding=rainfall_flood_hazard,
        terrain_and_landslide=terrain_hazard,
        official_warnings=official_warnings,
        relevant_cyclone_events=relevant_events,
        source_attribution=list(sources_dict.values()),
        contributing_factors=contributing_factors,
        missing_information=missing_information,
        data_freshness=data_freshness_label,
        scientific_limitations=CYCLONE_SCIENTIFIC_LIMITATIONS,
    )
