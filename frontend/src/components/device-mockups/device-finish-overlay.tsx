import React from "react";
import type { DeviceKind } from "./device-types";

interface DeviceFinishOverlayProps {
  device: DeviceKind;
  className?: string;
}

/** Lightweight SVG highlight pass layered over CSS-built device geometry. */
export function DeviceFinishOverlay({ device, className = "" }: DeviceFinishOverlayProps) {
  const gradientId = React.useId().replace(/:/g, "");
  const metalGradientId = `pdm-metal-highlight-${gradientId}`;
  const edgeGradientId = `pdm-edge-glint-${gradientId}`;
  const radius = device === "phone" ? 17 : device === "tablet" ? 12 : 5;
  return (
    <svg
      className={`pdm-finish-overlay ${className}`}
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id={metalGradientId} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#ffffff" stopOpacity=".88" />
          <stop offset=".18" stopColor="#dbeafe" stopOpacity=".08" />
          <stop offset=".52" stopColor="#ffffff" stopOpacity=".02" />
          <stop offset=".82" stopColor="#cbd5e1" stopOpacity=".18" />
          <stop offset="1" stopColor="#ffffff" stopOpacity=".72" />
        </linearGradient>
        <linearGradient id={edgeGradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffffff" stopOpacity=".55" />
          <stop offset=".5" stopColor="#ffffff" stopOpacity=".04" />
          <stop offset="1" stopColor="#020617" stopOpacity=".32" />
        </linearGradient>
      </defs>
      <rect
        x=".7" y=".7" width="98.6" height="98.6" rx={radius}
        fill="none" stroke={`url(#${metalGradientId})`} strokeWidth="1.2"
        vectorEffect="non-scaling-stroke"
      />
      <path
        d={`M ${radius + 3} 1.3 H ${100 - radius - 3}`}
        fill="none" stroke={`url(#${edgeGradientId})`} strokeWidth=".8"
        vectorEffect="non-scaling-stroke"
      />
      <path
        d={`M 1.3 ${radius + 3} V ${100 - radius - 3}`}
        fill="none" stroke="#ffffff" strokeOpacity=".22" strokeWidth=".65"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
