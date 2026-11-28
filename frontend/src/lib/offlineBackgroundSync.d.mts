export const BACKGROUND_SYNC_TAG: string;
export const REPLAY_QUEUE_MESSAGE_TYPE: string;

export function isReplayQueueMessage(data: unknown): boolean;

// The registration is typed as unknown on purpose: the DOM lib's
// ServiceWorkerRegistration has no `sync` property (Background Sync is not
// a standard API), so callers pass the platform registration straight
// through and this function probes for `sync` at runtime. It never throws.
export function registerBackgroundSync(registration: unknown): Promise<boolean>;
