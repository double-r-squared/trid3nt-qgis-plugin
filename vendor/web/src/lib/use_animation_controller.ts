// GRACE-2 web — React binding for the module-level AnimationController.
//
// JOB WEB-ANIM (#157.1-.3): the playback state (active group / frame index /
// playing) now lives in the process-global AnimationController (lib/
// animation_controller.ts) so it survives a LayerPanel unmount. Components that
// CONTROL or DISPLAY the animation (LayerPanel, SequenceScrubber, App) subscribe
// to that controller through this hook, which bridges the external store into
// React via useSyncExternalStore (React 18). It is kept SEPARATE from the
// controller module so the controller stays React-free + unit-testable.

import { useSyncExternalStore } from "react";
import {
  getAnimationController,
  type AnimState,
  type AnimationController,
} from "./animation_controller";

/**
 * Subscribe a component to the shared AnimationController's state. Returns the
 * latest AnimState; the component re-renders whenever the controller notifies
 * (group set / active group / frame / playing change).
 */
export function useAnimationState(
  controller: AnimationController = getAnimationController(),
): AnimState {
  return useSyncExternalStore(
    (cb) => {
      // controller.subscribe immediately invokes cb with the current state and
      // returns an unsubscribe fn — both of which match the store contract.
      // useSyncExternalStore ignores the immediate-invoke (it reads the snapshot
      // separately) and uses cb purely as the change signal, so this is safe.
      const unsub = controller.subscribe(() => cb());
      return unsub;
    },
    () => controller.snapshot(),
    () => controller.snapshot(),
  );
}
