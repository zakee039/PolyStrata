// Keep the Overview label and click action on the same model-state rule.
// "paused" is a manual hold: clicking Start must issue load_model, never unload_model.
export function modelIsLoaded(managed: boolean, phase: string): boolean {
  return managed && !["unloaded", "paused", "stopped", "starting", "stopping", "error", "external"].includes(phase);
}
