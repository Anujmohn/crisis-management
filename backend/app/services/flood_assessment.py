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
from app.schemas.disaster_search import DisasterSourceSummary, UserLocation
from app.schemas.flood_assessment import (
    FloodAssessmentResponse,
    FloodAssessmentSearchParameters,
    FloodAssessmentStatus,
    FloodWarningEvidence,
    GeographicalEnvironment,
    HillyTerrainAnalysis,
    HistoricalFloodEvidence,
    ObservedFloodEvidence,
    RainfallEvidence,
    RiverLevelStatus,
    RiverObservationEvidence,
    TerrainEvidence,
    TerrainHazardStatus,
)
from app.services.disaster_search import evaluate_freshness, safe_postgis_to_geojson
from app.services.terrain_analysis import ElevationGrid, analyze_hilly_terrain_hazard

logger = logging.getLogger(__name__)

FLOOD_SCIENTIFIC_LIMITATIONS = [
    (
        "Higher elevation does not automatically mean a location is safe from flooding; "
        "localized depressions, micro-topography, torrential flash runoff, and drainage backflow "
        "can cause severe inundation at higher altitudes."
    ),
    (
        "Distance from a major river alone cannot determine flood safety; pluvial flooding, "
        "dam gate operations, canal breaches, and urban surface runoff can inundate areas far from primary riverbeds."
    ),
    (
        "Heavy rainfall observations and warnings signify high precipitation hazard, but do not "
        "establish that a specific coordinate is currently inundated."
    ),
    (
        "A low-lying location is not necessarily currently flooded in the absence of active inundation "
        "observations or rising floodwaters."
    ),
    (
        "Flow accumulation is a terrain-derived index of potential runoff concentration, not an actual "
        "flood depth or hydraulic simulation."
    ),
    (
        "Digital Elevation Model (DEM) analysis alone cannot reliably calculate flood inundation depth "
        "without calibrated hydrodynamic modeling, river cross-sections, and drainage capacity inputs."
    ),
    (
        "Absence of recorded flood events or warnings within the search window does not establish that "
        "the location is safe from sudden flood hazards."
    ),
    (
        "The application does not predict future flood extents or recommend specific structural shelters "
        "or evacuation routes."
    ),
]


def calculate_haversine_distance_km(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    """
    Computes great-circle distance between two pairs of WGS84 coordinates in kilometers.
    """
    earth_radius_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return earth_radius_km * c


def classify_geographical_environment(
    latitude: float,
    longitude: float,
    elevation_m: float | None = None,
    slope_degrees: float | None = None,
) -> tuple[GeographicalEnvironment, list[str]]:
    """
    Classifies the geographical environment into PLAIN_OR_FLAT or HILLY_OR_MOUNTAINOUS.
    Modular design allows specialized hilly-area flash flood logic to hook into this classification.
    """
    notes: list[str] = []

    # 1. Slope / elevation-driven classification if terrain measurements exist
    if slope_degrees is not None:
        if slope_degrees >= 15.0 or (elevation_m is not None and elevation_m > 600.0 and slope_degrees >= 8.0):
            notes.append(
                f"Classified as HILLY_OR_MOUNTAINOUS based on measured slope ({slope_degrees:.1f}°) "
                f"and elevation ({elevation_m or 'N/A'} m)."
            )
            return GeographicalEnvironment.HILLY_OR_MOUNTAINOUS, notes
        else:
            notes.append(
                f"Classified as PLAIN_OR_FLAT based on gentle slope ({slope_degrees:.1f}°)."
            )
            return GeographicalEnvironment.PLAIN_OR_FLAT, notes

    # 2. Elevation check: if elevation is under 450 meters MSL, it is a plain/plateau basin (not a rugged mountain ridge)
    from app.services.city_elevation_data import get_city_height_from_sea_level
    current_elev = elevation_m if elevation_m is not None else get_city_height_from_sea_level(latitude, longitude)
    if current_elev is not None and current_elev < 450.0:
        notes.append(f"Classified as PLAIN_OR_FLAT based on ground elevation ({current_elev:.1f} m MSL).")
        return GeographicalEnvironment.PLAIN_OR_FLAT, notes

    # 3. Regional physiographic boundary heuristic for India
    # Himalayan & North-Eastern Mountain Arc
    is_himalayan_arc = (
        (29.8 <= latitude <= 37.0 and 73.0 <= longitude <= 81.0)  # Western / Central Himalayas (HP, Uttarakhand, J&K)
        or (26.5 <= latitude <= 29.5 and 88.0 <= longitude <= 97.5)  # Eastern Himalayas & Arunachal
        or (24.0 <= latitude <= 27.5 and 91.5 <= longitude <= 95.5)  # Meghalaya, Nagaland, Manipur hills
    )
    # Western Ghats escarpment
    is_western_ghats = (8.5 <= latitude <= 21.0) and (73.2 <= longitude <= 75.8)

    if is_himalayan_arc:
        notes.append("Located within the Himalayan/North-Eastern mountainous arc physiographic zone.")
        return GeographicalEnvironment.HILLY_OR_MOUNTAINOUS, notes

    if is_western_ghats:
        notes.append("Located along the Western Ghats mountain ridge zone.")
        return GeographicalEnvironment.HILLY_OR_MOUNTAINOUS, notes

    # Indo-Gangetic and Coastal Plains
    is_indo_gangetic = (23.5 <= latitude <= 31.0) and (75.0 <= longitude <= 89.0)
    is_coastal_plain = (
        (8.0 <= latitude <= 22.0 and 72.0 <= longitude <= 73.2)  # West coast plains
        or (8.0 <= latitude <= 22.5 and 79.5 <= longitude <= 87.5)  # East coast plains
    )

    if is_indo_gangetic or is_coastal_plain:
        notes.append("Located within major alluvial/coastal plain physiographic regions.")
        return GeographicalEnvironment.PLAIN_OR_FLAT, notes

    notes.append("Physiographic environment undetermined from available regional boundaries.")
    return GeographicalEnvironment.UNDETERMINED, notes


def extract_event_coordinates(event: CrisisEvent) -> tuple[float | None, float | None]:
    """
    Extracts centroid or point coordinates (lat, lon) from a CrisisEvent.
    """
    if event.location is not None:
        geojson = safe_postgis_to_geojson(event.location)
        if geojson and geojson.get("type") == "Point":
            coords = geojson.get("coordinates")
            if coords and len(coords) >= 2:
                return float(coords[1]), float(coords[0])

    if event.affected_area is not None:
        geojson = safe_postgis_to_geojson(event.affected_area)
        if geojson:
            try:
                poly = shape(geojson)
                centroid = poly.centroid
                return float(centroid.y), float(centroid.x)
            except Exception:
                pass

    for report in getattr(event, "source_reports", []):
        if report.raw_payload and isinstance(report.raw_payload, dict):
            lat = report.raw_payload.get("latitude")
            lon = report.raw_payload.get("longitude")
            if lat is not None and lon is not None:
                try:
                    return float(lat), float(lon)
                except (ValueError, TypeError):
                    pass

    return None, None


def check_geometry_intersection(
    user_lat: float,
    user_lon: float,
    geom_data: Any,
) -> tuple[float, bool]:
    """
    Determines distance (km) and whether the user point intersects the given GeoJSON geometry.
    """
    if not geom_data or not isinstance(geom_data, dict):
        return 0.0, False

    try:
        geom = shape(geom_data)
        user_pt = Point(user_lon, user_lat)

        if geom.contains(user_pt) or geom.intersects(user_pt):
            return 0.0, True

        centroid = geom.centroid
        dist_km = calculate_haversine_distance_km(user_lat, user_lon, centroid.y, centroid.x)
        return dist_km, False
    except Exception as exc:
        logger.debug("Failed geometry intersection check: %s", exc)
        return 0.0, False


def evaluate_river_level_status(
    water_level: float | None,
    warning_level: float | None,
    danger_level: float | None,
    hfl: float | None,
) -> RiverLevelStatus:
    """
    Compares observed river water level against official CWC threshold levels:
    - Warning Level
    - Danger Level
    - Highest Flood Level (HFL)
    Never uses 0.0 for missing measurements.
    """
    if water_level is None:
        return RiverLevelStatus.UNKNOWN

    if hfl is not None and water_level >= hfl:
        return RiverLevelStatus.ABOVE_HFL

    if danger_level is not None and water_level >= danger_level:
        return RiverLevelStatus.ABOVE_DANGER

    if warning_level is not None and water_level >= warning_level:
        return RiverLevelStatus.ABOVE_WARNING

    if warning_level is not None and water_level < warning_level:
        return RiverLevelStatus.BELOW_WARNING

    return RiverLevelStatus.UNKNOWN


def evaluate_rainfall_category(intensity_val: float | None) -> str:
    """
    Maps rainfall intensity (mm/hr) to IMD meteorological rainfall categories.
    """
    if intensity_val is None:
        return "NONE_REPORTED"
    if intensity_val >= 200.0:
        return "EXTREMELY_HEAVY"
    if intensity_val >= 115.6:
        return "VERY_HEAVY"
    if intensity_val >= 15.0:
        return "HEAVY"
    if intensity_val >= 5.0:
        return "MODERATE"
    if intensity_val > 0.0:
        return "LIGHT"
    return "NONE_REPORTED"


def assess_flood_hazard(
    session: Session,
    latitude: float,
    longitude: float,
    search_radius_km: float = 50.0,
    lookback_days: int = 7,
    dem_grid: ElevationGrid | None = None,
) -> FloodAssessmentResponse:
    """
    Comprehensive Flood Geographical Hazard Assessment Engine.
    Combines:
    1. Observed flood inundation
    2. Official flood warnings
    3. River water-level measurements & thresholds
    4. Heavy rainfall observations & nowcasts
    5. Terrain & physiographic susceptibility
    6. Historical flood records
    """
    now = datetime.now(timezone.utc)
    cutoff_time = now - timedelta(days=lookback_days)
    radius_meters = search_radius_km * 1000.0

    user_location = UserLocation(latitude=latitude, longitude=longitude)
    search_params = FloodAssessmentSearchParameters(
        lookback_days=lookback_days,
        search_radius_km=search_radius_km,
    )

    contributing_factors: list[str] = []
    missing_information: list[str] = []

    # 1. Environment Classification
    env_class, env_notes = classify_geographical_environment(latitude, longitude)
    contributing_factors.extend(env_notes)

    # 2. Query Flood CrisisEvents
    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    user_geom = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326)

    dist_location = func.ST_Distance(CrisisEvent.location.cast(Geography), user_geog)
    dist_affected = func.ST_Distance(CrisisEvent.affected_area.cast(Geography), user_geog)

    flood_events_query = (
        select(
            CrisisEvent,
            dist_location.label("dist_loc"),
            dist_affected.label("dist_aff"),
        )
        .where(
            and_(
                CrisisEvent.crisis_type == CrisisType.FLOOD,
                CrisisEvent.started_at.is_not(None),
                CrisisEvent.started_at >= cutoff_time,
                or_(
                    and_(
                        CrisisEvent.location.is_not(None),
                        func.ST_DWithin(CrisisEvent.location.cast(Geography), user_geog, radius_meters),
                    ),
                    and_(
                        CrisisEvent.affected_area.is_not(None),
                        func.ST_DWithin(CrisisEvent.affected_area.cast(Geography), user_geog, radius_meters),
                    ),
                    and_(
                        CrisisEvent.affected_area.is_not(None),
                        func.ST_Intersects(CrisisEvent.affected_area, user_geom),
                    ),
                ),
            )
        )
        .options(
            selectinload(CrisisEvent.source_reports).selectinload(CrisisSourceReport.data_source),
            selectinload(CrisisEvent.hazard_observations),
        )
        .order_by(CrisisEvent.started_at.desc())
    )

    try:
        raw_events = session.execute(flood_events_query).all()
    except Exception as exc:
        logger.warning("Spatial query for flood events fallback: %s", exc)
        # In-memory / Mock fallback
        stmt_fallback = (
            select(CrisisEvent)
            .where(
                and_(
                    CrisisEvent.crisis_type == CrisisType.FLOOD,
                    CrisisEvent.started_at.is_not(None),
                    CrisisEvent.started_at >= cutoff_time,
                )
            )
            .options(
                selectinload(CrisisEvent.source_reports).selectinload(CrisisSourceReport.data_source),
                selectinload(CrisisEvent.hazard_observations),
            )
        )
        candidates = session.scalars(stmt_fallback).all()
        raw_events = []
        for ev in candidates:
            # Check point
            ev_lat, ev_lon = extract_event_coordinates(ev)
            dist_km = (
                calculate_haversine_distance_km(latitude, longitude, ev_lat, ev_lon)
                if ev_lat is not None and ev_lon is not None
                else 0.0
            )
            # Check affected_area polygon
            aff_geojson = safe_postgis_to_geojson(ev.affected_area) if ev.affected_area is not None else None
            intersects_poly = False
            if aff_geojson:
                p_dist, intersects_poly = check_geometry_intersection(latitude, longitude, aff_geojson)
                if intersects_poly:
                    dist_km = 0.0

            if dist_km <= search_radius_km or intersects_poly:
                raw_events.append((ev, dist_km * 1000.0, 0.0 if intersects_poly else dist_km * 1000.0))

    # 3. Query Standalone HazardObservations (e.g. CWC river gauges & IMD stations)
    dist_obs = func.ST_Distance(HazardObservation.geometry.cast(Geography), user_geog)
    obs_query = (
        select(HazardObservation, dist_obs.label("obs_dist"))
        .where(
            and_(
                HazardObservation.observed_at >= cutoff_time,
                HazardObservation.geometry.is_not(None),
                func.ST_DWithin(HazardObservation.geometry.cast(Geography), user_geog, radius_meters),
            )
        )
        .options(
            selectinload(HazardObservation.data_source),
            selectinload(HazardObservation.crisis_event),
        )
        .order_by(dist_obs.asc())
    )

    try:
        raw_observations = session.execute(obs_query).all()
    except Exception as exc:
        logger.warning("Spatial query for flood observations fallback: %s", exc)
        obs_fallback = (
            select(HazardObservation)
            .where(HazardObservation.observed_at >= cutoff_time)
            .options(
                selectinload(HazardObservation.data_source),
                selectinload(HazardObservation.crisis_event),
            )
        )
        obs_candidates = session.scalars(obs_fallback).all()
        raw_observations = []
        for ob in obs_candidates:
            geom_dict = safe_postgis_to_geojson(ob.geometry) if ob.geometry is not None else None
            if geom_dict and geom_dict.get("type") == "Point":
                coords = geom_dict.get("coordinates")
                if coords and len(coords) >= 2:
                    d_km = calculate_haversine_distance_km(latitude, longitude, coords[1], coords[0])
                    if d_km <= search_radius_km:
                        raw_observations.append((ob, d_km * 1000.0))

    # Evidence containers
    observed_flooding_list: list[ObservedFloodEvidence] = []
    official_warnings_list: list[FloodWarningEvidence] = []
    river_observations_list: list[RiverObservationEvidence] = []
    rainfall_information_list: list[RainfallEvidence] = []
    source_attribution_map: dict[str, DisasterSourceSummary] = {}

    all_fresh = False
    any_stale = False

    # Process CrisisEvents
    for item in raw_events:
        event = item[0]
        dist_loc_m = item[1] if len(item) > 1 else None
        dist_aff_m = item[2] if len(item) > 2 else None

        # Distance resolution
        min_dist_m = None
        if dist_aff_m is not None and dist_loc_m is not None:
            min_dist_m = min(float(dist_aff_m), float(dist_loc_m))
        elif dist_aff_m is not None:
            min_dist_m = float(dist_aff_m)
        elif dist_loc_m is not None:
            min_dist_m = float(dist_loc_m)
        else:
            ev_lat, ev_lon = extract_event_coordinates(event)
            min_dist_m = (
                calculate_haversine_distance_km(latitude, longitude, ev_lat, ev_lon) * 1000.0
                if ev_lat is not None and ev_lon is not None
                else 0.0
            )

        distance_km = round(min_dist_m / 1000.0, 2)

        # Check intersection with affected_area
        intersects_location = False
        if min_dist_m <= 0.0:
            intersects_location = True
        elif event.affected_area is not None:
            aff_dict = safe_postgis_to_geojson(event.affected_area)
            if aff_dict:
                _, intersects_location = check_geometry_intersection(latitude, longitude, aff_dict)
                if intersects_location:
                    distance_km = 0.0

        # Source name
        src_name = "Official Disaster Feed"
        headline = None
        warning_level = None
        for rep in getattr(event, "source_reports", []):
            if rep.data_source:
                src_name = rep.data_source.name
            if rep.source_title:
                headline = rep.source_title
            if rep.source_severity:
                warning_level = rep.source_severity

            # Record attribution
            if rep.data_source and rep.data_source.code not in source_attribution_map:
                source_attribution_map[rep.data_source.code] = DisasterSourceSummary(
                    source_name=rep.data_source.name,
                    source_type=rep.data_source.source_type,
                    source_url=rep.source_url,
                    external_event_id=rep.external_event_id,
                    issued_at=rep.issued_at,
                    fetched_at=rep.fetched_at or now,
                    valid_until=rep.valid_until,
                )

        freshness = evaluate_freshness(event, now)
        if freshness == "FRESH":
            all_fresh = True
        elif freshness in ("STALE", "EXPIRED"):
            any_stale = True

        # Distinguish observed flooding vs official warnings
        title_lower = (event.title or "").lower()
        desc_lower = (event.description or "").lower()
        is_observed_inundation = any(
            w in title_lower or w in desc_lower
            for w in ("inundation observed", "submerged", "inundated", "flood observed", "actual flooding")
        )

        if is_observed_inundation:
            observed_flooding_list.append(
                ObservedFloodEvidence(
                    event_id=event.id,
                    title=event.title,
                    description=event.description,
                    observed_at=event.started_at,
                    distance_km=distance_km,
                    intersects_user_location=intersects_location,
                    source_name=src_name,
                    reported_extent_or_depth=warning_level or "Reported Inundation",
                )
            )
            contributing_factors.append(
                f"Observed flood inundation reported {distance_km:.1f} km away "
                f"('{event.title or 'Flood Event'}')."
            )
        else:
            official_warnings_list.append(
                FloodWarningEvidence(
                    event_id=event.id,
                    title=event.title,
                    description=event.description,
                    severity=event.severity,
                    status=event.status,
                    warning_issued_at=event.started_at,
                    valid_until=event.expires_at,
                    distance_km=distance_km,
                    intersects_user_location=intersects_location,
                    source_name=src_name,
                    headline=headline,
                    warning_level=warning_level,
                )
            )
            if intersects_location:
                contributing_factors.append(
                    f"User location intersects active official flood warning area: '{event.title or headline}'."
                )
            else:
                contributing_factors.append(
                    f"Official flood warning active {distance_km:.1f} km away: '{event.title or headline}'."
                )

    # Process Standalone / Linked HazardObservations
    for ob, dist_m in raw_observations:
        obs_dist_km = round(float(dist_m) / 1000.0, 2)
        obs_type = (ob.observation_type or "").upper()
        props = ob.properties or {}

        # River Water Level & Discharge
        if any(k in obs_type for k in ("RIVER", "WATER_LEVEL", "DISCHARGE")):
            w_level = float(ob.value_numeric) if ob.value_numeric is not None else None
            warn_lvl = float(props["warning_level_m"]) if props.get("warning_level_m") is not None else None
            dang_lvl = float(props["danger_level_m"]) if props.get("danger_level_m") is not None else None
            hfl_lvl = float(props["hfl_m"]) if props.get("hfl_m") is not None else None

            rel_status = evaluate_river_level_status(w_level, warn_lvl, dang_lvl, hfl_lvl)

            river_obs = RiverObservationEvidence(
                station_code=props.get("station_code") or str(ob.id)[:8],
                station_name=props.get("station_name"),
                river_name=props.get("river_name"),
                basin=props.get("basin"),
                water_level_m=w_level,
                warning_level_m=warn_lvl,
                danger_level_m=dang_lvl,
                hfl_m=hfl_lvl,
                distance_km=obs_dist_km,
                observed_at=ob.observed_at,
                status_relative_to_danger=rel_status,
            )
            river_observations_list.append(river_obs)

            if rel_status in (RiverLevelStatus.ABOVE_DANGER, RiverLevelStatus.ABOVE_HFL):
                contributing_factors.append(
                    f"CWC River Station '{river_obs.station_name or river_obs.station_code}' on river "
                    f"'{river_obs.river_name or 'N/A'}' is {rel_status.value} (water level: {w_level} m)."
                )
            elif rel_status == RiverLevelStatus.ABOVE_WARNING:
                contributing_factors.append(
                    f"CWC River Station '{river_obs.station_name or river_obs.station_code}' is ABOVE_WARNING "
                    f"(water level: {w_level} m; warning level: {warn_lvl} m)."
                )

        # Rainfall Observations / Nowcasts
        elif any(k in obs_type for k in ("RAIN", "PRECIPITATION")):
            intensity_val = float(ob.value_numeric) if ob.value_numeric is not None else None
            category = evaluate_rainfall_category(intensity_val)
            color_code = props.get("color_code")
            color_label = "Red" if color_code == 4 else ("Orange" if color_code == 3 else ("Yellow" if color_code == 2 else None))

            rain_obs = RainfallEvidence(
                station_or_district=props.get("district") or props.get("station_name"),
                rainfall_intensity_mm_hr=intensity_val,
                accumulation_period_hours=props.get("accumulation_hours"),
                rainfall_category=category,
                observed_or_forecast="NOWCAST_FORECAST" if "NOWCAST" in obs_type else "OBSERVED",
                distance_km=obs_dist_km,
                observed_at=ob.observed_at,
                warning_color=color_label,
            )
            rainfall_information_list.append(rain_obs)

            if intensity_val is not None and intensity_val >= 15.0:
                contributing_factors.append(
                    f"Heavy rainfall observed/forecast ({intensity_val} mm/hr) {obs_dist_km:.1f} km away "
                    f"in {rain_obs.station_or_district or 'district'}."
                )

        if ob.data_source and ob.data_source.code not in source_attribution_map:
            source_attribution_map[ob.data_source.code] = DisasterSourceSummary(
                source_name=ob.data_source.name,
                source_type=ob.data_source.source_type,
                source_url=ob.data_source.base_url,
                external_event_id=str(ob.id),
                issued_at=ob.observed_at,
                fetched_at=ob.ingested_at,
                valid_until=None,
            )

    # 4. Terrain & Specialized Hilly-Area Evidence
    hilly_analysis: HillyTerrainAnalysis | None = None
    terrain_hazard_statuses: list[TerrainHazardStatus] = []

    if env_class == GeographicalEnvironment.HILLY_OR_MOUNTAINOUS or dem_grid is not None:
        terrain_evidence, hilly_analysis = analyze_hilly_terrain_hazard(
            latitude=latitude,
            longitude=longitude,
            dem_grid=dem_grid,
            nearby_rainfall_obs=rainfall_information_list,
            nearby_warnings=official_warnings_list,
            nearby_river_obs=river_observations_list,
            observed_floods=observed_flooding_list,
        )
        terrain_hazard_statuses = hilly_analysis.terrain_hazard_statuses
        contributing_factors.extend(hilly_analysis.contributing_factors)
        for lim in hilly_analysis.limitations_and_assumptions:
            if lim not in FLOOD_SCIENTIFIC_LIMITATIONS:
                FLOOD_SCIENTIFIC_LIMITATIONS.append(lim)

        if not terrain_evidence.available:
            missing_information.append(
                "Local high-resolution Digital Elevation Model (DEM) raster dataset is unavailable. "
                "Terrain-derived slope, flow accumulation, and flow direction are uncomputed."
            )
    else:
        # Flat alluvial plains or undetermined terrain without DEM
        terrain_notes = [
            f"Physiographic broad classification assigned as {env_class.value} based on regional geographical boundaries.",
            "General flat-plain flood analysis applied. Specialized mountain drainage analysis is not required for plain terrain.",
        ]
        terrain_evidence = TerrainEvidence(
            available=False,
            elevation_m=None,
            slope_degrees=None,
            aspect=None,
            flow_accumulation=None,
            drainage_proximity_m=None,
            upstream_catchment_area_km2=None,
            flow_direction_deg=None,
            topographic_wetness_index=None,
            relative_elevation_above_drainage_m=None,
            valley_confinement=None,
            geographical_environment=env_class,
            notes=terrain_notes,
        )
        missing_information.append(
            "Local high-resolution Digital Elevation Model (DEM) raster dataset is unavailable. "
            "Terrain-derived slope, flow accumulation, and flow direction are uncomputed."
        )

    missing_information.append(
        "Central Water Commission (CWC) automated telemetry REST API requires NWIC organizational credentials; live river gauge feeds may require manual synchronization."
    )
    missing_information.append(
        "National Database for Emergency Management (NDEM) satellite flood extent layers require restricted government endorsement and are not publicly queried."
    )

    # 5. Historical Flood Evidence
    historical_count = 0
    last_historical_date = None
    try:
        hist_stmt = (
            select(func.count(CrisisEvent.id), func.max(CrisisEvent.started_at))
            .where(
                and_(
                    CrisisEvent.crisis_type == CrisisType.FLOOD,
                    CrisisEvent.started_at < cutoff_time,
                    func.ST_DWithin(CrisisEvent.location.cast(Geography), user_geog, radius_meters),
                )
            )
        )
        hist_row = session.execute(hist_stmt).first()
        if hist_row:
            historical_count = int(hist_row[0] or 0)
            last_historical_date = hist_row[1]
    except Exception:
        pass

    hist_notes = []
    if historical_count > 0:
        hist_notes.append(f"Recorded {historical_count} historical flood events in database within {search_radius_km:.1f} km.")
    else:
        hist_notes.append("No historical flood events recorded in database prior to the lookback window.")

    historical_evidence = HistoricalFloodEvidence(
        available=historical_count > 0,
        historical_events_count=historical_count,
        last_recorded_event_date=last_historical_date,
        notes=hist_notes,
    )

    # 6. Evaluate Overall Status
    has_active_intersecting_warning = any(
        w.intersects_user_location for w in official_warnings_list
    )
    has_river_above_danger = any(
        r.status_relative_to_danger in (RiverLevelStatus.ABOVE_DANGER, RiverLevelStatus.ABOVE_HFL)
        for r in river_observations_list
    )
    has_river_above_warning = any(
        r.status_relative_to_danger == RiverLevelStatus.ABOVE_WARNING
        for r in river_observations_list
    )
    has_heavy_rainfall = any(
        (rf.rainfall_intensity_mm_hr or 0) >= 15.0 or rf.warning_color in ("Red", "Orange")
        for rf in rainfall_information_list
    )

    overall_freshness = "FRESH" if all_fresh else ("STALE" if any_stale else "UNKNOWN")

    total_evidence_count = (
        len(observed_flooding_list)
        + len(official_warnings_list)
        + len(river_observations_list)
        + len(rainfall_information_list)
    )

    is_all_stale = (total_evidence_count > 0) and (not all_fresh) and any_stale

    if is_all_stale:
        assessment_status = FloodAssessmentStatus.DATA_STALE
        summary_msg = (
            f"Found {total_evidence_count} flood evidence record(s) within {search_radius_km:.1f} km, "
            f"but all records are marked stale or expired."
        )
    elif len(observed_flooding_list) > 0:
        assessment_status = FloodAssessmentStatus.OBSERVED_FLOODING
        summary_msg = (
            f"Active flood inundation observed within {search_radius_km:.1f} km. "
            f"Found {len(observed_flooding_list)} reported inundation record(s)."
        )
    elif has_active_intersecting_warning or has_river_above_danger:
        assessment_status = FloodAssessmentStatus.OFFICIAL_FLOOD_WARNING
        if has_active_intersecting_warning:
            summary_msg = (
                f"Location intersects an active official flood/heavy-rainfall warning zone. "
                f"Official alert issued for this area."
            )
        else:
            summary_msg = (
                f"Nearby river telemetry indicates water level ABOVE DANGER threshold. "
                f"Official flood warning conditions present."
            )
    elif hilly_analysis is not None and TerrainHazardStatus.HIGH_FLOW_PATH_EXPOSURE in terrain_hazard_statuses:
        assessment_status = FloodAssessmentStatus.HIGH_FLOW_PATH_EXPOSURE
        summary_msg = (
            f"Mountain terrain hazard: Location lies along a concentrated water-flow path "
            f"(accumulation: {terrain_evidence.flow_accumulation:.0f} cells)."
        )
    elif hilly_analysis is not None and TerrainHazardStatus.UPSTREAM_CATCHMENT_CONCERN in terrain_hazard_statuses and (has_heavy_rainfall or has_river_above_warning):
        assessment_status = FloodAssessmentStatus.UPSTREAM_CATCHMENT_CONCERN
        summary_msg = (
            f"Mountain terrain hazard: Upstream catchment of {terrain_evidence.upstream_catchment_area_km2} km² "
            "drains towards this location with active precipitation."
        )
    elif hilly_analysis is not None and TerrainHazardStatus.VALLEY_FLOOD_CONCERN in terrain_hazard_statuses:
        assessment_status = FloodAssessmentStatus.VALLEY_FLOOD_CONCERN
        summary_msg = (
            f"Mountain terrain hazard: Confined valley topography ({terrain_evidence.valley_confinement}) "
            "restricts runoff dispersion."
        )
    elif hilly_analysis is not None and TerrainHazardStatus.DEBRIS_FLOW_CONCERN in terrain_hazard_statuses:
        assessment_status = FloodAssessmentStatus.DEBRIS_FLOW_CONCERN
        summary_msg = (
            "Mountain terrain hazard: Steep drainage convergence creates debris-flow susceptibility."
        )
    elif hilly_analysis is not None and TerrainHazardStatus.LANDSLIDE_RELATED_CONCERN in terrain_hazard_statuses:
        assessment_status = FloodAssessmentStatus.LANDSLIDE_RELATED_CONCERN
        summary_msg = (
            f"Mountain terrain hazard: Steep slope ({terrain_evidence.slope_degrees:.1f}°) elevates landslide susceptibility."
        )
    elif len(official_warnings_list) > 0 or has_river_above_warning or has_heavy_rainfall:
        assessment_status = FloodAssessmentStatus.ELEVATED_FLOOD_SUSCEPTIBILITY
        summary_msg = (
            f"Elevated flood susceptibility detected within {search_radius_km:.1f} km based on "
            f"nearby official warnings, heavy precipitation, or river levels above warning mark."
        )
    elif total_evidence_count == 0:
        assessment_status = FloodAssessmentStatus.NO_RELEVANT_FLOOD_EVIDENCE
        summary_msg = (
            f"No relevant flood event, official warning, or abnormal river gauge was found in the available dataset "
            f"within {search_radius_km:.1f} km over the past {lookback_days} days. "
            f"This does not establish that the location is safe."
        )
    else:
        assessment_status = FloodAssessmentStatus.NO_RELEVANT_FLOOD_EVIDENCE
        summary_msg = (
            f"No active flood detected within {search_radius_km:.1f} km. "

        )

    # Check conflicting information
    if has_heavy_rainfall and not has_river_above_warning and len(river_observations_list) > 0:
        contributing_factors.append(
            "Source Divergence: Heavy rainfall warning recorded while nearby river gauges remain below warning level. "
            "Hydrological runoff and river rise may lag behind atmospheric precipitation."
        )

    return FloodAssessmentResponse(
        success=True,
        user_location=user_location,
        assessment_timestamp=now,
        geographical_environment=env_class,
        assessment_status=assessment_status,
        terrain_hazard_statuses=terrain_hazard_statuses,
        summary_message=summary_msg,
        search_parameters=search_params,
        observed_flooding=observed_flooding_list,
        official_warnings=official_warnings_list,
        river_observations=river_observations_list,
        rainfall_information=rainfall_information_list,
        terrain_information=terrain_evidence,
        historical_flood_information=historical_evidence,
        hilly_terrain_analysis=hilly_analysis,
        contributing_factors=contributing_factors,
        missing_information=missing_information,
        data_freshness=overall_freshness,
        source_attribution=list(source_attribution_map.values()),
        scientific_limitations=FLOOD_SCIENTIFIC_LIMITATIONS,
    )
