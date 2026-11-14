// src/features/memory/lib/motion.ts

/**
 * Shared animation tokens for the memory feature.
 *
 * Mirrors the pattern used in `app/settings/setup/tabs/SearchTab.tsx`
 * so the memory pages feel visually consistent with the rest of
 * the app. Importing these constants (rather than redefining the
 * easing curve in every file) guarantees a single animation
 * vocabulary across the entire feature.
 */

import type { Variants } from 'framer-motion';

/**
 * The "confident, not bouncy" easing curve used throughout
 * SearchTab. Expo-out: fast start, soft landing -- reads as
 * "settling into place" rather than "arriving from off-screen".
 */
export const EASE_OUT_EXPO: [number, number, number, number] = [
  0.16, 1, 0.3, 1,
];

/** Standard entry animation for cards and sections. */
export const FADE_UP: Variants = {
  initial: { opacity: 0, y: 20 },
  animate: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.5, ease: EASE_OUT_EXPO },
  },
};

/** Fast entry for elements that should not draw attention. */
export const FADE_IN: Variants = {
  initial: { opacity: 0 },
  animate: { opacity: 1, transition: { duration: 0.4 } },
};

/** Container that staggers its children in sequence. */
export const STAGGER_CONTAINER: Variants = {
  initial: {},
  animate: { transition: { staggerChildren: 0.04 } },
};

/** Child item of a STAGGER_CONTAINER. */
export const STAGGER_ITEM: Variants = {
  initial: { opacity: 0, y: 10 },
  animate: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.35, ease: EASE_OUT_EXPO },
  },
};
