import React from "react";
import type { DeviceKind, LaptopStyle } from "./device-types";
import { DeviceFinishOverlay } from "./device-finish-overlay";

interface DeviceFrameProps {
  device: DeviceKind;
  mockup: boolean;
  laptopStyle: LaptopStyle;
  viewportWidth: number;
  viewportHeight: number;
  children: React.ReactNode;
}

/**
 * Decorative hardware only. The child remains an independent, exact-size
 * CSS viewport; frame chrome never changes the iframe's layout dimensions.
 */
export function DeviceFrame({
  device,
  mockup,
  laptopStyle,
  viewportWidth,
  viewportHeight,
  children,
}: DeviceFrameProps) {
  if (!mockup || device === "responsive" || device === "custom") {
    return (
      <div className="pdm-plain-viewport" style={{ width: viewportWidth, height: viewportHeight }}>
        {children}
      </div>
    );
  }

  if (device === "phone") {
    return (
      <div className="pdm-phone-shell" style={{ width: viewportWidth + 30, height: viewportHeight + 30 }}>
        <div className="pdm-phone-button pdm-phone-button--power" />
        <div className="pdm-phone-button pdm-phone-button--volume-up" />
        <div className="pdm-phone-button pdm-phone-button--volume-down" />
        <div className="pdm-phone-glass">
          <div className="pdm-phone-camera-island">
            <span className="pdm-phone-camera-lens" />
          </div>
          <div className="pdm-phone-screen" style={{ width: viewportWidth, height: viewportHeight }}>
            {children}
          </div>
        </div>
        <DeviceFinishOverlay device="phone" />
        <div className="pdm-phone-home-indicator" />
      </div>
    );
  }

  if (device === "tablet") {
    return (
      <div className="pdm-tablet-shell" style={{ width: viewportWidth + 34, height: viewportHeight + 34 }}>
        <span className="pdm-tablet-camera" />
        <div className="pdm-tablet-screen" style={{ width: viewportWidth, height: viewportHeight }}>
          {children}
        </div>
        <DeviceFinishOverlay device="tablet" /> 
      </div>
    );
  }

  if (device === "laptop") {
    return (
      <div className={`pdm-laptop pdm-laptop--${laptopStyle}`} style={{ width: viewportWidth + 32 }}>
        <div className="pdm-laptop-lid" style={{ width: viewportWidth + 32, height: viewportHeight + 32 }}>
          <span className="pdm-laptop-camera" />
          <div className="pdm-laptop-screen" style={{ width: viewportWidth, height: viewportHeight }}>
            {children}
          </div>
          <DeviceFinishOverlay device="laptop" /> 
        </div>
        <div className="pdm-laptop-hinge" />
        <div className="pdm-laptop-base">
          {laptopStyle === "macbook" ? (
            <div className="pdm-laptop-trackpad" />
          ) : (
            <div className="pdm-windows-keyboard">
              {Array.from({ length: 5 }).map((_, row) => (
                <div className="pdm-key-row" key={row}>
                  {Array.from({ length: row === 4 ? 7 : 13 }).map((__, key) => (
                    <i key={key} />
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="pdm-desktop" style={{ width: viewportWidth + 30 }}>
      <div className="pdm-desktop-display" style={{ width: viewportWidth + 30, height: viewportHeight + 30 }}>
        <span className="pdm-desktop-camera" />
        <div className="pdm-desktop-screen" style={{ width: viewportWidth, height: viewportHeight }}>
          {children}
        </div>
        <DeviceFinishOverlay device="desktop" />
      </div>
      <div className="pdm-desktop-neck" />
      <div className="pdm-desktop-foot" />
    </div>
  );
}
