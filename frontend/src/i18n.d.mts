export type LanguageId = "en" | "roman_urdu" | "urdu";

export interface LanguageOption { id: LanguageId; label: string; nativeLabel: string }

export const DEFAULT_LANGUAGE: LanguageId;
export const LANGUAGES: readonly LanguageOption[];
export function isLanguage(value: string | null | undefined): value is LanguageId;
export function normalizeLanguage(value: string | null | undefined): LanguageId;
export const NAV_STRINGS: Record<string, Record<LanguageId, string>>;
export function tNav(label: string, language: string | null | undefined): string;
export function isRtlLanguage(language: string | null | undefined): boolean;
export function localeForLanguage(language: string | null | undefined): string;

export interface NavEntryLike { view: string; label: string; [key: string]: unknown }
export interface NavGroupLike { id: string; label: string; items: NavEntryLike[]; [key: string]: unknown }
export function translateNavGroups<T extends NavGroupLike>(groups: readonly T[] | null | undefined, language: string | null | undefined): T[];

export function getLanguage(): LanguageId;
export function subscribeLanguage(listener: () => void): () => void;
export function applyLanguageToDocument(language: string | null | undefined): void;
export function setLanguage(language: string | null | undefined): void;
export function restoreLanguage(language: string | null | undefined): void;
