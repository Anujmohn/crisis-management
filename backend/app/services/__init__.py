from app.services.cyclone_assessment import (
    assess_cyclone_hazard,
    calculate_distance_to_indian_coastline_km,
    classify_cyclone_intensity,
    normalize_wind_speed_to_kmph,
)
from app.services.cyclone_imd import sync_imd_cyclone_data
from app.services.cyclone_incois import sync_incois_coastal_data
from app.services.cyclone_ingestion import sync_all_cyclone_sources
from app.services.cyclone_sachet import sync_sachet_cyclone_alerts
from app.services.disaster_search import get_nearby_disasters
from app.services.earthquake_assessment import assess_earthquake_hazard
from app.services.earthquake_ingestion import (
    fetch_ncs_earthquake_html,
    parse_ncs_html,
    sync_earthquakes_from_ncs,
)
from app.services.evacuation_routing import (
    analyze_route_hazards,
    calculate_evacuation_routes,
    call_osrm_driving_route,
    check_destination_eligibility,
    compare_and_rank_routes,
)
from app.services.flood_assessment import (
    assess_flood_hazard,
    classify_geographical_environment,
)
from app.services.flood_cwc import sync_cwc_flood_data
from app.services.flood_imd import sync_imd_flood_data
from app.services.flood_ingestion import sync_all_flood_sources
from app.services.flood_sachet import sync_sachet_flood_alerts
from app.services.location import normalize_location
from app.services.recommendation_engine import (
    assess_safe_locations_suitability,
    evaluate_candidate_suitability,
    evaluate_cyclone_suitability,
    evaluate_earthquake_suitability,
    evaluate_flood_suitability,
)
from app.services.safe_location import (
    get_nearby_safe_locations,
    get_safe_location_by_id,
)
from app.services.safe_location_ingestion import sync_all_safe_location_sources
from app.services.safe_location_ncrmp import sync_ncrmp_shelters
from app.services.safe_location_osm import sync_osm_locations
from app.services.source_registry import (
    VERIFIED_SOURCES,
    VerifiedSourceDefinition,
    get_verified_source_by_code,
    get_verified_sources,
    sync_data_sources,
)
from app.services.unified_recommendation import (
    UNIFIED_SAFETY_DISCLAIMER,
    generate_unified_recommendations,
)
from app.services.alert_service import (
    ALERT_SAFETY_DISCLAIMER,
    classify_report_and_event,
    dispatch_alert_notifications_for_report,
    get_device_notification_history,
    get_device_preferences,
    get_nearby_alerts,
    mark_notification_read,
    register_or_update_device,
    send_expo_push_notification,
    sync_active_disaster_alerts,
    unregister_device,
    update_device_preferences,
)
from app.services.terrain_analysis import (
    ElevationGrid,
    analyze_hilly_terrain_hazard,
    create_synthetic_ridge_dem_grid,
    create_synthetic_steep_slope_dem_grid,
    create_synthetic_valley_dem_grid,
)

__all__ = [
    "VERIFIED_SOURCES",
    "VerifiedSourceDefinition",
    "assess_cyclone_hazard",
    "assess_earthquake_hazard",
    "assess_flood_hazard",
    "assess_safe_locations_suitability",
    "analyze_route_hazards",
    "calculate_evacuation_routes",
    "call_osrm_driving_route",
    "check_destination_eligibility",
    "compare_and_rank_routes",
    "classify_geographical_environment",
    "evaluate_candidate_suitability",
    "evaluate_cyclone_suitability",
    "evaluate_earthquake_suitability",
    "evaluate_flood_suitability",
    "fetch_ncs_earthquake_html",
    "get_nearby_disasters",
    "get_nearby_safe_locations",
    "get_safe_location_by_id",
    "get_verified_source_by_code",
    "get_verified_sources",
    "normalize_location",
    "parse_ncs_html",
    "sync_all_cyclone_sources",
    "sync_all_flood_sources",
    "sync_all_safe_location_sources",
    "sync_cwc_flood_data",
    "sync_earthquakes_from_ncs",
    "sync_imd_cyclone_data",
    "sync_imd_flood_data",
    "sync_incois_coastal_data",
    "sync_ncrmp_shelters",
    "sync_osm_locations",
    "sync_sachet_cyclone_alerts",
    "sync_sachet_flood_alerts",
    "sync_data_sources",
    "ElevationGrid",
    "analyze_hilly_terrain_hazard",
    "create_synthetic_ridge_dem_grid",
    "create_synthetic_steep_slope_dem_grid",
    "create_synthetic_valley_dem_grid",
    "calculate_distance_to_indian_coastline_km",
    "classify_cyclone_intensity",
    "normalize_wind_speed_to_kmph",
    "UNIFIED_SAFETY_DISCLAIMER",
    "generate_unified_recommendations",
    "ALERT_SAFETY_DISCLAIMER",
    "classify_report_and_event",
    "dispatch_alert_notifications_for_report",
    "get_device_notification_history",
    "get_device_preferences",
    "get_nearby_alerts",
    "mark_notification_read",
    "register_or_update_device",
    "send_expo_push_notification",
    "sync_active_disaster_alerts",
    "unregister_device",
    "update_device_preferences",
]
