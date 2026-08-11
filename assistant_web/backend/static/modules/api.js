export const apiFetch = globalThis.AssistantApiFetch || globalThis.fetch.bind(globalThis);

export async function responseJson(response) {
  return response.json().catch(() => ({}));
}
