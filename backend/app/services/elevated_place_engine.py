from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from geoalchemy2 import Geography
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models.enums import DesignationStatus, VerificationStatus
from app.models.recommendations import SafeLocation, SafeLocationSourceReference
from app.services.city_elevation_data import (
    calculate_elevation_difference,
    find_nearest_city_elevation,
    get_city_height_from_sea_level,
    haversine_distance_km,
)
from app.services.recommendation_engine import (
    extract_location_coordinates,
    safe_postgis_to_geojson,
)
from app.services.safe_location_osm import discover_and_sync_candidate_locations

logger = logging.getLogger(__name__)


@dataclass
class ElevatedPlaceItem:
    """An elevated physical safe place having more height from sea level than the user."""
    id: uuid.UUID
    name: str
    category: str
    place_type: str  # "ELEVATED_FLYOVER", "HIGH_ELEVATION_OPEN_FIELD", "ELEVATED_BUILDING_COMPLEX"
    latitude: float
    longitude: float
    distance_m: float
    distance_km: float
    elevation_msl_m: float
    elevation_gain_above_user_m: float
    nearest_city: str
    elevation_comparison: dict[str, Any]
    location_geojson: dict[str, Any]
    verification_status: str


@dataclass
class ElevatedPlacesResponse:
    """Response returned by the Elevated Place Discovery Engine."""
    user_latitude: float
    user_longitude: float
    user_height_msl: float
    user_nearest_city: str
    initial_radius_km: float
    effective_radius_km: float
    total_found: int
    elevated_places: list[ElevatedPlaceItem]


def is_elevated_place_candidate(loc: SafeLocation) -> tuple[bool, str]:
    """
    Identifies whether a safe location is an elevated place (open field, flyover,
    or elevated building complex).
    Rejects any synthetic/hypothetical placeholder names.
    """
    name_str = (loc.name or "").strip()
    name_lower = name_str.lower()
    if not name_str or len(name_str) < 3:
        return False, "UNKNOWN"

    # Reject synthetic coordinate names
    if "(" in name_lower and ")" in name_lower and any(c.isdigit() for c in name_lower):
        if "refuge zone" in name_lower or "assembly zone" in name_lower:
            return False, "SYNTHETIC"

    cat = (loc.category or "").upper()

    # 1. Flyovers / Elevated decks
    if (
        "flyover" in name_lower
        or "elevated" in name_lower
        or "viaduct" in name_lower
        or "bridge" in name_lower
        or cat in ("FLYOVER", "ELEVATED_STRUCTURE")
    ):
        return True, "ELEVATED_FLYOVER"

    # 2. Open fields / grounds on higher elevation
    if (
        cat in ("OPEN_GROUND", "PARK", "PLAYGROUND", "FIELD", "CAMPUS")
        or any(w in name_lower for w in ["ground", "park", "field", "stadium", "maidan", "turf", "lawn", "open"])
    ):
        return True, "HIGH_ELEVATION_OPEN_FIELD"

    # 3. Elevated Safe Buildings / Government complexes / Hospitals
    if (
        cat in ("HOSPITAL", "SCHOOL", "GOVERNMENT_BUILDING", "COMMUNITY_CENTER", "OFFICIAL_FLOOD_SHELTER")
        or any(w in name_lower for w in ["hospital", "bhawan", "secretariat", "building", "college", "school", "hall", "complex", "centre", "center", "institute", "university"])
    ):
        return True, "ELEVATED_BUILDING_COMPLEX"

    return False, "UNKNOWN"


def find_elevated_places_near_user(
    session: Session,
    latitude: float,
    longitude: float,
    max_radius_km: float = 5.0,
    limit: int = 20,
) -> ElevatedPlacesResponse:
    """
    Separate Engine for finding elevated places (open field, flyover, safe building)
    which have MORE HEIGHT than the user's location.

    Requirements:
    1. Determines user location's height from sea level.
    2. Searches for elevated places (open field, flyover, building) in a radius of 100 meters (0.1 km).
    3. If no elevated place having more height than user location is found within 100m,
       progressively increases the radius until elevated places are found.
    4. Evaluates elevation using height from sea level of all locations:
       Finds the difference between two locations' heights; whichever has the greater value has high elevation.
    5. Candidate MUST have height_from_sea_level > user_height.
    """
    # 1. Determine user elevation and nearest city
    user_height = get_city_height_from_sea_level(latitude, longitude)
    user_city, _, _, _, _ = find_nearest_city_elevation(latitude, longitude)

    user_geog = func.ST_SetSRID(func.ST_MakePoint(longitude, latitude), 4326).cast(Geography)
    dist_m_col = func.ST_Distance(SafeLocation.location.cast(Geography), user_geog).label("distance_m")

    # 2. Ensure candidate locations exist near user coordinates
    search_radius_m = min(15.0, max(1.0, float(max_radius_km))) * 1000.0
    existing_count = session.scalar(
        select(func.count(SafeLocation.id)).where(
            func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, search_radius_m),
            ~SafeLocation.name.ilike("%refuge zone (%%"),
            ~SafeLocation.name.ilike("%assembly zone (%%"),
        )
    )
    try:
        cnt = int(existing_count) if existing_count is not None else 0
    except (TypeError, ValueError):
        cnt = 999

    if cnt < 3:
        try:
            discover_and_sync_candidate_locations(
                session=session,
                latitude=latitude,
                longitude=longitude,
                radius_km=max(5.0, float(max_radius_km)),
            )
        except Exception as exc:
            logger.debug(f"Dynamic candidate discovery note: {exc}")

    # 3. Progressive search starting strictly at 100 meters (0.1 km)
    search_tiers_km = [0.1, 0.25, 0.5, 1.0, 2.0, 3.5, 5.0, 10.0, 25.0, 50.0, 100.0, 500.0]

    found_places: list[ElevatedPlaceItem] = []
    effective_radius_km = 0.1

    for tier_km in search_tiers_km:
        tier_m = tier_km * 1000.0
        effective_radius_km = tier_km

        tier_query = (
            select(SafeLocation, dist_m_col)
            .options(
                selectinload(SafeLocation.source_references).selectinload(SafeLocationSourceReference.data_source)
            )
            .where(
                func.ST_DWithin(SafeLocation.location.cast(Geography), user_geog, tier_m),
                ~SafeLocation.name.ilike("%refuge zone (%%"),
                ~SafeLocation.name.ilike("%assembly zone (%%"),
            )
            .order_by(dist_m_col.asc())
            .limit(limit * 3)
        )
        tier_rows = session.execute(tier_query).all()
        if not tier_rows:
            continue

        matching_tier_items: list[ElevatedPlaceItem] = []

        for row in tier_rows:
            loc: SafeLocation = row[0]
            dist_m_val = float(row[1]) if row[1] is not None else 0.0
            dist_km_val = round(dist_m_val / 1000.0, 3)
            c_lat, c_lon = extract_location_coordinates(loc)

            # Check if this location is an open field, flyover, or elevated complex
            is_cand, place_type = is_elevated_place_candidate(loc)
            if not is_cand:
                continue

            # Compute height from sea level for this candidate
            cand_ground_msl = get_city_height_from_sea_level(c_lat, c_lon)
            cand_nearest_city, _, _, _, _ = find_nearest_city_elevation(c_lat, c_lon)

            # Flyover structural deck adds +6 meters vertical clearance above ground
            cand_final_msl = cand_ground_msl + (6.0 if place_type == "ELEVATED_FLYOVER" else 0.0)

            # Check elevation difference between candidate and user:
            # Candidate MUST have greater value and high elevation than user location!
            diff_eval = calculate_elevation_difference(
                loc1_name=loc.name,
                loc1_height_m=cand_final_msl,
                loc2_name="User Location",
                loc2_height_m=user_height,
            )

            # Must have MORE height than user location (greater value)
            if cand_final_msl <= user_height:
                continue

            elevation_gain_m = round(cand_final_msl - user_height, 1)

            loc_geojson = safe_postgis_to_geojson(loc.location) or {"type": "Point", "coordinates": [c_lon, c_lat]}

            v_status = "OFFICIALLY_VERIFIED" if loc.verification_status == VerificationStatus.VERIFIED else "UNVERIFIED"

            matching_tier_items.append(
                ElevatedPlaceItem(
                    id=loc.id,
                    name=loc.name,
                    category=loc.category,
                    place_type=place_type,
                    latitude=round(c_lat, 5),
                    longitude=round(c_lon, 5),
                    distance_m=round(dist_m_val, 1),
                    distance_km=dist_km_val,
                    elevation_msl_m=round(cand_final_msl, 1),
                    elevation_gain_above_user_m=elevation_gain_m,
                    nearest_city=cand_nearest_city,
                    elevation_comparison=diff_eval,
                    location_geojson=loc_geojson,
                    verification_status=v_status,
                )
            )

        if matching_tier_items:
            # Sort by elevation gain above user (highest first) and proximity
            matching_tier_items.sort(key=lambda x: (x.elevation_gain_above_user_m, -x.distance_km), reverse=True)
            found_places = matching_tier_items[:limit]
            break

    return ElevatedPlacesResponse(
        user_latitude=round(latitude, 5),
        user_longitude=round(longitude, 5),
        user_height_msl=round(user_height, 1),
        user_nearest_city=user_city,
        initial_radius_km=0.1,
        effective_radius_km=round(effective_radius_km, 2),
        total_found=len(found_places),
        elevated_places=found_places,
    )
