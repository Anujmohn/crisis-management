from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.core import CrisisEvent, CrisisSourceReport
from app.models.enums import (
    CrisisStatus,
    CrisisType,
    DesignationStatus,
    VerificationStatus,
)
from app.models.recommendations import LocationAssessment, SafeLocation
from app.schemas.disaster_search import (
    DisasterEventSummary,
    DisasterObservationSummary,
    DisasterSourceSummary,
    UserLocation,
)
from app.schemas.recommendation import SuitabilityClassification
from app.schemas.route import RouteClassification
from app.schemas.unified_recommendation import (
    DisasterHazardAssessmentSummary,
    RecommendedDestinationItem,
    UnifiedRecommendationResponse,
    UnifiedRecommendationStatus,
)
from app.services.cyclone_assessment import assess_cyclone_hazard
from app.services.disaster_search import (
    evaluate_freshness,
    get_nearby_disasters,
    safe_postgis_to_geojson,
)
from app.services.earthquake_assessment import assess_earthquake_hazard
from app.services.evacuation_routing import (
    calculate_evacuation_routes,
    extract_location_coordinates,
)
from app.services.flood_assessment import assess_flood_hazard
from app.services.recommendation_engine import assess_safe_locations_suitability

logger = logging.getLogger(__name__)

UNIFIED_SAFETY_DISCLAIMER = (
    "Unified recommendations and evacuation routes are evidence-based relative rankings based on available "
    "official hazard feeds and do NOT guarantee safety, structural integrity, open roads, or survival. "
    "Real-time conditions on the ground must always be confirmed. Never cross flooded roads or enter "
    "structurally damaged buildings. Follow official instructions from NDMA, SDMA, and local emergency authorities."
)

METHODOLOGY_NOTES = [
    "Evaluates active physical disaster hazards using official scientific feeds (NCS for Earthquakes, CWC/IMD for Floods, IMD/INCOIS for Cyclones).",
    "Candidate destinations are discovered via PostGIS spatial distance queries and vetted against disaster-specific physical requirements.",
    "Hard-excluded destinations (inundated facilities, coastal storm-surge exposure zones) are strictly excluded from recommendation lists.",
    "Evacuation road routes are computed via OSRM and analyzed for spatial intersection with reported disaster hazard zones.",
    "Rankings prioritize verified suitability status, clear road routes without hazard intersections, and official shelter designation ahead of distance or travel time.",
]


def _build_event_summary(event: CrisisEvent, now: datetime) -> DisasterEventSummary:
    """Builds a DisasterEventSummary from a CrisisEvent entity."""
    freshness = evaluate_freshness(event, now)
    sources = [
        DisasterSourceSummary(
            source_name=sr.data_source.name if sr.data_source else "Official Source",
            source_type=sr.data_source.source_type if sr.data_source else "UNKNOWN",
            source_url=sr.source_url,
            external_event_id=sr.external_event_id,
            issued_at=sr.issued_at,
            fetched_at=sr.fetched_at,
            valid_until=sr.valid_until,
        )
        for sr in (event.source_reports or [])
    ]
    observations = [
        DisasterObservationSummary(
            id=obs.id,
            observation_type=obs.observation_type,
            value_numeric=obs.value_numeric,
            value_text=obs.value_text,
            unit=obs.unit,
            geometry=safe_postgis_to_geojson(obs.geometry),
            observed_at=obs.observed_at,
            properties=obs.properties,
        )
        for obs in (event.hazard_observations or [])
    ]
    return DisasterEventSummary(
        id=event.id,
        disaster_type=event.crisis_type,
        title=event.title,
        description=event.description,
        severity=event.severity,
        status=event.status,
        location=safe_postgis_to_geojson(event.location),
        affected_area=safe_postgis_to_geojson(event.affected_area),
        distance_km=None,
        started_at=event.started_at,
        expires_at=event.expires_at,
        first_seen_at=event.first_seen_at,
        last_seen_at=event.last_seen_at,
        freshness=freshness,
        sources=sources,
        observations=observations,
    )


def _extract_str(val: Any, default: str) -> str:
    """Safely extracts a string from an attribute, enum value, or mock."""
    if isinstance(val, str):
        return val
    if hasattr(val, "value") and isinstance(val.value, str):
        return val.value
    return default


def _extract_str_list(val: Any) -> list[str]:
    """Safely extracts a list of strings from an attribute or mock."""
    if isinstance(val, (list, tuple)):
        items = []
        for item in val:
            if isinstance(item, str):
                items.append(item)
            elif hasattr(item, "value") and isinstance(item.value, str):
                items.append(item.value)
            elif isinstance(item, (int, float)):
                items.append(str(item))
        return items
    return []


def generate_unified_recommendations(
    session: Session,
    latitude: float,
    longitude: float,
    disaster_type: CrisisType,
    crisis_event_id: UUID | None = None,
    search_radius_km: float = 25.0,
    max_destinations: int = 10,
    osrm_client_fn: Any = None,
) -> UnifiedRecommendationResponse:
    """
    Main orchestration engine for Unified Disaster Recommendations.

    Integrates:
    1. Nearby disaster context verification & event retrieval.
    2. Disaster-specific geographical hazard assessment (Earthquake, Flood, Cyclone).
    3. Safe-location discovery & disaster-specific suitability evaluation.
    4. Hard exclusions & filtering.
    5. Evacuation route calculation & hazard avoidance.
    6. Transparent multi-criteria comparative ranking and overall status assignment.
    """
    now = datetime.now(timezone.utc)
    user_location = UserLocation(latitude=latitude, longitude=longitude)

    # ---------------------------------------------------------
    # Step 1: Input Validation & Disaster Context Verification
    # ---------------------------------------------------------
    target_event: CrisisEvent | None = None
    target_event_summary: DisasterEventSummary | None = None

    if crisis_event_id is not None:
        event_query = (
            select(CrisisEvent)
            .options(
                selectinload(CrisisEvent.source_reports).selectinload(CrisisSourceReport.data_source),
                selectinload(CrisisEvent.hazard_observations),
            )
            .where(CrisisEvent.id == crisis_event_id)
        )
        target_event = session.scalars(event_query).first()

        if target_event is None or target_event.crisis_type != disaster_type:
            logger.warning(
                "Unified recommendations failed: crisis_event_id %s not found or type mismatch (expected %s)",
                crisis_event_id,
                disaster_type.value,
            )
            return UnifiedRecommendationResponse(
                success=False,
                status=UnifiedRecommendationStatus.DISASTER_CONTEXT_UNAVAILABLE,
                user_location=user_location,
                disaster_type=disaster_type,
                crisis_event=None,
                hazard_assessment=DisasterHazardAssessmentSummary(
                    disaster_type=disaster_type,
                    status="CONTEXT_UNAVAILABLE",
                    summary=(
                        f"The requested disaster event '{crisis_event_id}' could not be verified "
                        f"or does not match the specified disaster type '{disaster_type.value}'."
                    ),
                    data_freshness="UNKNOWN",
                    key_hazards=[],
                    warnings=["Disaster context verification failed."],
                    limitations=["Recommendations cannot proceed without a valid active disaster context."],
                ),
                total_candidates_found=0,
                total_suitable_destinations=0,
                total_routed_destinations=0,
                recommendations=[],
                excluded_destinations_count=0,
                methodology_and_ranking_notes=METHODOLOGY_NOTES,
                overall_uncertainties=["Active crisis event could not be verified in official database."],
                safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
            )

        target_event_summary = _build_event_summary(target_event, now)
    else:
        # Auto-detect nearby active disaster matching the disaster type
        nearby_resp = get_nearby_disasters(
            session=session,
            latitude=latitude,
            longitude=longitude,
            radius_km=max(search_radius_km, 100.0),
            disaster_types=[disaster_type],
            statuses=[CrisisStatus.ACTIVE],
            limit=5,
        )
        if nearby_resp.events:
            target_event_summary = nearby_resp.events[0]
            # Fetch entity
            target_event = session.get(CrisisEvent, target_event_summary.id)

    # ---------------------------------------------------------
    # Step 2: Disaster-Specific Geographical Hazard Assessment
    # ---------------------------------------------------------
    hazard_summary: DisasterHazardAssessmentSummary
    overall_uncertainties: list[str] = []

    try:
        if disaster_type == CrisisType.EARTHQUAKE:
            eq_resp = assess_earthquake_hazard(
                session=session,
                latitude=latitude,
                longitude=longitude,
                search_radius_km=max(search_radius_km, 300.0),
                lookback_days=30,
            )
            raw_eq_list = getattr(eq_resp, "earthquakes", None) or getattr(eq_resp, "relevant_events", [])
            eq_list = raw_eq_list if isinstance(raw_eq_list, (list, tuple)) else []
            key_hazards = []
            for eq in eq_list[:3]:
                mag = getattr(eq, "magnitude", None)
                loc = getattr(eq, "location_name", None) or getattr(eq, "title", "unnamed")
                dist = getattr(eq, "distance_km", None) or getattr(eq, "epicentral_distance_km", 0.0)
                dep = getattr(eq, "depth_km", "unknown")
                dist_str = f"{dist:.1f} km" if isinstance(dist, (int, float)) else "unknown distance"
                key_hazards.append(f"M{mag} earthquake at {loc} ({dist_str}, depth {dep} km)")

            st_raw = getattr(eq_resp, "status", None) or getattr(eq_resp, "assessment_status", None)
            st_str = _extract_str(st_raw, "POTENTIAL_RELEVANT_EVENT")
            sum_text = _extract_str(
                getattr(eq_resp, "assessment_summary", None) or getattr(eq_resp, "summary_message", None),
                "Earthquake assessment evaluated."
            )
            raw_freshness = getattr(eq_resp, "data_freshness", None)
            if not isinstance(raw_freshness, str) and not (hasattr(raw_freshness, "value") and isinstance(raw_freshness.value, str)):
                raw_freshness = eq_list[0].freshness if eq_list and hasattr(eq_list[0], "freshness") else "FRESH"
            freshness = _extract_str(raw_freshness, "FRESH")
            warns = _extract_str_list(
                getattr(eq_resp, "contributing_factors", None) or getattr(eq_resp, "missing_data_warnings", None)
            )
            lims = _extract_str_list(getattr(eq_resp, "scientific_limitations", None))

            hazard_summary = DisasterHazardAssessmentSummary(
                disaster_type=disaster_type,
                status=st_str,
                summary=sum_text,
                data_freshness=freshness,
                key_hazards=key_hazards,
                warnings=warns,
                limitations=lims,
            )
            overall_uncertainties.extend(lims)

        elif disaster_type == CrisisType.FLOOD:
            fl_resp = assess_flood_hazard(
                session=session,
                latitude=latitude,
                longitude=longitude,
                search_radius_km=search_radius_km,
                lookback_days=7,
            )
            raw_ob_list = getattr(fl_resp, "observed_flooding", [])
            ob_list = raw_ob_list if isinstance(raw_ob_list, (list, tuple)) else []
            key_hazards = []
            for ob in ob_list[:2]:
                ob_dist = getattr(ob, "distance_km", 0.0)
                ob_depth = getattr(ob, "inundation_depth_meters", None) or "unknown"
                dist_str = f"{ob_dist:.1f} km" if isinstance(ob_dist, (int, float)) else "nearby"
                key_hazards.append(f"Observed flood {dist_str} ({ob_depth} m depth)")
            raw_w_list = getattr(fl_resp, "official_warnings", [])
            w_list = raw_w_list if isinstance(raw_w_list, (list, tuple)) else []
            for w in w_list[:2]:
                w_head = getattr(w, "headline", None) or getattr(w, "warning_level", "Warning")
                w_src = getattr(w, "source_name", "Official")
                key_hazards.append(f"Warning: {w_head} ({w_src})")
            raw_r_list = getattr(fl_resp, "river_observations", [])
            r_list = raw_r_list if isinstance(raw_r_list, (list, tuple)) else []
            for r in r_list[:2]:
                r_name = getattr(r, "river_name", None) or getattr(r, "station_name", "River")
                r_st = getattr(r, "level_status", None)
                r_st_str = r_st.value if hasattr(r_st, "value") else str(r_st)
                r_depth = getattr(r, "water_level_meters", "unknown")
                key_hazards.append(f"River {r_name}: {r_st_str} ({r_depth} m)")

            st_raw = getattr(fl_resp, "status", None) or getattr(fl_resp, "assessment_status", None)
            st_str = _extract_str(st_raw, "POTENTIAL_RELEVANT_EVENT")
            sum_text = _extract_str(
                getattr(fl_resp, "assessment_summary", None) or getattr(fl_resp, "summary_message", None),
                "Flood hazard assessment evaluated."
            )
            freshness = _extract_str(getattr(fl_resp, "data_freshness", None), "FRESH")
            warns = _extract_str_list(getattr(fl_resp, "contributing_factors", None))
            lims = _extract_str_list(getattr(fl_resp, "scientific_limitations", None))

            hazard_summary = DisasterHazardAssessmentSummary(
                disaster_type=disaster_type,
                status=st_str,
                summary=sum_text,
                data_freshness=freshness,
                key_hazards=key_hazards,
                warnings=warns,
                limitations=lims,
            )
            overall_uncertainties.extend(lims)

        elif disaster_type == CrisisType.CYCLONE:
            cy_resp = assess_cyclone_hazard(
                session=session,
                latitude=latitude,
                longitude=longitude,
                search_radius_km=max(search_radius_km, 500.0),
                lookback_days=7,
            )
            key_hazards = []
            wind_obj = getattr(cy_resp, "wind_hazard", None)
            wind_cat = _extract_str(getattr(wind_obj, "intensity_category", None), "")
            if wind_cat:
                max_w = getattr(wind_obj, "max_sustained_wind_kmph", "unknown")
                key_hazards.append(f"Wind hazard: {wind_cat} (max wind {max_w} km/h)")

            surge_obj = getattr(cy_resp, "storm_surge_hazard", None) or getattr(cy_resp, "storm_surge", None)
            surge_m = getattr(surge_obj, "storm_surge_meters", None)
            if isinstance(surge_m, (int, float)):
                key_hazards.append(f"Storm surge: {surge_m} m forecast")

            coast_obj = getattr(cy_resp, "coastal_hazard", None) or getattr(cy_resp, "coastal_inundation", None)
            coast_dist = getattr(coast_obj, "distance_to_coastline_km", None)
            if isinstance(coast_dist, (int, float)):
                key_hazards.append(f"Coastline distance: {coast_dist:.1f} km")

            st_raw = getattr(cy_resp, "status", None) or getattr(cy_resp, "primary_status", None)
            st_str = _extract_str(st_raw, "POTENTIAL_RELEVANT_EVENT")
            sum_text = _extract_str(
                getattr(cy_resp, "assessment_summary", None) or getattr(cy_resp, "summary_message", None),
                "Cyclone hazard assessment evaluated."
            )
            freshness = _extract_str(getattr(cy_resp, "data_freshness", None), "FRESH")
            warns = _extract_str_list(getattr(cy_resp, "contributing_factors", None))
            lims = _extract_str_list(getattr(cy_resp, "scientific_limitations", None))

            hazard_summary = DisasterHazardAssessmentSummary(
                disaster_type=disaster_type,
                status=st_str,
                summary=sum_text,
                data_freshness=freshness,
                key_hazards=key_hazards,
                warnings=warns,
                limitations=lims,
            )
            overall_uncertainties.extend(lims)

        else:
            hazard_summary = DisasterHazardAssessmentSummary(
                disaster_type=disaster_type,
                status="UNSUPPORTED",
                summary=f"Disaster type {disaster_type.value} does not have an active hazard assessment engine.",
                data_freshness="UNKNOWN",
                key_hazards=[],
                warnings=[],
                limitations=[],
            )
    except Exception as exc:
        logger.exception("Error executing hazard assessment engine for %s: %s", disaster_type.value, exc)
        return UnifiedRecommendationResponse(
            success=False,
            status=UnifiedRecommendationStatus.ASSESSMENT_FAILED,
            user_location=user_location,
            disaster_type=disaster_type,
            crisis_event=target_event_summary,
            hazard_assessment=DisasterHazardAssessmentSummary(
                disaster_type=disaster_type,
                status="FAILED",
                summary=f"Hazard assessment engine failed: {exc}",
                data_freshness="UNKNOWN",
                key_hazards=[],
                warnings=["Hazard assessment engine encountered an unexpected error."],
                limitations=[],
            ),
            total_candidates_found=0,
            total_suitable_destinations=0,
            total_routed_destinations=0,
            recommendations=[],
            excluded_destinations_count=0,
            methodology_and_ranking_notes=METHODOLOGY_NOTES,
            overall_uncertainties=["Internal assessment exception occurred."],
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

    # ---------------------------------------------------------
    # Step 3: Destination Discovery & Suitability Assessment
    # ---------------------------------------------------------
    try:
        suitability_resp = assess_safe_locations_suitability(
            session=session,
            latitude=latitude,
            longitude=longitude,
            disaster_type=disaster_type,
            crisis_event_id=target_event.id if target_event else None,
            search_radius_km=search_radius_km,
            limit=50,
        )
    except Exception as exc:
        logger.exception("Error executing safe locations suitability assessment: %s", exc)
        return UnifiedRecommendationResponse(
            success=False,
            status=UnifiedRecommendationStatus.ASSESSMENT_FAILED,
            user_location=user_location,
            disaster_type=disaster_type,
            crisis_event=target_event_summary,
            hazard_assessment=hazard_summary,
            total_candidates_found=0,
            total_suitable_destinations=0,
            total_routed_destinations=0,
            recommendations=[],
            excluded_destinations_count=0,
            methodology_and_ranking_notes=METHODOLOGY_NOTES,
            overall_uncertainties=["Failed to evaluate safe location suitability."],
            safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
        )

    all_candidates = suitability_resp.assessed_locations
    total_candidates_found = len(all_candidates)

    # ---------------------------------------------------------
    # Step 4: Candidate Filtering & Hard Exclusions
    # ---------------------------------------------------------
    eligible_candidates = [
        c for c in all_candidates
        if c.suitability_classification in (
            SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE,
            SuitabilityClassification.CONDITIONALLY_SUITABLE,
        )
    ]
    excluded_candidates = [
        c for c in all_candidates
        if c.suitability_classification in (
            SuitabilityClassification.EXCLUDED,
            SuitabilityClassification.NOT_RECOMMENDED,
        )
    ]
    excluded_count = len(excluded_candidates)
    total_suitable = len(eligible_candidates)

    # ---------------------------------------------------------
    # Step 5: Evacuation Route Calculation & Hazard Avoidance
    # ---------------------------------------------------------
    candidate_destinations: list[RecommendedDestinationItem] = []
    
    # Process up to max_destinations eligible candidates
    for cand in eligible_candidates[:max_destinations]:
        # Extract destination coordinates
        dest_coords = {"latitude": 0.0, "longitude": 0.0}
        loc_assessment = session.get(LocationAssessment, cand.assessment_id) if cand.assessment_id else None
        if loc_assessment and isinstance(loc_assessment.factors, dict) and "coordinates" in loc_assessment.factors:
            dest_coords = loc_assessment.factors["coordinates"]
        elif loc_assessment and loc_assessment.safe_location:
            lat_val, lon_val = extract_location_coordinates(loc_assessment.safe_location)
            dest_coords = {"latitude": lat_val, "longitude": lon_val}

        # Calculate evacuation route
        route_resp = calculate_evacuation_routes(
            session=session,
            origin_latitude=latitude,
            origin_longitude=longitude,
            location_assessment_id=cand.assessment_id or cand.location_id,
            allow_alternatives=True,
            osrm_client_fn=osrm_client_fn,
        )

        item = RecommendedDestinationItem(
            destination_id=cand.location_id,
            assessment_id=cand.assessment_id,
            name=cand.name,
            category=cand.category,
            official_designation=cand.official_designation,
            verification_status=cand.verification_status,
            verification_detail=cand.verification_detail,
            coordinates=dest_coords,
            distance_km=cand.distance_km,
            suitability_classification=cand.suitability_classification,
            suitability_score=cand.suitability_score,
            suitability_reasons=cand.supporting_evidence,
            suitability_warnings=cand.warnings,
            route=route_resp.primary_route,
            route_status=route_resp.route_status,
            ranking_score=None,
            ranking_rationale="",
        )
        candidate_destinations.append(item)

    # ---------------------------------------------------------
    # Step 6: Multi-Criteria Comparative Ranking
    # ---------------------------------------------------------
    def ranking_key(item: RecommendedDestinationItem) -> tuple[int, int, int, int, float]:
        # Tier 1: Suitability (Evidence-backed 0 > Conditional 1)
        suit_tier = 0 if item.suitability_classification == SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE else 1

        # Tier 2: Route safety (ROUTE_AVAILABLE 0 > ROUTE_WITH_WARNINGS 1 > UNAVAILABLE 2 > EXCLUDED 3)
        if item.route_status == RouteClassification.ROUTE_AVAILABLE:
            route_tier = 0
        elif item.route_status == RouteClassification.ROUTE_WITH_WARNINGS:
            route_tier = 1
        elif item.route_status == RouteClassification.ROUTING_SERVICE_UNAVAILABLE:
            route_tier = 2
        else:
            route_tier = 3

        # Tier 3: Verification (OFFICIAL/VERIFIED 0 > UNVERIFIED 1)
        verif_tier = 0 if (
            item.verification_status == VerificationStatus.VERIFIED
            or item.official_designation == DesignationStatus.OFFICIAL
        ) else 1

        # Tier 4: Route hazard intersections count (fewer is better)
        hazard_count = item.route.hazard_intersections_count if item.route else 99

        # Tier 5: Travel time / distance (closer/faster only within the same safety tier)
        travel_time = (
            item.route.estimated_time_seconds
            if (item.route and item.route.estimated_time_seconds)
            else (item.distance_km * 120.0)
        )

        return (suit_tier, route_tier, verif_tier, hazard_count, travel_time)

    candidate_destinations.sort(key=ranking_key)

    # Generate ranking scores and transparent justifications
    for idx, dest in enumerate(candidate_destinations, start=1):
        # Normalized score: relative index scale from 1.0 down to 0.1
        dest.ranking_score = round(max(0.1, 1.0 - (idx - 1) * 0.05), 2)

        reasons: list[str] = []
        if dest.official_designation == DesignationStatus.OFFICIAL or dest.verification_status == VerificationStatus.VERIFIED:
            reasons.append("Officially designated/verified facility")
        else:
            reasons.append("Unverified candidate facility")

        if dest.suitability_classification == SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE:
            reasons.append(f"Evidence-backed physical suitability for {disaster_type.value}")
        else:
            reasons.append(f"Conditionally suitable for {disaster_type.value}")

        if dest.route:
            if dest.route_status == RouteClassification.ROUTE_AVAILABLE:
                reasons.append(
                    f"Clear road route (0 hazard intersections, {dest.route.distance_km:.1f} km, ~{dest.route.estimated_time_minutes:.0f} min)"
                )
            elif dest.route_status == RouteClassification.ROUTE_WITH_WARNINGS:
                reasons.append(
                    f"Route has {dest.route.hazard_intersections_count} hazard warnings ({dest.route.distance_km:.1f} km)"
                )
            elif dest.route_status == RouteClassification.ROUTE_EXCLUDED:
                reasons.append(
                    f"Route intersects active disaster exclusion zones ({dest.route.distance_km:.1f} km)"
                )
            else:
                reasons.append(f"Route status: {dest.route_status.value}")
        else:
            reasons.append(f"No usable route ({dest.route_status.value if dest.route_status else 'UNKNOWN'})")

        dest.ranking_rationale = f"Rank {idx}: " + "; ".join(reasons) + "."

    # ---------------------------------------------------------
    # Step 7: Overall Status Determination
    # ---------------------------------------------------------
    usable_destinations = [
        d for d in candidate_destinations
        if d.route_status in (RouteClassification.ROUTE_AVAILABLE, RouteClassification.ROUTE_WITH_WARNINGS)
    ]

    if total_candidates_found == 0:
        overall_status = UnifiedRecommendationStatus.INSUFFICIENT_DATA
    elif total_suitable == 0:
        overall_status = UnifiedRecommendationStatus.NO_SUITABLE_DESTINATION_FOUND
    elif not usable_destinations:
        overall_status = UnifiedRecommendationStatus.NO_ROUTE_AVAILABLE
    elif any(
        d.suitability_classification == SuitabilityClassification.SUITABILITY_SUPPORTED_BY_EVIDENCE
        and d.route_status == RouteClassification.ROUTE_AVAILABLE
        for d in usable_destinations
    ):
        overall_status = UnifiedRecommendationStatus.RECOMMENDATIONS_AVAILABLE
    else:
        overall_status = UnifiedRecommendationStatus.LIMITED_RECOMMENDATIONS

    total_routed = sum(1 for d in candidate_destinations if d.route is not None)

    return UnifiedRecommendationResponse(
        success=True,
        status=overall_status,
        user_location=user_location,
        disaster_type=disaster_type,
        crisis_event=target_event_summary,
        hazard_assessment=hazard_summary,
        total_candidates_found=total_candidates_found,
        total_suitable_destinations=total_suitable,
        total_routed_destinations=total_routed,
        recommendations=candidate_destinations,
        excluded_destinations_count=excluded_count,
        methodology_and_ranking_notes=METHODOLOGY_NOTES,
        overall_uncertainties=list(set(overall_uncertainties)),
        safety_disclaimer=UNIFIED_SAFETY_DISCLAIMER,
    )


__all__ = [
    "METHODOLOGY_NOTES",
    "UNIFIED_SAFETY_DISCLAIMER",
    "generate_unified_recommendations",
]
