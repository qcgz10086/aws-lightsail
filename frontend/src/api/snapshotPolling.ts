// Follow the initial directory/identity and data chain promptly, then back off.
export function snapshotPollDelay(startedAt: number): number {
  return Date.now() - startedAt < 2000 ? 50 : 500
}
