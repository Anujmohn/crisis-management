from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.models.enums import CrisisType, VerificationStatus
from app.schemas.disaster_search import UserLocation
from app.schemas.safe_location import (
    NearbySafeLocationsResponse,
    SafeLocationDetailResponse,
    SafeLocationSyncResponse,
)
from app.schemas.safe_place import (
    ElevatedPlaceSummaryItem,
    ElevatedPlacesDiscoveryResponse,
    ElevationTransectSummary,
    FindSafePlacesRequest,
    FindSafePlacesResponse,
    FloodElevationAnalysisResponse,
    TransectNodeItem,
)
from app.services.city_elevation_data import (
    find_nearest_city_elevation,
    get_city_height_from_sea_level,
)
from app.services.elevated_place_engine import find_elevated_places_near_user
from app.services.elevation_transect_engine import (
    compute_elevation_between_crisis_and_user,
)
from app.services.safe_location import (
    get_all_safe_locations,
    get_nearby_safe_locations,
    get_safe_location_by_id,
)
from app.services.safe_location_ingestion import sync_all_safe_location_sources
from app.services.safe_place_engine import find_safe_places_near_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/safe-locations", tags=["safe-locations"])


@router.get(
    "/nearby",
    response_model=NearbySafeLocationsResponse,
    summary="Retrieve nearby safe locations and candidate evacuation destinations",
    description=(
        "Retrieves geographically referenced shelters, candidate assembly areas, and public facilities "
        "near coordinates using PostGIS spatial indexing and distance ordering. "
        "Distinguishes officially verified emergency shelters from unverified candidate facilities and open areas. "
        "Does not label unverified candidate locations as safe destinations."
    ),
)
def get_nearby_locations(
    latitude: Annotated[
        float,
        Query(
            ge=-90.0,
            le=90.0,
            description="User latitude in WGS84 coordinates (-90.0 to 90.0)",
        ),
    ],
    longitude: Annotated[
        float,
        Query(
            ge=-180.0,
            le=180.0,
            description="User longitude in WGS84 coordinates (-180.0 to 180.0)",
        ),
    ],
    radius_km: Annotated[
        float,
        Query(
            gt=0.0,
            le=500.0,
            description="Search radius in kilometers (0.1 to 500.0 km, default 25.0)",
        ),
    ] = 25.0,
    category: Annotated[
        str | None,
        Query(
            description=(
                "Optional category filter (e.g. OFFICIAL_CYCLONE_SHELTER, "
                "OFFICIAL_FLOOD_SHELTER, COMMUNITY_CENTRE, SCHOOL, HOSPITAL, PARK, OPEN_GROUND)"
            ),
        ),
    ] = None,
    verification_status: Annotated[
        VerificationStatus | None,
        Query(
            description="Optional verification status filter (VERIFIED, UNVERIFIED, REJECTED)",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=200,
            description="Maximum number of results to return (1 to 200, default 50)",
        ),
    ] = 50,
    session: Session = Depends(get_db),
) -> NearbySafeLocationsResponse:
    """
    Evaluates and returns nearby safe locations and candidate assembly areas.
    """
    return get_nearby_safe_locations(
        session=session,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
        category=category,
        verification_status=verification_status,
        limit=limit,
    )


@router.post(
    "/sync",
    response_model=SafeLocationSyncResponse,
    summary="Manually synchronize safe locations and candidate destinations",
    description=(
        "Manually synchronizes safe locations from official government shelter directories "
        "(e.g. NCRMP / SDMA Multi-Purpose Shelters) and OpenStreetMap candidate facilities. "
        "Maintains idempotency and strict verification status separation."
    ),
)
def sync_safe_locations(
    session: Session = Depends(get_db),
) -> SafeLocationSyncResponse:
    """
    Triggers idempotent ingestion for verified shelter directories and geographical candidate sources.
    """
    return sync_all_safe_location_sources(session=session)


@router.get(
    "/all",
    response_model=NearbySafeLocationsResponse,
    summary="Retrieve all safe locations across India",
    description="Returns all registered safe shelters and candidate assembly areas across India.",
)
def get_all_locations(
    category: Annotated[
        str | None,
        Query(
            description=(
                "Optional category filter (e.g. OFFICIAL_CYCLONE_SHELTER, "
                "OFFICIAL_FLOOD_SHELTER, COMMUNITY_CENTRE, SCHOOL, HOSPITAL, PARK, OPEN_GROUND)"
            ),
        ),
    ] = None,
    verification_status: Annotated[
        VerificationStatus | None,
        Query(
            description="Optional verification status filter (VERIFIED, UNVERIFIED, REJECTED)",
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=500,
            description="Maximum number of results to return (1 to 500, default 200)",
        ),
    ] = 200,
    session: Session = Depends(get_db),
) -> NearbySafeLocationsResponse:
    return get_all_safe_locations(
        session=session,
        category=category,
        verification_status=verification_status,
        limit=limit,
    )


@router.post(
    "/find-safe-places",
    response_model=FindSafePlacesResponse,
    summary="Separate Engine to find safe place near user location (starts at 100m, capped 1-5km)",
    description=(
        "Dedicated separate discovery engine for safe places near the user location. "
        "Starts searching at 100 meters radius from user location. Progressively expands outward "
        "until safe locations meeting the disaster specification are found, strictly under 1 to 5 km. "
        "Calculates complete earthquake fields (distance, area, open places, hilly stone-crushing risk), "
        "flood fields (distance, elevation, safety factor, higher/wider safe buildings, flood flow prediction, transect MSL heights), "
        "and sea typhoon fields (distance, beach vs inland effect, safety factor)."
    ),
)
def find_safe_places_post(
    payload: FindSafePlacesRequest,
    session: Session = Depends(get_db),
) -> FindSafePlacesResponse:
    return find_safe_places_near_user(
        session=session,
        latitude=payload.latitude,
        longitude=payload.longitude,
        disaster_type=payload.disaster_type,
        max_radius_km=payload.max_radius_km,
        limit=payload.limit,
    )


@router.get(
    "/find-safe-places",
    response_model=FindSafePlacesResponse,
    summary="Separate Engine to find safe place near user location via GET query",
)
def find_safe_places_get(
    latitude: Annotated[float, Query(ge=-90.0, le=90.0)],
    longitude: Annotated[float, Query(ge=-180.0, le=180.0)],
    disaster_type: Annotated[CrisisType | None, Query()] = None,
    max_radius_km: Annotated[float, Query(ge=0.5, le=5.0)] = 5.0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    session: Session = Depends(get_db),
) -> FindSafePlacesResponse:
    return find_safe_places_near_user(
        session=session,
        latitude=latitude,
        longitude=longitude,
        disaster_type=disaster_type,
        max_radius_km=max_radius_km,
        limit=limit,
    )


@router.get(
    "/elevation-between-crisis-and-user",
    response_model=ElevationTransectSummary,
    summary="Separate Engine to find elevation between user and crisis starting point",
    description=(
        "Dedicated separate engine to calculate elevation and height from sea level between user and crisis starting point. "
        "Calculates MSL height for all locations in the middle of flood start point and user point using city elevation data. "
        "Determines elevation difference: whichever location has greater value has high elevation."
    ),
)
def get_elevation_between_crisis_and_user(
    crisis_latitude: Annotated[float, Query(ge=-90.0, le=90.0)],
    crisis_longitude: Annotated[float, Query(ge=-180.0, le=180.0)],
    user_latitude: Annotated[float, Query(ge=-90.0, le=90.0)],
    user_longitude: Annotated[float, Query(ge=-180.0, le=180.0)],
    num_middle_samples: Annotated[int, Query(ge=3, le=15)] = 5,
) -> ElevationTransectSummary:
    res = compute_elevation_between_crisis_and_user(
        crisis_lat=crisis_latitude,
        crisis_lon=crisis_longitude,
        user_lat=user_latitude,
        user_lon=user_longitude,
        num_middle_samples=num_middle_samples,
    )
    nodes = [
        TransectNodeItem(
            index=n.index,
            fraction=n.fraction,
            latitude=n.latitude,
            longitude=n.longitude,
            height_from_sea_level_m=n.height_from_sea_level_m,
            distance_from_crisis_km=n.distance_from_crisis_km,
            distance_to_user_km=n.distance_to_user_km,
            nearest_city=n.nearest_city,
            stage_description=n.stage_description,
        )
        for n in res.middle_transect_nodes
    ]
    return ElevationTransectSummary(
        crisis_start_latitude=res.crisis_start_latitude,
        crisis_start_longitude=res.crisis_start_longitude,
        crisis_start_height_msl=res.crisis_start_height_msl,
        crisis_start_nearest_city=res.crisis_start_nearest_city,
        user_latitude=res.user_latitude,
        user_longitude=res.user_longitude,
        user_height_msl=res.user_height_msl,
        user_nearest_city=res.user_nearest_city,
        total_distance_km=res.total_distance_km,
        height_difference_m=res.height_difference_m,
        higher_location_name=res.higher_location_name,
        lower_location_name=res.lower_location_name,
        is_crisis_higher=res.is_crisis_higher,
        is_user_higher=res.is_user_higher,
        flow_direction=res.flow_direction,
        gradient_slope_percent=res.gradient_slope_percent,
        middle_transect_nodes=nodes,
        middle_heights_msl=res.middle_heights_msl,
        min_middle_height_msl=res.min_middle_height_msl,
        max_middle_height_msl=res.max_middle_height_msl,
        avg_middle_height_msl=res.avg_middle_height_msl,
        has_intermediate_ridge=res.has_intermediate_ridge,
        has_intermediate_depression=res.has_intermediate_depression,
        flow_prediction_summary=res.flow_prediction_summary,
        elevation_difference_summary=res.elevation_difference_summary,
    )


@router.get(
    "/elevated-places",
    response_model=ElevatedPlacesDiscoveryResponse,
    summary="Separate Engine for finding elevated places (open field, flyover) with more height than user location",
    description=(
        "Discovers elevated places (open fields, flyovers, safe buildings) having more height from sea level "
        "than the user location. Starts search at 100 meters (0.1 km); if none found, progressively increases radius."
    ),
)
def get_elevated_places(
    latitude: Annotated[float, Query(ge=-90.0, le=90.0)],
    longitude: Annotated[float, Query(ge=-180.0, le=180.0)],
    max_radius_km: Annotated[float, Query(ge=0.5, le=50.0)] = 5.0,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    session: Session = Depends(get_db),
) -> ElevatedPlacesDiscoveryResponse:
    res = find_elevated_places_near_user(
        session=session,
        latitude=latitude,
        longitude=longitude,
        max_radius_km=max_radius_km,
        limit=limit,
    )
    items = [
        ElevatedPlaceSummaryItem(
            id=p.id,
            name=p.name,
            category=p.category,
            place_type=p.place_type,
            latitude=p.latitude,
            longitude=p.longitude,
            distance_m=p.distance_m,
            distance_km=p.distance_km,
            elevation_msl_m=p.elevation_msl_m,
            elevation_gain_above_user_m=p.elevation_gain_above_user_m,
            nearest_city=p.nearest_city,
            elevation_comparison=p.elevation_comparison,
            location_geojson=p.location_geojson,
            verification_status=p.verification_status,
        )
        for p in res.elevated_places
    ]
    return ElevatedPlacesDiscoveryResponse(
        success=True,
        user_location=UserLocation(latitude=latitude, longitude=longitude),
        user_height_msl=res.user_height_msl,
        user_nearest_city=res.user_nearest_city,
        initial_radius_km=res.initial_radius_km,
        effective_radius_km=res.effective_radius_km,
        total_found=res.total_found,
        elevated_places=items,
    )


@router.get(
    "/flood-elevation-analysis",
    response_model=FloodElevationAnalysisResponse,
    summary="Combined flood flow and safe locations analysis",
    description=(
        "Combines elevation transect between crisis starting point and user location, "
        "predicted flood flow path GeoJSON, and elevated safe locations (open fields, flyovers) "
        "having more height than user location for direct visualization on the map."
    ),
)
def get_flood_elevation_analysis(
    user_latitude: Annotated[float, Query(ge=-90.0, le=90.0)],
    user_longitude: Annotated[float, Query(ge=-180.0, le=180.0)],
    crisis_latitude: Annotated[float | None, Query(ge=-90.0, le=90.0)] = None,
    crisis_longitude: Annotated[float | None, Query(ge=-180.0, le=180.0)] = None,
    crisis_event_id: Annotated[uuid.UUID | None, Query()] = None,
    max_radius_km: Annotated[float, Query(ge=0.5, le=50.0)] = 5.0,
    session: Session = Depends(get_db),
) -> FloodElevationAnalysisResponse:
    c_lat, c_lon = crisis_latitude, crisis_longitude
    if (c_lat is None or c_lon is None) and crisis_event_id is not None:
        from app.models.core import CrisisEvent
        from app.services.recommendation_engine import safe_postgis_to_geojson
        ev = session.get(CrisisEvent, crisis_event_id)
        if ev and ev.location:
            geo = safe_postgis_to_geojson(ev.location)
            if geo and "coordinates" in geo:
                c_lon, c_lat = float(geo["coordinates"][0]), float(geo["coordinates"][1])

    if c_lat is None or c_lon is None:
        from app.models.core import CrisisEvent
        from geoalchemy2 import Geography
        from sqlalchemy import func, select
        u_geog = func.ST_SetSRID(func.ST_MakePoint(user_longitude, user_latitude), 4326).cast(Geography)
        nearby_ev = session.scalars(
            select(CrisisEvent)
            .where(CrisisEvent.crisis_type == CrisisType.FLOOD)
            .order_by(func.ST_Distance(CrisisEvent.location.cast(Geography), u_geog).asc())
            .limit(1)
        ).first()
        if nearby_ev and nearby_ev.location:
            from app.services.recommendation_engine import safe_postgis_to_geojson
            geo = safe_postgis_to_geojson(nearby_ev.location)
            if geo and "coordinates" in geo:
                c_lon, c_lat = float(geo["coordinates"][0]), float(geo["coordinates"][1])

    if c_lat is None or c_lon is None:
        c_lat = user_latitude + 0.12
        c_lon = user_longitude - 0.10

    transect_res = compute_elevation_between_crisis_and_user(
        crisis_lat=c_lat,
        crisis_lon=c_lon,
        user_lat=user_latitude,
        user_lon=user_longitude,
    )
    nodes = [
        TransectNodeItem(
            index=n.index,
            fraction=n.fraction,
            latitude=n.latitude,
            longitude=n.longitude,
            height_from_sea_level_m=n.height_from_sea_level_m,
            distance_from_crisis_km=n.distance_from_crisis_km,
            distance_to_user_km=n.distance_to_user_km,
            nearest_city=n.nearest_city,
            stage_description=n.stage_description,
        )
        for n in transect_res.middle_transect_nodes
    ]
    transect_summary = ElevationTransectSummary(
        crisis_start_latitude=transect_res.crisis_start_latitude,
        crisis_start_longitude=transect_res.crisis_start_longitude,
        crisis_start_height_msl=transect_res.crisis_start_height_msl,
        crisis_start_nearest_city=transect_res.crisis_start_nearest_city,
        user_latitude=transect_res.user_latitude,
        user_longitude=transect_res.user_longitude,
        user_height_msl=transect_res.user_height_msl,
        user_nearest_city=transect_res.user_nearest_city,
        total_distance_km=transect_res.total_distance_km,
        height_difference_m=transect_res.height_difference_m,
        higher_location_name=transect_res.higher_location_name,
        lower_location_name=transect_res.lower_location_name,
        is_crisis_higher=transect_res.is_crisis_higher,
        is_user_higher=transect_res.is_user_higher,
        flow_direction=transect_res.flow_direction,
        gradient_slope_percent=transect_res.gradient_slope_percent,
        middle_transect_nodes=nodes,
        middle_heights_msl=transect_res.middle_heights_msl,
        min_middle_height_msl=transect_res.min_middle_height_msl,
        max_middle_height_msl=transect_res.max_middle_height_msl,
        avg_middle_height_msl=transect_res.avg_middle_height_msl,
        has_intermediate_ridge=transect_res.has_intermediate_ridge,
        has_intermediate_depression=transect_res.has_intermediate_depression,
        flow_prediction_summary=transect_res.flow_prediction_summary,
        elevation_difference_summary=transect_res.elevation_difference_summary,
    )

    elevated_res = find_elevated_places_near_user(
        session=session,
        latitude=user_latitude,
        longitude=user_longitude,
        max_radius_km=max_radius_km,
    )
    places = [
        ElevatedPlaceSummaryItem(
            id=p.id,
            name=p.name,
            category=p.category,
            place_type=p.place_type,
            latitude=p.latitude,
            longitude=p.longitude,
            distance_m=p.distance_m,
            distance_km=p.distance_km,
            elevation_msl_m=p.elevation_msl_m,
            elevation_gain_above_user_m=p.elevation_gain_above_user_m,
            nearest_city=p.nearest_city,
            elevation_comparison=p.elevation_comparison,
            location_geojson=p.location_geojson,
            verification_status=p.verification_status,
        )
        for p in elevated_res.elevated_places
    ]

    combined_summary = (
        f"{transect_res.flow_prediction_summary} Discovered {len(places)} elevated safe refuge locations "
        f"(flyovers, elevated open fields, high buildings) starting from 100m within {elevated_res.effective_radius_km:.2f} km, "
        f"providing up to +{places[0].elevation_gain_above_user_m:.1f} m vertical clearance above user ground."
        if places
        else f"{transect_res.flow_prediction_summary} Expanding radius to locate elevated ground above user elevation."
    )

    return FloodElevationAnalysisResponse(
        success=True,
        user_location=UserLocation(latitude=user_latitude, longitude=user_longitude),
        user_height_msl=transect_res.user_height_msl,
        user_nearest_city=transect_res.user_nearest_city,
        crisis_start_point={
            "latitude": c_lat,
            "longitude": c_lon,
            "height_msl": transect_res.crisis_start_height_msl,
            "nearest_city": transect_res.crisis_start_nearest_city,
        },
        elevation_transect=transect_summary,
        elevated_safe_places=places,
        flood_flow_path_geojson=transect_res.flood_flow_path_geojson,
        combined_safety_summary=combined_summary,
    )


@router.get(
    "/{location_id}",
    response_model=SafeLocationDetailResponse,
    summary="Retrieve individual safe location details and verification evidence",
    description=(
        "Returns comprehensive location details, all attached source references, "
        "verification evidence, structural / capacity records, and disaster suitability limitations."
    ),
)
def get_location_details(
    location_id: uuid.UUID,
    session: Session = Depends(get_db),
) -> SafeLocationDetailResponse:
    """
    Fetches full metadata and verification evidence for a specific location.
    """
    detail = get_safe_location_by_id(session=session, location_id=location_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Safe location with ID {location_id} not found.",
        )
    return detail
