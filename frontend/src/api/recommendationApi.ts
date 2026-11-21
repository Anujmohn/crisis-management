/**
 * Recommendation & Disaster-Specific Safe-Location Suitability API Client
 *
 * Connects to:
 *   - POST /api/v1/recommendations/assess
 *   - POST /api/v1/recommendations/unified
 */

import { DisasterEventSummary } from './disasterApi';
import { RouteClassification, RouteDetailItem } from './routeApi';

export type DisasterType = 'EARTHQUAKE' | 'FLOOD' | 'CYCLONE';

export type SuitabilityClassification =
  | 'SUITABILITY_SUPPORTED_BY_EVIDENCE'
  | 'CONDITIONALLY_SUITABLE'
  | 'NOT_RECOMMENDED'
  | 'EXCLUDED'
  | 'INSUFFICIENT_DATA'
  | 'UNKNOWN';

export type DesignationStatus = 'OFFICIAL' | 'POTENTIAL' | 'UNKNOWN';
export type VerificationStatus = 'VERIFIED' | 'UNVERIFIED' | 'REJECTED';

export type UnifiedRecommendationStatus =
  | 'RECOMMENDATIONS_AVAILABLE'
  | 'LIMITED_RECOMMENDATIONS'
  | 'NO_SUITABLE_DESTINATION_FOUND'
  | 'NO_ROUTE_AVAILABLE'
  | 'INSUFFICIENT_DATA'
  | 'DISASTER_CONTEXT_UNAVAILABLE'
  | 'ASSESSMENT_FAILED';

export interface DisasterHazardAssessmentSummary {
  disaster_type: DisasterType;
  status: string;
  summary: string;
  data_freshness: string;
  key_hazards: string[];
  warnings: string[];
  limitations: string[];
}

export interface RecommendedDestinationItem {
  destination_id: string;
  assessment_id?: string | null;
  name: string | null;
  category: string;
  official_designation: DesignationStatus;
  verification_status: VerificationStatus;
  verification_detail: string;
  coordinates: { latitude: number; longitude: number };
  distance_km: number;
  suitability_classification: SuitabilityClassification;
  suitability_score: number | null;
  distance_points?: number | null;
  safety_points?: number | null;
  availability_points?: number | null;
  features_points?: number | null;
  total_points?: number | null;
  suitability_reasons: string[];
  suitability_warnings: string[];
  route: RouteDetailItem | null;
  route_status: RouteClassification | null;
  ranking_score: number | null;
  ranking_rationale: string;
}

export interface UnifiedRecommendationRequest {
  latitude: number;
  longitude: number;
  disaster_type: DisasterType;
  crisis_event_id?: string | null;
  search_radius_km?: number;
  max_destinations?: number;
}

export interface UnifiedRecommendationResponse {
  success: boolean;
  status: UnifiedRecommendationStatus;
  user_location: { latitude: number; longitude: number };
  disaster_type: DisasterType;
  crisis_event: DisasterEventSummary | null;
  hazard_assessment: DisasterHazardAssessmentSummary;
  total_candidates_found: number;
  total_suitable_destinations: number;
  total_routed_destinations: number;
  recommendations: RecommendedDestinationItem[];
  excluded_destinations_count: number;
  methodology_and_ranking_notes: string[];
  overall_uncertainties: string[];
  safety_disclaimer: string;
}

const BASE_URL =
  (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ??
  'http://localhost:8000';

export interface LocationSuitabilityAssessmentItem {
  assessment_id?: string | null;
  location_id: string;
  name: string | null;
  category: string;
  official_designation: DesignationStatus;
  verification_status: VerificationStatus;
  verification_detail: string;
  distance_km: number;
  disaster_type: DisasterType;
  suitability_classification: SuitabilityClassification;
  suitability_score: number | null;
  distance_points?: number | null;
  safety_points?: number | null;
  availability_points?: number | null;
  features_points?: number | null;
  total_points?: number | null;
  data_completeness: number | null;
  hard_exclusion_status: boolean;
  hard_exclusion_reasons: string[];
  supporting_evidence: string[];
  identified_hazards: string[];
  warnings: string[];
  missing_information: string[];
  data_sources: string[];
  data_freshness: string;
  assessed_at: string;
}

export interface AssessRecommendationsRequest {
  latitude: number;
  longitude: number;
  disaster_type: DisasterType;
  crisis_event_id?: string | null;
  search_radius_km?: number;
  limit?: number;
}

export interface AssessRecommendationsResponse {
  success: boolean;
  recommendation_run_id: string;
  user_location: { latitude: number; longitude: number };
  disaster_type: DisasterType;
  crisis_event_id: string | null;
  total_candidates_assessed: number;
  total_potentially_suitable: number;
  total_conditionally_suitable: number;
  total_not_recommended: number;
  total_excluded: number;
  total_insufficient_data: number;
  assessed_locations: LocationSuitabilityAssessmentItem[];
  assessment_methodology: string;
  overall_data_limitations: string[];
  safety_disclaimer: string;
}

/**
 * Triggers disaster-specific safe-location suitability assessment for candidate destinations.
 */
export async function assessSafeLocationsSuitability(
  request: AssessRecommendationsRequest,
): Promise<AssessRecommendationsResponse> {
  const url = `${BASE_URL}/api/v1/recommendations/assess`;
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    throw new Error(
      `Failed to assess recommendations: ${response.status} ${response.statusText}`,
    );
  }

  return response.json();
}

/**
 * Executes Unified Disaster Recommendation Engine:
 * Combines hazard evaluation, destination suitability, and OSRM evacuation routing.
 */
export async function fetchUnifiedRecommendations(
  request: UnifiedRecommendationRequest,
): Promise<UnifiedRecommendationResponse> {
  const url = `${BASE_URL}/api/v1/recommendations/unified`;
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
    },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    throw new Error(
      `Failed to generate unified recommendations: ${response.status} ${response.statusText}`,
    );
  }

  return response.json();
}

