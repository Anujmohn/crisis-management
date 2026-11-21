from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from app.schemas.flood_assessment import (
    GeographicalEnvironment,
    HillyTerrainAnalysis,
    TerrainEvidence,
    TerrainHazardStatus,
)

logger = logging.getLogger(__name__)

# Metadata for authoritative Digital Elevation Model (DEM) datasets
COPERNICUS_DEM_METADATA = {
    "name": "Copernicus DEM GLO-30",
    "agency": "European Space Agency (ESA) / Airbus",
    "spatial_resolution_m": 30.0,
    "spatial_resolution_arcsec": 1.0,
    "vertical_datum": "EGM2008 geoid",
    "horizontal_datum": "WGS84 (EPSG:4326)",
    "coverage": "Global landmass including entire Indian territory and Himalayan arc",
    "license": "Open and free under the Copernicus Programme Open Data Policy",
    "quality_and_limitations": (
        "Derived from TanDEM-X InSAR radar mission. Subject to radar layover and shadow in extreme "
        "high-relief Himalayan gorges; water bodies and flat coastal zones flattened; voids filled "
        "with secondary DEM sources."
    ),
}

SRTM_DEM_METADATA = {
    "name": "Shuttle Radar Topography Mission (SRTM GL1)",
    "agency": "NASA / USGS / NGA",
    "spatial_resolution_m": 30.0,
    "spatial_resolution_arcsec": 1.0,
    "vertical_datum": "EGM96 geoid",
    "horizontal_datum": "WGS84 (EPSG:4326)",
    "coverage": "Global landmass between 60°N and 56°S",
    "license": "Public domain (US Government work)",
    "quality_and_limitations": (
        "Acquired in February 2000. Contains data voids in high alpine glaciated peaks; "
        "C-band radar partially reflects off forest canopy rather than bare earth; "
        "does not capture post-2000 infrastructure or terrain alterations."
    ),
}

D8_OFFSETS = [
    (0, 1, 0.0),      # 0: East
    (-1, 1, 45.0),    # 1: North-East
    (-1, 0, 90.0),    # 2: North
    (-1, -1, 135.0),  # 3: North-West
    (0, -1, 180.0),   # 4: West
    (1, -1, 225.0),   # 5: South-West
    (1, 0, 270.0),    # 6: South
    (1, 1, 315.0),    # 7: South-East
]


@dataclass
class ElevationGrid:
    """
    In-memory elevation raster grid representing local topography around a target coordinate.
    """

    elevations: np.ndarray  # 2D array of elevation values in meters
    cell_size_m: float = 30.0
    source_name: str = "Copernicus DEM GLO-30"
    resolution_m: float = 30.0
    center_row: int | None = None
    center_col: int | None = None

    def __post_init__(self) -> None:
        if self.center_row is None:
            self.center_row = self.elevations.shape[0] // 2
        if self.center_col is None:
            self.center_col = self.elevations.shape[1] // 2


def calculate_local_elevation(grid: ElevationGrid) -> float:
    """Returns elevation in meters at the center cell."""
    return float(grid.elevations[grid.center_row, grid.center_col])


def calculate_slope_and_aspect(grid: ElevationGrid) -> tuple[float, float, str]:
    """
    Computes local terrain slope (degrees) and aspect (degrees and 8-point compass text)
    at the center cell using finite central differences on a 3x3 window.
    """
    r, c = grid.center_row, grid.center_col
    elev = grid.elevations
    dx = grid.cell_size_m

    # Finite differences with boundary safety
    r_prev = max(0, r - 1)
    r_next = min(elev.shape[0] - 1, r + 1)
    c_prev = max(0, c - 1)
    c_next = min(elev.shape[1] - 1, c + 1)

    dz_dx = (elev[r, c_next] - elev[r, c_prev]) / (2.0 * dx) if c_next > c_prev else 0.0
    dz_dy = (elev[r_prev, c] - elev[r_next, c]) / (2.0 * dx) if r_next > r_prev else 0.0  # North is -y in matrix

    gradient = math.sqrt(dz_dx**2 + dz_dy**2)
    slope_deg = round(math.degrees(math.atan(gradient)), 2)

    if gradient < 1e-5:
        return slope_deg, 0.0, "FLAT"

    aspect_rad = math.atan2(dz_dy, -dz_dx)
    aspect_deg = (90.0 - math.degrees(aspect_rad)) % 360.0

    compass_sectors = [
        "N", "NE", "E", "SE", "S", "SW", "W", "NW", "N"
    ]
    sector_idx = int((aspect_deg + 22.5) // 45) % 8
    compass_text = compass_sectors[sector_idx]

    return slope_deg, round(aspect_deg, 1), compass_text


def calculate_d8_flow_directions(grid: ElevationGrid) -> tuple[np.ndarray, float]:
    """
    Computes the D8 flow direction matrix across the elevation grid.
    Returns (flow_dir_matrix, center_flow_deg).
    flow_dir_matrix stores neighbor index (0..7) or -1 for pit/flat cells.
    """
    elev = grid.elevations
    rows, cols = elev.shape
    flow_dirs = np.full((rows, cols), -1, dtype=int)
    dx = grid.cell_size_m

    for r in range(rows):
        for c in range(cols):
            z_curr = elev[r, c]
            max_drop = -1.0
            best_dir = -1

            for dir_idx, (dr, dc, deg) in enumerate(D8_OFFSETS):
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols:
                    dist = dx * (math.sqrt(2.0) if dr != 0 and dc != 0 else 1.0)
                    drop = (z_curr - elev[nr, nc]) / dist
                    if drop > max_drop and drop > 0:
                        max_drop = drop
                        best_dir = dir_idx

            flow_dirs[r, c] = best_dir

    center_dir_idx = flow_dirs[grid.center_row, grid.center_col]
    center_deg = D8_OFFSETS[center_dir_idx][2] if center_dir_idx >= 0 else 0.0

    return flow_dirs, center_deg


def calculate_flow_accumulation(grid: ElevationGrid, flow_dirs: np.ndarray) -> tuple[np.ndarray, int, float]:
    """
    Computes flow accumulation across the elevation grid using D8 routing.
    Returns (accumulation_matrix, center_accumulation_count, upstream_catchment_area_km2).
    """
    rows, cols = flow_dirs.shape
    accum = np.ones((rows, cols), dtype=int)

    # Sort cells in descending order of elevation (topological routing from peaks down to valleys)
    cell_indices = [
        (r, c) for r in range(rows) for c in range(cols)
    ]
    cell_indices.sort(key=lambda idx: grid.elevations[idx[0], idx[1]], reverse=True)

    for r, c in cell_indices:
        dir_idx = flow_dirs[r, c]
        if dir_idx >= 0:
            dr, dc, _ = D8_OFFSETS[dir_idx]
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                accum[nr, nc] += accum[r, c]

    center_accum = int(accum[grid.center_row, grid.center_col])
    # Upstream catchment area in square kilometers: cells * (cell_size_m)^2 / 10^6
    upstream_km2 = round((center_accum * (grid.cell_size_m**2)) / 1_000_000.0, 4)

    return accum, center_accum, upstream_km2


def extract_drainage_network_and_hand(
    grid: ElevationGrid,
    flow_dirs: np.ndarray,
    accum_matrix: np.ndarray,
    stream_initiation_threshold_cells: int = 15,
) -> tuple[float, float]:
    """
    Identifies concentrated drainage network cells (flow accumulation >= threshold)
    and computes:
    1. Distance (meters) to the nearest drainage network cell.
    2. Relative elevation above nearest drainage (HAND in meters).
    """
    rows, cols = accum_matrix.shape
    cr, cc = grid.center_row, grid.center_col
    drainage_mask = accum_matrix >= stream_initiation_threshold_cells

    # Check if center cell is itself part of the drainage network
    if drainage_mask[cr, cc]:
        return 0.0, 0.0

    # 1. Tracing flow path downhill to compute HAND (Height Above Nearest Drainage)
    curr_r, curr_c = cr, cc
    z_center = grid.elevations[cr, cc]
    hand_m = 0.0
    found_channel = False

    visited = set()
    while 0 <= curr_r < rows and 0 <= curr_c < cols and (curr_r, curr_c) not in visited:
        visited.add((curr_r, curr_c))
        if drainage_mask[curr_r, curr_c]:
            hand_m = max(0.0, float(z_center - grid.elevations[curr_r, curr_c]))
            found_channel = True
            break
        dir_idx = flow_dirs[curr_r, curr_c]
        if dir_idx < 0:
            break
        dr, dc, _ = D8_OFFSETS[dir_idx]
        curr_r += dr
        curr_c += dc

    if not found_channel:
        # Fallback to elevation difference relative to minimum cell in grid
        hand_m = max(0.0, float(z_center - np.min(grid.elevations)))

    # 2. Euclidean distance in meters to nearest drainage cell
    drainage_indices = np.argwhere(drainage_mask)
    if len(drainage_indices) > 0:
        distances_m = np.sqrt(
            ((drainage_indices[:, 0] - cr) * grid.cell_size_m) ** 2
            + ((drainage_indices[:, 1] - cc) * grid.cell_size_m) ** 2
        )
        min_dist_m = float(np.min(distances_m))
    else:
        min_dist_m = float(max(rows, cols) * grid.cell_size_m)

    return round(min_dist_m, 1), round(hand_m, 2)


# Alias for explicit relative elevation calculation
calculate_relative_elevation_above_drainage = extract_drainage_network_and_hand


def calculate_topographic_wetness_index(
    slope_deg: float,
    upstream_catchment_m2: float,
    contour_width_m: float = 30.0,
) -> float | None:
    """
    Computes Topographic Wetness Index (TWI):
    TWI = ln(a / tan(beta)), where a is specific catchment area (upstream area per unit contour length).
    """
    slope_rad = math.radians(max(0.5, slope_deg))
    specific_catchment_area = max(contour_width_m, upstream_catchment_m2 / contour_width_m)
    twi = math.log(specific_catchment_area / math.tan(slope_rad))
    return round(twi, 2)


def classify_valley_morphometry(
    grid: ElevationGrid,
    flow_dir_deg: float,
) -> tuple[str, list[str]]:
    """
    Evaluates valley confinement and cross-sectional morphology perpendicular to water flow:
    - NARROW_CANYON: Steep opposing slopes (> 20°) with confined channel width.
    - V_SHAPED_VALLEY: Slopes between 10° and 20° flanking drainage axis.
    - CONVERGENT_HOLLOW: Surrounding slopes funneling inward towards candidate point.
    - RIDGE_OR_PEAK: Candidate location sits on a local topographical crest.
    - OPEN_VALLEY: Broad unconfined plain or valley bottom.
    """
    elev = grid.elevations
    r, c = grid.center_row, grid.center_col
    dx = grid.cell_size_m
    z_center = elev[r, c]

    # Evaluate lateral neighbors perpendicular to flow direction
    perp_angle_rad = math.radians((flow_dir_deg + 90.0) % 360.0)
    dr_perp = -round(math.sin(perp_angle_rad))  # Row offset
    dc_perp = round(math.cos(perp_angle_rad))   # Col offset

    r1, c1 = int(r + dr_perp), int(c + dc_perp)
    r2, c2 = int(r - dr_perp), int(c - dc_perp)

    rows, cols = elev.shape
    notes: list[str] = []

    # Check surrounding 3x3 for ridge crest behavior
    neighbors_lower = 0
    total_neighbors = 0
    for dr, dc, _ in D8_OFFSETS:
        nr, nc = r + dr, c + dc
        if 0 <= nr < rows and 0 <= nc < cols:
            total_neighbors += 1
            if elev[nr, nc] < z_center:
                neighbors_lower += 1

    if total_neighbors > 0 and neighbors_lower >= 6:
        notes.append("Location is situated on a local topographical ridge or crest; surface runoff sheds away.")
        return "RIDGE_OR_PEAK", notes

    left_slope = 0.0
    right_slope = 0.0
    if 0 <= r1 < rows and 0 <= c1 < cols:
        left_slope = math.degrees(math.atan(max(0.0, elev[r1, c1] - z_center) / dx))
    if 0 <= r2 < rows and 0 <= c2 < cols:
        right_slope = math.degrees(math.atan(max(0.0, elev[r2, c2] - z_center) / dx))

    avg_lateral_slope = (left_slope + right_slope) / 2.0

    if left_slope >= 20.0 and right_slope >= 20.0:
        notes.append(
            f"Narrow canyon formation: Steep opposing lateral slopes ({left_slope:.1f}° / {right_slope:.1f}°) "
            "severely confine water flow, concentrating flood depth and kinetic velocity."
        )
        return "NARROW_CANYON", notes

    if left_slope >= 10.0 and right_slope >= 10.0:
        notes.append(
            f"V-shaped mountain valley: Flanked by opposing valley walls ({left_slope:.1f}° / {right_slope:.1f}°)."
        )
        return "V_SHAPED_VALLEY", notes

    if avg_lateral_slope >= 8.0:
        notes.append(
            "Convergent drainage hollow: Surrounding terrain slopes funnel surface runoff into this path."
        )
        return "CONVERGENT_HOLLOW", notes

    notes.append("Open unconfined valley or moderate relief morphology.")
    return "OPEN_VALLEY", notes


def create_synthetic_valley_dem_grid(
    valley_depth_m: float = 80.0,
    slope_lateral_deg: float = 25.0,
    cell_size_m: float = 30.0,
) -> ElevationGrid:
    """
    Creates a controlled synthetic elevation grid representing a narrow mountain valley with high upstream accumulation.
    Used for unit testing and simulation.
    """
    # 7x7 grid with central drainage channel along column 3 (flowing from North to South)
    grid = np.zeros((7, 7), dtype=float)
    base_elev = 1500.0

    for r in range(7):
        channel_elev = base_elev - (r * 15.0)  # Longitudinal descent 15m per cell
        for c in range(7):
            dist_from_channel = abs(c - 3)
            # Lateral rise on valley walls
            lateral_rise = dist_from_channel * (cell_size_m * math.tan(math.radians(slope_lateral_deg)))
            grid[r, c] = channel_elev + lateral_rise

    return ElevationGrid(
        elevations=grid,
        cell_size_m=cell_size_m,
        source_name="Synthetic Valley DEM (Controlled Verification)",
        resolution_m=cell_size_m,
        center_row=3,
        center_col=3,
    )


def create_synthetic_steep_slope_dem_grid(
    slope_deg: float = 32.0,
    cell_size_m: float = 30.0,
) -> ElevationGrid:
    """
    Creates a controlled synthetic elevation grid representing a steep mountain hillside.
    Used for unit testing slope instability and landslide susceptibility.
    """
    grid = np.zeros((7, 7), dtype=float)
    base_elev = 2200.0
    tan_slope = math.tan(math.radians(slope_deg))

    for r in range(7):
        for c in range(7):
            grid[r, c] = base_elev - (r * cell_size_m * tan_slope)

    return ElevationGrid(
        elevations=grid,
        cell_size_m=cell_size_m,
        source_name="Synthetic Steep Slope DEM (Controlled Verification)",
        resolution_m=cell_size_m,
        center_row=3,
        center_col=3,
    )


def create_synthetic_ridge_dem_grid(
    cell_size_m: float = 30.0,
) -> ElevationGrid:
    """
    Creates a controlled synthetic elevation grid representing a mountain ridge crest.
    """
    grid = np.zeros((7, 7), dtype=float)
    ridge_elev = 2800.0

    for r in range(7):
        for c in range(7):
            dist_from_center = math.sqrt((r - 3) ** 2 + (c - 3) ** 2)
            grid[r, c] = ridge_elev - (dist_from_center * 25.0)

    return ElevationGrid(
        elevations=grid,
        cell_size_m=cell_size_m,
        source_name="Synthetic Ridge DEM (Controlled Verification)",
        resolution_m=cell_size_m,
        center_row=3,
        center_col=3,
    )


def analyze_hilly_terrain_hazard(
    latitude: float,
    longitude: float,
    dem_grid: ElevationGrid | None = None,
    nearby_rainfall_obs: list[Any] | None = None,
    nearby_warnings: list[Any] | None = None,
    nearby_river_obs: list[Any] | None = None,
    observed_floods: list[Any] | None = None,
) -> tuple[TerrainEvidence, HillyTerrainAnalysis]:
    """
    Specialized terrain-aware hazard assessment engine for hilly and mountainous regions.
    Analyzes:
    1. Upstream flow path & contributing catchment area
    2. Valley confinement & drainage convergence
    3. Flash flood susceptibility
    4. Landslide & debris-flow susceptibility
    """
    nearby_rainfall_obs = nearby_rainfall_obs or []
    nearby_warnings = nearby_warnings or []
    nearby_river_obs = nearby_river_obs or []
    observed_floods = observed_floods or []

    contributing_factors: list[str] = []
    limitations: list[str] = [
        "Higher elevation does not guarantee flood safety if the location sits along an active upstream runoff path.",
        "Mountain ridges are not automatically safe evacuation locations and may experience lightning, landslides, or road severance.",
        "DEM-derived flow accumulation is a topographic indicator of water collection, not a direct measurement of flood discharge or depth.",
        "Flash floods in steep catchments can occur with minimal warning; upstream rainfall can generate rapid downstream surges.",
        "Slope alone cannot predict landslides or debris flows without geotechnical, soil moisture, and geological structural data.",
    ]
    terrain_hazard_statuses: list[TerrainHazardStatus] = []

    # 1. Graceful Handling when DEM Grid is Unavailable
    if dem_grid is None:
        contributing_factors.append(
            "Location identified in mountainous physiographic zone, but authoritative high-resolution DEM raster "
            "is not locally available to compute flow direction, upstream catchment, or flow accumulation."
        )
        limitations.append(
            "Topographical variables (slope, flow accumulation, TWI, HAND) cannot be calculated without authoritative DEM raster coverage."
        )
        terrain_hazard_statuses.append(TerrainHazardStatus.INSUFFICIENT_DATA)

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
            geographical_environment=GeographicalEnvironment.HILLY_OR_MOUNTAINOUS,
            notes=[
                "High-resolution DEM raster (Copernicus DEM / SRTM) is not locally stored.",
                "Topographic flow accumulation, slope, and valley morphometry are uncomputed.",
            ],
        )

        hilly_analysis = HillyTerrainAnalysis(
            applied=True,
            dem_source=None,
            dem_resolution_m=None,
            upstream_flow_path_exposure=False,
            upstream_catchment_concern=False,
            valley_flood_concern=False,
            landslide_susceptibility_concern=False,
            debris_flow_susceptibility_concern=False,
            flash_flood_concern=False,
            upstream_rainfall_concern=False,
            terrain_hazard_statuses=terrain_hazard_statuses,
            contributing_factors=contributing_factors,
            limitations_and_assumptions=limitations,
        )

        return terrain_evidence, hilly_analysis

    # 2. Extract Topographical Metrics from DEM
    elevation_m = calculate_local_elevation(dem_grid)
    slope_deg, aspect_deg, aspect_text = calculate_slope_and_aspect(dem_grid)
    flow_dirs, center_flow_deg = calculate_d8_flow_directions(dem_grid)
    accum_matrix, center_accum, upstream_km2 = calculate_flow_accumulation(dem_grid, flow_dirs)
    drainage_prox_m, hand_m = extract_drainage_network_and_hand(dem_grid, flow_dirs, accum_matrix)
    upstream_m2 = center_accum * (dem_grid.cell_size_m**2)
    twi = calculate_topographic_wetness_index(slope_deg, upstream_m2, dem_grid.cell_size_m)
    valley_conf, valley_notes = classify_valley_morphometry(dem_grid, center_flow_deg)

    # 3. Assess Hydrometeorological Signals
    has_heavy_rainfall = any(
        (rf.rainfall_intensity_mm_hr or 0) >= 15.0 or rf.warning_color in ("Red", "Orange")
        for rf in nearby_rainfall_obs
    )
    has_official_warning = len(nearby_warnings) > 0
    has_river_above_warning = any(
        getattr(r, "status_relative_to_danger", None) in ("ABOVE_WARNING", "ABOVE_DANGER", "ABOVE_HFL")
        for r in nearby_river_obs
    )
    has_observed_flooding = len(observed_floods) > 0

    # 4. Evaluate Specific Terrain Hazards
    # A. Flow Path Exposure
    # Candidate cell has high flow accumulation or sits in immediate drainage channel with low HAND
    high_flow_path = (center_accum >= 15) or (drainage_prox_m <= 40.0 and hand_m <= 5.0)
    if high_flow_path:
        terrain_hazard_statuses.append(TerrainHazardStatus.HIGH_FLOW_PATH_EXPOSURE)
        contributing_factors.append(
            f"High flow path exposure: Location lies directly along or adjacent to a concentrated drainage path "
            f"(accumulation: {center_accum} cells; relative elevation above drainage HAND: {hand_m:.1f} m)."
        )

    # B. Upstream Catchment Concern
    # Significant upstream contributing area funneling runoff towards the location
    upstream_concern = upstream_km2 >= 0.02 or center_accum >= 20
    if upstream_concern:
        terrain_hazard_statuses.append(TerrainHazardStatus.UPSTREAM_CATCHMENT_CONCERN)
        contributing_factors.append(
            f"Upstream catchment concern: Drainage area of {upstream_km2:.4f} km² ({center_accum} cells) "
            "drains towards this location. Water volume can accumulate rapidly during precipitation."
        )

    # C. Valley Flood Concern
    # Narrow or V-shaped valley confinement
    valley_concern = valley_conf in ("NARROW_CANYON", "V_SHAPED_VALLEY", "CONVERGENT_HOLLOW") and hand_m <= 15.0
    if valley_concern:
        terrain_hazard_statuses.append(TerrainHazardStatus.VALLEY_FLOOD_CONCERN)
        contributing_factors.append(
            f"Valley flood concern: Confinement classified as {valley_conf} with low drainage clearance "
            f"(HAND: {hand_m:.1f} m); lateral slopes restrict flood dispersion."
        )
    contributing_factors.extend(valley_notes)

    # D. Flash Flood Concern
    # Convergence of flow path / valley confinement with active heavy rainfall or warning
    flash_flood_concern = (high_flow_path or valley_concern or upstream_concern) and (has_heavy_rainfall or has_official_warning)
    if flash_flood_concern:
        contributing_factors.append(
            "Flash flood concern: Concentrated drainage path/catchment convergence combined with active "
            "heavy rainfall warnings elevates flash flood surge hazard."
        )

    # E. Landslide-Related Concern
    # Steep slope (>= 25°) or (>= 18° with heavy rain / warnings)
    landslide_concern = (slope_deg >= 25.0) or (slope_deg >= 18.0 and (has_heavy_rainfall or has_official_warning))
    if landslide_concern:
        terrain_hazard_statuses.append(TerrainHazardStatus.LANDSLIDE_RELATED_CONCERN)
        contributing_factors.append(
            f"Landslide susceptibility concern: Steep terrain slope ({slope_deg:.1f}°) "
            f"elevates slope failure risk under intense saturation conditions."
        )

    # F. Debris-Flow Concern
    # Steep slope + drainage convergence / valley confinement + heavy rainfall
    debris_flow_concern = (slope_deg >= 15.0) and (high_flow_path or valley_conf in ("NARROW_CANYON", "V_SHAPED_VALLEY")) and (has_heavy_rainfall or has_official_warning)
    if debris_flow_concern:
        terrain_hazard_statuses.append(TerrainHazardStatus.DEBRIS_FLOW_CONCERN)
        contributing_factors.append(
            f"Debris-flow concern: Steep gradient ({slope_deg:.1f}°) combined with channelized valley confinement "
            "can trigger sediment-laden debris surges during extreme runoff events."
        )

    # Check observed flooding and official warnings
    if has_observed_flooding:
        terrain_hazard_statuses.insert(0, TerrainHazardStatus.OBSERVED_FLOODING)
    if has_official_warning:
        terrain_hazard_statuses.insert(0, TerrainHazardStatus.OFFICIAL_FLOOD_WARNING)

    if not terrain_hazard_statuses:
        terrain_hazard_statuses.append(TerrainHazardStatus.NO_RELEVANT_HAZARD_EVIDENCE)

    terrain_notes = [
        f"DEM source: {dem_grid.source_name} ({dem_grid.resolution_m}m resolution).",
        f"Measured elevation: {elevation_m:.1f} m; slope: {slope_deg:.1f}°; aspect: {aspect_deg:.1f}° ({aspect_text}).",
        f"Upstream catchment area: {upstream_km2:.4f} km² ({center_accum} contributing cells).",
        f"Height Above Nearest Drainage (HAND): {hand_m:.1f} m; distance to channel: {drainage_prox_m:.1f} m.",
        f"Valley confinement: {valley_conf}.",
    ]
    if twi is not None:
        terrain_notes.append(f"Topographic Wetness Index (TWI): {twi:.2f}.")

    terrain_evidence = TerrainEvidence(
        available=True,
        elevation_m=elevation_m,
        slope_degrees=slope_deg,
        aspect=f"{aspect_deg:.1f}° ({aspect_text})",
        flow_accumulation=float(center_accum),
        drainage_proximity_m=drainage_prox_m,
        upstream_catchment_area_km2=upstream_km2,
        flow_direction_deg=center_flow_deg,
        topographic_wetness_index=twi,
        relative_elevation_above_drainage_m=hand_m,
        valley_confinement=valley_conf,
        geographical_environment=GeographicalEnvironment.HILLY_OR_MOUNTAINOUS,
        notes=terrain_notes,
    )

    hilly_analysis = HillyTerrainAnalysis(
        applied=True,
        dem_source=dem_grid.source_name,
        dem_resolution_m=dem_grid.resolution_m,
        upstream_flow_path_exposure=high_flow_path,
        upstream_catchment_concern=upstream_concern,
        valley_flood_concern=valley_concern,
        landslide_susceptibility_concern=landslide_concern,
        debris_flow_susceptibility_concern=debris_flow_concern,
        flash_flood_concern=flash_flood_concern,
        upstream_rainfall_concern=has_heavy_rainfall,
        terrain_hazard_statuses=terrain_hazard_statuses,
        contributing_factors=contributing_factors,
        limitations_and_assumptions=limitations,
    )

    return terrain_evidence, hilly_analysis
