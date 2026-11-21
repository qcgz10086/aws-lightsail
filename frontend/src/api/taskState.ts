export const liveTaskRequests = new Map<string, string>()

export function clearTaskRequests() {
  liveTaskRequests.clear()
}
