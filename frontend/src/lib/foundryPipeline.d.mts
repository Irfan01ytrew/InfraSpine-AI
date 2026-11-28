import type { Spec } from "../workflows/types";
import type { FoundryAppDetail, FoundryBuild } from "../types";

export interface FoundryPipelineStage {
  id: string;
  label: string;
  description: string;
}

export declare const PIPELINE_STAGES: FoundryPipelineStage[];
export declare function stageStatus(
  build: FoundryBuild | null,
  feedbackCount?: number,
): Record<string, string>;
export declare function buildFoundryPipelineSpec(appDetail: FoundryAppDetail | null): Spec;
