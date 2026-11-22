from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.services.city_elevation_data import (
    calculate_elevation_difference,
    find_nearest_city_elevation,
    get_city_height_from_sea_level,
    haversine_distance_km,
)

logger = logging.getLogger(__name__)


@dataclass
class TransectNode:
    """Represents a discrete waypoint in the middle between crisis start point and user point."""
    index: int
    fraction: float
    latitude: float
    longitude: float
    height_from_sea_level_m: float
    distance_from_crisis_km: float
    distance_to_user_km: float
    nearest_city: str
    stage_description: str


@dataclass
class ElevationTransectResult:
    """Complete result from the Elevation Transect Engine between Crisis Starting Point and User."""
    crisis_start_latitude: float
    crisis_start_longitude: float
    crisis_start_height_msl: float
    crisis_start_nearest_city: str

    user_latitude: float
    user_longitude: float
    user_height_msl: float
    user_nearest_city: str

    total_distance_km: float
    height_difference_m: float
    higher_location_name: str
    lower_location_name: str
    is_crisis_higher: bool
    is_user_higher: bool

    flow_direction: str  # "DOWNHILL_TOWARDS_USER", "DOWNHILL_AWAY_FROM_USER", "FLAT_BASIN_PONDING"
    gradient_slope_percent: float
    middle_transect_nodes: list[TransectNode]
    middle_heights_msl: list[float]
    min_middle_height_msl: float
    max_middle_height_msl: float
    avg_middle_height_msl: float

    has_intermediate_ridge: bool
    has_intermediate_depression: bool
    flood_flow_path_geojson: dict[str, Any]
    flow_prediction_summary: str
    elevation_difference_summary: str


def compute_elevation_between_crisis_and_user(
    crisis_lat: float,
    crisis_lon: float,
    user_lat: float,
    user_lon: float,
    num_middle_samples: int = 5,
) -> ElevationTransectResult:
    """
    Separate Engine to find the elevation profile between User and Crisis Starting Point.

    Calculates:
    1. Elevation and height from sea level (MSL) for the Crisis Starting Point.
    2. Elevation and height from sea level (MSL) for the User Location.
    3. Height from sea level for all intermediate locations in the middle of flood start point and user point.
    4. Elevation difference between the two locations: whichever has the greater value has high elevation.
    5. Predicted flood flow direction based on hydraulic gravity gradient.
    """
    # 1. Crisis Starting Point elevation & nearest city
    crisis_height = get_city_height_from_sea_level(crisis_lat, crisis_lon)
    crisis_city, _, _, _, _ = find_nearest_city_elevation(crisis_lat, crisis_lon)

    # 2. User Location elevation & nearest city
    user_height = get_city_height_from_sea_level(user_lat, user_lon)
    user_city, _, _, _, _ = find_nearest_city_elevation(user_lat, user_lon)

    # Total distance
    tot_dist_km = haversine_distance_km(crisis_lat, crisis_lon, user_lat, user_lon)

    # 3. Elevation difference evaluation
    # Whichever has the greater value has high elevation
    diff_info = calculate_elevation_difference(
        loc1_name="Crisis Starting Point",
        loc1_height_m=crisis_height,
        loc2_name="User Location",
        loc2_height_m=user_height,
    )
    diff_m = diff_info["difference_m"]
    is_crisis_higher = crisis_height > user_height
    is_user_higher = user_height > crisis_height

    # Hydraulic gradient (meters per km)
    slope_pct = round((diff_m / max(0.5, tot_dist_km * 1000.0)) * 100.0, 3)

    if is_crisis_higher:
        flow_dir = "DOWNHILL_TOWARDS_USER"
        pred_summary = (
            f"DOWNWARD FLOOD GRADIENT: Crisis starting point ({crisis_city} area, {crisis_height:.1f} m MSL) "
            f"is higher by {diff_m:.1f} m than user location ({user_city} area, {user_height:.1f} m MSL). "
            f"Flood runoff naturally flows downhill towards user at a gradient of {slope_pct:.2f}%."
        )
    elif is_user_higher:
        flow_dir = "DOWNHILL_AWAY_FROM_USER"
        pred_summary = (
            f"ELEVATED USER ADVANTAGE: User location ({user_city} area, {user_height:.1f} m MSL) "
            f"is higher by {diff_m:.1f} m than crisis starting point ({crisis_city} area, {crisis_height:.1f} m MSL). "
            "Flood runoff flows downhill away from user; user enjoys natural topographic elevation."
        )
    else:
        flow_dir = "FLAT_BASIN_PONDING"
        pred_summary = (
            f"FLAT RETENTION PLAIN: Both crisis starting point and user location have equal elevation "
            f"({user_height:.1f} m MSL). High risk of stagnant floodwater ponding and slow drainage."
        )

    # 4. Middle transect sampling across all intermediate locations
    middle_nodes: list[TransectNode] = []
    middle_heights: list[float] = []

    # Sample evenly spaced fractions between 0.15 and 0.85
    step = 0.7 / max(1, num_middle_samples - 1) if num_middle_samples > 1 else 0.5
    fractions = [round(0.15 + i * step, 2) for i in range(num_middle_samples)]

    flow_path_coords: list[list[float]] = [[round(crisis_lon, 5), round(crisis_lat, 5)]]

    for idx, t in enumerate(fractions):
        m_lat = crisis_lat + t * (user_lat - crisis_lat)
        m_lon = crisis_lon + t * (user_lon - crisis_lon)
        m_h = get_city_height_from_sea_level(m_lat, m_lon)
        m_city, _, _, _, _ = find_nearest_city_elevation(m_lat, m_lon)
        d_from_crisis = round(tot_dist_km * t, 2)
        d_to_user = round(tot_dist_km * (1.0 - t), 2)

        stage_desc = f"Intermediate Transect Waypoint {idx + 1}"
        if idx == 0:
            stage_desc = "Upstream Drainage Transition"
        elif idx == len(fractions) // 2:
            stage_desc = "Mid-Corridor Basin Transect"
        elif idx == len(fractions) - 1:
            stage_desc = "Approach to User Vicinity"

        middle_nodes.append(
            TransectNode(
                index=idx + 1,
                fraction=t,
                latitude=round(m_lat, 5),
                longitude=round(m_lon, 5),
                height_from_sea_level_m=round(m_h, 1),
                distance_from_crisis_km=d_from_crisis,
                distance_to_user_km=d_to_user,
                nearest_city=m_city,
                stage_description=stage_desc,
            )
        )
        middle_heights.append(round(m_h, 1))
        flow_path_coords.append([round(m_lon, 5), round(m_lat, 5)])

    flow_path_coords.append([round(user_lon, 5), round(user_lat, 5)])

    min_mid = min(middle_heights) if middle_heights else user_height
    max_mid = max(middle_heights) if middle_heights else user_height
    avg_mid = round(sum(middle_heights) / len(middle_heights), 1) if middle_heights else user_height

    # Ridge and depression checks
    has_ridge = any(h > max(crisis_height, user_height) + 2.0 for h in middle_heights)
    has_depression = any(h < min(crisis_height, user_height) - 2.0 for h in middle_heights)

    # Build GeoJSON for the flood flow path
    flow_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "title": "Flood Elevation Transect & Hydraulic Flow Path",
                    "flow_direction": flow_dir,
                    "crisis_start_height_msl": crisis_height,
                    "user_height_msl": user_height,
                    "height_difference_m": diff_m,
                    "slope_percent": slope_pct,
                    "higher_location": diff_info["higher_location"],
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": flow_path_coords,
                },
            }
        ],
    }

    return ElevationTransectResult(
        crisis_start_latitude=round(crisis_lat, 5),
        crisis_start_longitude=round(crisis_lon, 5),
        crisis_start_height_msl=round(crisis_height, 1),
        crisis_start_nearest_city=crisis_city,
        user_latitude=round(user_lat, 5),
        user_longitude=round(user_lon, 5),
        user_height_msl=round(user_height, 1),
        user_nearest_city=user_city,
        total_distance_km=round(tot_dist_km, 2),
        height_difference_m=diff_m,
        higher_location_name=diff_info["higher_location"],
        lower_location_name=diff_info["lower_location"],
        is_crisis_higher=is_crisis_higher,
        is_user_higher=is_user_higher,
        flow_direction=flow_dir,
        gradient_slope_percent=slope_pct,
        middle_transect_nodes=middle_nodes,
        middle_heights_msl=middle_heights,
        min_middle_height_msl=round(min_mid, 1),
        max_middle_height_msl=round(max_mid, 1),
        avg_middle_height_msl=avg_mid,
        has_intermediate_ridge=has_ridge,
        has_intermediate_depression=has_depression,
        flood_flow_path_geojson=flow_geojson,
        flow_prediction_summary=pred_summary,
        elevation_difference_summary=diff_info["summary"],
    )
