import type { DocumentParserTestResult, EmbeddingModelStatus, OcrEngineTestResult } from "./types";

export function availabilityLabel(status: EmbeddingModelStatus): string;
export function loadLabel(status: EmbeddingModelStatus): string;
export function parserAvailabilityLabel(available: boolean): string;
export function parserTestLabel(result: DocumentParserTestResult): string;
export function ocrTestLabel(result: OcrEngineTestResult): string;
