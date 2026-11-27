import type { Root } from "mdast";
export function safeAnswerUrl(value: string, key?: string): string;
export function webCitationTargets(text: string): Map<string, { title: string; url: string }>;
export function remarkCitationLinks(): (tree: Root) => void;
