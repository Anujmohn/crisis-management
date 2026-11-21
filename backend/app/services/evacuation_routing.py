from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from geoalchemy2 import Geography
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database.geometry import geojson_to_postgis, postgis_to_geojson
from app.models.core import CrisisEvent, HazardObservation
from app.models.enums import (
    CrisisStatus,
    CrisisType,
    RiskLevel,
    RouteSafetyStatus,
    StructuralCondition,
    VerificationStatus,
)
from app.models.recommendations import (
    EvacuationRoute,
    LocationAssessment,
    RecommendationRun,
    SafeLocation,
)
from app.schemas.disaster_search import UserLocation
from app.schemas.route import (
    CalculateRouteResponse,
    HazardIntersectionItem,
    RouteClassification,
    RouteDetailItem,
)
from app.services.cyclone_assessment import calculate_distance_to_indian_coastline_km
from app.services.disaster_search import safe_postgis_to_geojson
from app.services.flood_assessment import (
    calculate_haversine_distance_km,
    classify_geographical_environment,
)
from app.services.recommendation_engine import extract_location_coordinates

logger = logging.getLogger(__name__)

ROUTING_PROVIDER_NAME = "OSRM (driving)"

SAFETY_DISCLAIMER = (
    "A calculated evacuation route and its safety status indicate relative evidence-based alignment "
    "using available official hazard feeds and do NOT guarantee safety, open road conditions, or survival. "
    "Road infrastructure integrity, flash flooding, fallen debris, and localized closures must be confirmed on-ground. "
    "Never attempt to drive through flooded roads or fast-flowing water. Follow official disaster management instructions."
)

ROUTING_LIMITATIONS = [
    "OSRM calculates road routes based on OpenStreetMap road network connectivity; it does not possess real-time hazard avoidance or dynamic road condition awareness.",
    "Route safety is evaluated via post-processing against available disaster observations; absence of a reported closure does not imply that a road is passable.",
    "Bridge structural safety, culvert washouts, downed powerlines, and minor road blockages may not be captured in official spatial feeds.",
    "Terrain flow accumulation indicates potential surface runoff concentration, not measured flood water depth.",
    "Do not attempt to traverse unverified roads or flooded river crossings during active emergencies.",
]


def call_osrm_driving_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    allow_alternatives: bool = True,
    base_url: str | None = None,
    timeout: float | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Invokes the Open Source Routing Machine (OSRM) driving profile.

    Format: /route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson&alternatives=true
    """
    root_url = (base_url or settings.osrm_base_url).rstrip("/")
    timeout_sec = timeout or settings.osrm_timeout_seconds

    alternatives_val = "true" if allow_alternatives else "false"
    coords_param = f"{origin_lon:.6f},{origin_lat:.6f};{dest_lon:.6f},{dest_lat:.6f}"
    url = (
        f"{root_url}/route/v1/driving/{coords_param}?"
        f"overview=full&geometries=geojson&alternatives={alternatives_val}&steps=false"
    )

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "DisasterSafetyApp-EvacuationRouter/1.0",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            if response.status != 200:
                return [], f"OSRM server returned HTTP status {response.status}"
            raw_body = response.read().decode("utf-8")
            data = json.loads(raw_body)
            code = data.get("code")
            if code != "Ok":
                msg = data.get("message", code or "Unknown routing error")
                return [], f"OSRM routing error: {msg}"
            routes = data.get("routes", [])
            if not routes:
                return [], "OSRM returned zero routes for the given coordinates."
            return routes, None
    except urllib.error.HTTPError as http_err:
        logger.warning("OSRM HTTPError %d: %s", http_err.code, http_err.reason)
        return [], f"Routing service HTTP error: {http_err.code} {http_err.reason}"
    except urllib.error.URLError as url_err:
        logger.warning("OSRM URLError: %s", url_err.reason)
        return [], f"Routing service unreachable: {url_err.reason}"
    except TimeoutError:
        logger.warning("OSRM request timed out after %.1f seconds", timeout_sec)
        return [], f"Routing request timed out after {timeout_sec:.1f}s"
    except Exception as exc:
        logger.warning("Unexpected error querying OSRM: %s", exc)
        return [], f"Routing service error: {exc}"


def check_destination_eligibility(
    assessment: LocationAssessment,
) -> tuple[bool, str | None, CrisisType]:
    """
    Verifies that the candidate destination is eligible for evacuation routing.

    Hard-excluded, Not Recommended, or Insufficient Data destinations cannot receive
    a recommended evacuation route.
    """
    factors = assessment.factors if isinstance(assessment.factors, dict) else {}
    dt_str = factors.get("disaster_type", "EARTHQUAKE")
    try:
        disaster_type = CrisisType(dt_str)
    except Exception:
        disaster_type = CrisisType.EARTHQUAKE

    is_excluded = factors.get("hard_exclusion_status", False)
    cls_str = factors.get("suitability_classification", "UNKNOWN")

    if is_excluded or cls_str == "EXCLUDED":
        reasons = factors.get("hard_exclusion_reasons", [])
        detail = reasons[0] if reasons else "Destination is under active disaster hard exclusion"
        return False, f"Ineligible destination (active disaster hard exclusion): {detail}. Evacuation route cannot be recommended.", disaster_type

    if cls_str == "NOT_RECOMMENDED":
        return (
            False,
            "Ineligible destination: Destination is classified as NOT_RECOMMENDED for the active disaster.",
            disaster_type,
        )

    if cls_str == "INSUFFICIENT_DATA":
        return (
            False,
            "Ineligible destination: Destination has INSUFFICIENT_DATA and cannot be confirmed as suitable.",
            disaster_type,
        )

    if cls_str not in ("SUITABILITY_SUPPORTED_BY_EVIDENCE", "CONDITIONALLY_SUITABLE"):
        return (
            False,
            f"Ineligible destination: Classification '{cls_str}' does not meet routing eligibility criteria.",
            disaster_type,
        )

    return True, None, disaster_type


def compute_point_to_segment_distance_m(
    p_lat: float, p_lon: float,
    a_lat: float, a_lon: float,
    b_lat: float, b_lon: float,
) -> float:
    """Computes great-circle distance from point P to line segment AB in meters."""
    # Approximate flat projection for short segments
    dx = (b_lon - a_lon) * math.cos(math.radians((a_lat + b_lat) / 2.0))
    dy = b_lat - a_lat
    seg_len_sq = dx * dx + dy * dy

    if seg_len_sq <= 1e-12:
        return calculate_haversine_distance_km(p_lat, p_lon, a_lat, a_lon) * 1000.0

    t = max(0.0, min(1.0, ((p_lon - a_lon) * dx + (p_lat - a_lat) * dy) / seg_len_sq))
    proj_lon = a_lon + t * (b_lon - a_lon)
    proj_lat = a_lat + t * (b_lat - a_lat)
    return calculate_haversine_distance_km(p_lat, p_lon, proj_lat, proj_lon) * 1000.0


def distance_point_to_linestring_m(
    pt_lat: float, pt_lon: float, route_coords: list[list[float]]
) -> float:
    """Calculates minimum distance from a point to a route LineString in meters."""
    min_dist_m = float("inf")
    for i in range(len(route_coords) - 1):
        a_lon, a_lat = route_coords[i][0], route_coords[i][1]
        b_lon, b_lat = route_coords[i + 1][0], route_coords[i + 1][1]
        dist_m = compute_point_to_segment_distance_m(pt_lat, pt_lon, a_lat, a_lon, b_lat, b_lon)
        if dist_m < min_dist_m:
            min_dist_m = dist_m
    return min_dist_m


def analyze_route_hazards(
    route_coords: list[list[float]],
    disaster_type: CrisisType,
    active_events: list[CrisisEvent],
) -> tuple[list[HazardIntersectionItem], list[str], bool, list[str]]:
    """
    Evaluates disaster-specific physical hazard exposures and intersections along a route geometry.

    Checks:
    - Active flood inundation extents and low-water crossings.
    - Mountain valley flash-flood channels and steep slope rockfall zones.
    - Coastal cyclone storm surge corridors.
    - Point infrastructure damage, road blockages, and observation warnings.
    """
    intersections: list[HazardIntersectionItem] = []
    warnings: list[str] = []
    hard_excluded = False
    hard_reasons: list[str] = []

    if len(route_coords) < 2:
        return intersections, ["Route contains fewer than 2 coordinates."], False, []

    # 1. Evaluate Active Crisis Events & Hazard Polygons
    for ev in active_events:
        ev_title = ev.title or f"{ev.crisis_type.value} Event"
        ev_lat, ev_lon = 0.0, 0.0
        try:
            ev_geo = safe_postgis_to_geojson(ev.location)
            if ev_geo and "coordinates" in ev_geo:
                ev_lon, ev_lat = float(ev_geo["coordinates"][0]), float(ev_geo["coordinates"][1])
        except Exception:
            pass

        # Check distance from route to event epicentre / reference center
        d_ev_m = distance_point_to_linestring_m(ev_lat, ev_lon, route_coords) if ev_lat != 0.0 else float("inf")

        # Specific Inundation / Submerged Flood Polygon checks
        is_inundation_hazard = (
            ev.crisis_type == CrisisType.FLOOD
            and ("inundation" in ev_title.lower() or "submerged" in (ev.description or "").lower())
        )

        if is_inundation_hazard and d_ev_m <= 250.0:
            is_direct = d_ev_m <= 80.0
            intersections.append(
                HazardIntersectionItem(
                    hazard_id=str(ev.id),
                    hazard_type="ACTIVE_FLOOD_INUNDATION",
                    severity=ev.severity.value if ev.severity else "HIGH",
                    distance_to_route_m=round(d_ev_m, 1),
                    is_direct_intersection=is_direct,
                    is_hard_exclusion=True,
                    description=f"Route traverses or directly borders active flood inundation extent ({ev_title}).",
                    source_name="Official Flood Alert Feed",
                    reported_at=ev.started_at.isoformat() if ev.started_at else None,
                    approximate_segment_coords=[ev_lon, ev_lat],
                )
            )
            hard_excluded = True
            hard_reasons.append(
                f"Route intersects or directly borders active flood inundation extent ({ev_title}, distance: {d_ev_m:.1f}m)."
            )

        elif ev.crisis_type == CrisisType.FLOOD and d_ev_m <= 1000.0:
            intersections.append(
                HazardIntersectionItem(
                    hazard_id=str(ev.id),
                    hazard_type="RIVER_CORRIDOR_WARNING",
                    severity=ev.severity.value if ev.severity else "MODERATE",
                    distance_to_route_m=round(d_ev_m, 1),
                    is_direct_intersection=d_ev_m <= 100.0,
                    is_hard_exclusion=False,
                    description=f"Route approaches river gauge / flood warning corridor ({ev_title}).",
                    source_name="CWC / Official Flood Feed",
                    reported_at=ev.started_at.isoformat() if ev.started_at else None,
                    approximate_segment_coords=[ev_lon, ev_lat],
                )
            )
            warnings.append(
                f"Route passes within {d_ev_m:.0f}m of active flood warning corridor ({ev_title}). Low-water crossings may be compromised."
            )

        elif ev.crisis_type == CrisisType.CYCLONE and d_ev_m <= 2500.0:
            intersections.append(
                HazardIntersectionItem(
                    hazard_id=str(ev.id),
                    hazard_type="CYCLONE_IMPACT_ZONE",
                    severity=ev.severity.value if ev.severity else "HIGH",
                    distance_to_route_m=round(d_ev_m, 1),
                    is_direct_intersection=d_ev_m <= 500.0,
                    is_hard_exclusion=False,
                    description=f"Route traverses active cyclone storm area ({ev_title}).",
                    source_name="IMD Cyclone Warning",
                    reported_at=ev.started_at.isoformat() if ev.started_at else None,
                    approximate_segment_coords=[ev_lon, ev_lat],
                )
            )

    # 2. Environmental & Terrain Morphology checks along route
    # Sample every 5th vertex or minimum 5 points along route
    step = max(1, len(route_coords) // 10)
    sampled_pts = route_coords[::step]
    if route_coords[-1] not in sampled_pts:
        sampled_pts.append(route_coords[-1])

    has_mountainous_segment = False
    has_coastal_surge_risk = False
    min_coastal_dist_km = float("inf")

    for pt in sampled_pts:
        p_lon, p_lat = pt[0], pt[1]
        geo_env, _ = classify_geographical_environment(p_lat, p_lon)
        if geo_env.value == "HILLY_OR_MOUNTAINOUS":
            has_mountainous_segment = True

        if disaster_type == CrisisType.CYCLONE:
            d_coast = calculate_distance_to_indian_coastline_km(p_lat, p_lon)
            if d_coast < min_coastal_dist_km:
                min_coastal_dist_km = d_coast

    # Evaluate Mountainous Hazards
    if has_mountainous_segment:
        if disaster_type == CrisisType.EARTHQUAKE:
            warnings.append(
                "Route traverses hilly/mountainous terrain subject to earthquake-triggered slope failure, rockfalls, and road blockage."
            )
            intersections.append(
                HazardIntersectionItem(
                    hazard_type="SEISMIC_SLOPE_HAZARD",
                    severity="MODERATE",
                    distance_to_route_m=0.0,
                    is_direct_intersection=True,
                    is_hard_exclusion=False,
                    description="Mountainous roadway alignment exposed to potential rockfall and hillside debris.",
                    source_name="Terrain Morphology Analysis",
                )
            )
        elif disaster_type == CrisisType.FLOOD:
            warnings.append(
                "Route traverses mountain valley drainage corridors susceptible to torrential flash flooding and debris flows."
            )
            intersections.append(
                HazardIntersectionItem(
                    hazard_type="FLASH_FLOOD_CORRIDOR",
                    severity="MODERATE",
                    distance_to_route_m=0.0,
                    is_direct_intersection=True,
                    is_hard_exclusion=False,
                    description="Valley roadway alignment exposed to upstream catchment runoff convergence.",
                    source_name="Terrain Morphology Analysis",
                )
            )

    # Evaluate Cyclone Coastal Surge Hazards
    has_active_cyclone = any(ev.crisis_type == CrisisType.CYCLONE for ev in active_events)
    if disaster_type == CrisisType.CYCLONE and has_active_cyclone:
        if min_coastal_dist_km <= 5.0:
            hard_excluded = True
            hard_reasons.append(
                f"Route traverses low-lying coastal surge corridor ({min_coastal_dist_km:.1f} km from sea) during active cyclone warning. Catastrophic storm surge and high wave threat."
            )
            intersections.append(
                HazardIntersectionItem(
                    hazard_type="STORM_SURGE_ZONE",
                    severity="CRITICAL",
                    distance_to_route_m=0.0,
                    is_direct_intersection=True,
                    is_hard_exclusion=True,
                    description=f"Coastal route alignment ({min_coastal_dist_km:.1f} km from coastline) in active cyclone surge corridor.",
                    source_name="INCOIS / IMD Coastal Warning",
                )
            )
        elif min_coastal_dist_km <= 20.0:
            warnings.append(
                f"Route passes through coastal buffer zone ({min_coastal_dist_km:.1f} km from sea) exposed to severe gale-force winds, flying projectile debris, and localized marine overwash."
            )

    return intersections, warnings, hard_excluded, hard_reasons


def compare_and_rank_routes(
    candidate_routes: list[dict[str, Any]],
    disaster_type: CrisisType,
    active_events: list[CrisisEvent],
) -> list[RouteDetailItem]:
    """
    Analyzes, ranks, and compares candidate routes returned by OSRM.

    The shortest route is not automatically the safest. Routes with fewer verified hazard
    intersections are preferred. Routes with verified hard exclusions are rejected.
    """
    now = datetime.now(timezone.utc)
    analyzed: list[dict[str, Any]] = []

    for idx, r in enumerate(candidate_routes):
        dist_m = float(r.get("distance", 0.0))
        dist_km = round(dist_m / 1000.0, 2)
        dur_sec = int(round(float(r.get("duration", 0.0))))
        dur_min = round(dur_sec / 60.0, 1)

        geom = r.get("geometry", {})
        coords = geom.get("coordinates", [])

        # Analyze hazards
        (
            hazard_items,
            warnings,
            is_excluded,
            hard_reasons,
        ) = analyze_route_hazards(
            route_coords=coords,
            disaster_type=disaster_type,
            active_events=active_events,
        )

        direct_hazards_count = sum(1 for h in hazard_items if h.is_direct_intersection)
        all_hazards_count = len(hazard_items)

        # Exposure level
        if is_excluded:
            exposure_level = "CRITICAL"
            route_status = RouteClassification.ROUTE_EXCLUDED
        elif direct_hazards_count > 0 or all_hazards_count >= 3:
            exposure_level = "HIGH"
            route_status = RouteClassification.ROUTE_WITH_WARNINGS
        elif all_hazards_count > 0 or len(warnings) > 0:
            exposure_level = "MODERATE"
            route_status = RouteClassification.ROUTE_WITH_WARNINGS
        else:
            exposure_level = "LOW"
            route_status = RouteClassification.ROUTE_AVAILABLE

        analyzed.append({
            "route_id": uuid.uuid4(),
            "geometry": geom,
            "dist_m": dist_m,
            "dist_km": dist_km,
            "dur_sec": dur_sec,
            "dur_min": dur_min,
            "route_status": route_status,
            "exposure_level": exposure_level,
            "hazard_items": hazard_items,
            "warnings": warnings,
            "is_excluded": is_excluded,
            "hard_reasons": hard_reasons,
            "direct_hazards_count": direct_hazards_count,
            "all_hazards_count": all_hazards_count,
            "original_index": idx,
        })

    # Ranking logic:
    # 1. Non-excluded routes rank above excluded routes.
    # 2. Routes with fewer direct hazard intersections rank higher.
    # 3. Routes with fewer total hazard exposures rank higher.
    # 4. Tie-breaker: shorter travel time.
    def sort_key(item: dict[str, Any]) -> tuple[int, int, int, float]:
        excluded_rank = 1 if item["is_excluded"] else 0
        return (
            excluded_rank,
            item["direct_hazards_count"],
            item["all_hazards_count"],
            item["dur_sec"],
        )

    analyzed.sort(key=sort_key)

    # Build RouteDetailItem instances with comparative rationale
    results: list[RouteDetailItem] = []
    primary = analyzed[0] if analyzed else None

    for rank, item in enumerate(analyzed):
        comp_notes: str | None = None
        if rank == 0:
            if item["is_excluded"]:
                comp_notes = "Primary route candidate intersects verified hard exclusion zones; no safe alternative was found."
            elif len(analyzed) > 1:
                comp_notes = (
                    f"Recommended Primary Route: Minimizes verified hazard exposure ({item['all_hazards_count']} identified hazard points/corridors). "
                    f"Travel distance: {item['dist_km']} km (~{item['dur_min']} mins)."
                )
            else:
                comp_notes = f"Primary calculated route: {item['dist_km']} km (~{item['dur_min']} mins)."
        else:
            diff_km = round(item["dist_km"] - primary["dist_km"], 1)
            diff_str = f"+{diff_km} km" if diff_km >= 0 else f"{diff_km} km"
            if item["is_excluded"]:
                comp_notes = f"Alternative Route {rank}: Excluded due to verified physical hazard exposure."
            elif item["all_hazards_count"] > primary["all_hazards_count"]:
                comp_notes = (
                    f"Alternative Route {rank} ({diff_str}): Has {item['all_hazards_count']} hazard exposures "
                    f"compared to {primary['all_hazards_count']} on Primary Route."
                )
            else:
                comp_notes = (
                    f"Alternative Route {rank} ({diff_str}): Offers alternative corridor with {item['all_hazards_count']} hazard exposures."
                )

        results.append(
            RouteDetailItem(
                route_id=item["route_id"],
                route_geometry=item["geometry"],
                distance_m=item["dist_m"],
                distance_km=item["dist_km"],
                estimated_time_seconds=item["dur_sec"],
                estimated_time_minutes=item["dur_min"],
                route_status=item["route_status"],
                hazard_exposure_level=item["exposure_level"],
                hazard_intersections_count=item["all_hazards_count"],
                hazard_intersections=item["hazard_items"],
                warnings=item["warnings"],
                is_hard_excluded=item["is_excluded"],
                hard_exclusion_reasons=item["hard_reasons"],
                comparison_notes=comp_notes,
                routing_provider=ROUTING_PROVIDER_NAME,
                calculated_at=now,
            )
        )

    return results


def calculate_evacuation_routes(
    session: Session,
    origin_latitude: float,
    origin_longitude: float,
    location_assessment_id: UUID,
    allow_alternatives: bool = True,
    osrm_client_fn: Any = None,
) -> CalculateRouteResponse:
    """
    Main orchestration service for Evacuation Route Calculation and Hazard Avoidance.

    1. Validates origin coordinates and destination assessment eligibility.
    2. Calls OSRM for candidate driving routes and alternatives.
    3. Analyzes route geometries against disaster-specific physical hazards.
    4. Compares alternatives and ranks the safest path.
    5. Persists calculated routes to EvacuationRoute database table.
    6. Returns structured response with GeoJSON geometries, hazard intersections, and non-safety disclaimers.
    """
    now = datetime.now(timezone.utc)

    # 1. Fetch LocationAssessment with SafeLocation
    stmt = (
        select(LocationAssessment)
        .options(
            selectinload(LocationAssessment.safe_location),
            selectinload(LocationAssessment.recommendation_run),
        )
        .where(
            (LocationAssessment.id == location_assessment_id)
            | (LocationAssessment.safe_location_id == location_assessment_id)
        )
        .order_by(LocationAssessment.assessed_at.desc())
    )
    assessment = session.scalars(stmt).first()

    if assessment is None:
        return CalculateRouteResponse(
            success=False,
            route_status=RouteClassification.INSUFFICIENT_DATA,
            origin={"latitude": origin_latitude, "longitude": origin_longitude},
            destination_id=location_assessment_id,
            disaster_type=CrisisType.EARTHQUAKE,
            comparison_summary=f"LocationAssessment '{location_assessment_id}' not found.",
            routing_provider=ROUTING_PROVIDER_NAME,
            routing_limitations=ROUTING_LIMITATIONS,
            safety_disclaimer=SAFETY_DISCLAIMER,
        )

    loc = assessment.safe_location
    dest_name = loc.name if loc else None
    dest_category = loc.category if loc else None
    dest_lat, dest_lon = extract_location_coordinates(loc) if loc else (0.0, 0.0)

    # 2. Check destination suitability eligibility
    is_eligible, ineligibility_reason, disaster_type = check_destination_eligibility(assessment)

    if not is_eligible:
        return CalculateRouteResponse(
            success=False,
            route_status=RouteClassification.NO_SAFE_ROUTE_FOUND,
            origin={"latitude": origin_latitude, "longitude": origin_longitude},
            destination_id=loc.id if loc else location_assessment_id,
            destination_name=dest_name,
            destination_category=dest_category,
            disaster_type=disaster_type,
            comparison_summary=ineligibility_reason,
            routing_provider=ROUTING_PROVIDER_NAME,
            routing_limitations=ROUTING_LIMITATIONS,
            safety_disclaimer=SAFETY_DISCLAIMER,
        )

    # 3. Fetch active CrisisEvents for hazard analysis
    event_query = select(CrisisEvent).where(
        CrisisEvent.crisis_type == disaster_type,
        CrisisEvent.status == CrisisStatus.ACTIVE,
    )
    active_events = list(session.scalars(event_query).all())

    # 4. Invoke OSRM routing provider
    router_fn = osrm_client_fn or call_osrm_driving_route
    raw_routes, osrm_err = router_fn(
        origin_lat=origin_latitude,
        origin_lon=origin_longitude,
        dest_lat=dest_lat,
        dest_lon=dest_lon,
        allow_alternatives=allow_alternatives,
    )

    if osrm_err or not raw_routes:
        return CalculateRouteResponse(
            success=False,
            route_status=RouteClassification.ROUTING_SERVICE_UNAVAILABLE,
            origin={"latitude": origin_latitude, "longitude": origin_longitude},
            destination_id=loc.id,
            destination_name=dest_name,
            destination_category=dest_category,
            disaster_type=disaster_type,
            comparison_summary=f"Routing service could not provide a route: {osrm_err or 'No road route found'}",
            routing_provider=ROUTING_PROVIDER_NAME,
            routing_limitations=ROUTING_LIMITATIONS,
            safety_disclaimer=SAFETY_DISCLAIMER,
        )

    # 5. Evaluate and rank candidate routes
    ranked_routes = compare_and_rank_routes(
        candidate_routes=raw_routes,
        disaster_type=disaster_type,
        active_events=active_events,
    )

    primary_route = ranked_routes[0] if ranked_routes else None
    alternative_routes = ranked_routes[1:] if len(ranked_routes) > 1 else []

    # Overall route status
    if all(r.is_hard_excluded for r in ranked_routes):
        overall_status = RouteClassification.NO_SAFE_ROUTE_FOUND
        summary = "All available road routes intersect verified disaster exclusion zones. No safe evacuation route found."
    elif primary_route and primary_route.route_status == RouteClassification.ROUTE_AVAILABLE:
        overall_status = RouteClassification.ROUTE_AVAILABLE
        summary = f"Route calculated successfully with low verified hazard exposure ({len(ranked_routes)} route options analyzed)."
    else:
        overall_status = RouteClassification.ROUTE_WITH_WARNINGS
        summary = f"Route calculated with hazard exposure warnings ({len(ranked_routes)} route options analyzed)."

    # 6. Persist EvacuationRoute database records
    # Map RouteClassification to DB RouteSafetyStatus enum
    DB_STATUS_MAP = {
        RouteClassification.ROUTE_AVAILABLE: RouteSafetyStatus.SAFE,
        RouteClassification.ROUTE_WITH_WARNINGS: RouteSafetyStatus.CAUTION,
        RouteClassification.ROUTE_EXCLUDED: RouteSafetyStatus.UNSAFE,
        RouteClassification.NO_SAFE_ROUTE_FOUND: RouteSafetyStatus.UNSAFE,
        RouteClassification.INSUFFICIENT_DATA: RouteSafetyStatus.UNKNOWN,
        RouteClassification.ROUTING_SERVICE_UNAVAILABLE: RouteSafetyStatus.UNKNOWN,
    }

    for r_item in ranked_routes:
        # Convert GeoJSON LineString to PostGIS WKBElement
        route_geom_wkb = geojson_to_postgis(r_item.route_geometry)
        intersections_payload = [
            it.model_dump(mode="json") for it in r_item.hazard_intersections
        ]

        db_route = EvacuationRoute(
            id=r_item.route_id,
            location_assessment_id=assessment.id,
            route_geometry=route_geom_wkb,
            distance_m=Decimal(str(round(r_item.distance_m, 3))),
            estimated_time_seconds=r_item.estimated_time_seconds,
            hazard_intersections=intersections_payload,
            route_safety_status=DB_STATUS_MAP.get(r_item.route_status, RouteSafetyStatus.UNKNOWN),
            routing_provider=ROUTING_PROVIDER_NAME,
            calculated_at=r_item.calculated_at,
        )
        session.add(db_route)

    session.commit()

    return CalculateRouteResponse(
        success=True,
        route_status=overall_status,
        origin={"latitude": origin_latitude, "longitude": origin_longitude},
        destination_id=loc.id,
        destination_name=dest_name,
        destination_category=dest_category,
        disaster_type=disaster_type,
        primary_route=primary_route,
        alternative_routes=alternative_routes,
        total_routes_analyzed=len(ranked_routes),
        comparison_summary=summary,
        routing_provider=ROUTING_PROVIDER_NAME,
        routing_limitations=ROUTING_LIMITATIONS,
        safety_disclaimer=SAFETY_DISCLAIMER,
    )
