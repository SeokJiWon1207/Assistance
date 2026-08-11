export function scopedStorageKey(base, userId) {
  return `${base}:${userId || "guest"}`;
}

export const WORKSPACE_ENDPOINTS = Object.freeze({
  personal: "/api/workspace/me",
  team: "/api/workspace/team",
});
