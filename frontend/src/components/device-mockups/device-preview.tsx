import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DeviceFrame } from "./device-frame";
import { DeviceToolbar } from "./device-toolbar";
import {
  DEVICE_PRESETS,
  VIEWPORT_LIMITS,
  type DeviceKind,
  type LaptopStyle,
} from "./device-types";
import "./device-mockups.css";

interface DevicePreviewProps {
  children: React.ReactNode;
  className?: string;
  initialDevice?: DeviceKind;
  initialMockup?: boolean;
  initialLaptopStyle?: LaptopStyle;
}

/**
 * Reusable responsive preview shell.
 *
 * It owns only preview presentation and dimensions. It does not know whether
 * the child is an iframe, a local app, or any other React content.
 */
export function DevicePreview({
  children,
  className = "",
  initialDevice = "responsive",
  initialMockup = true,
  initialLaptopStyle = "macbook",
}: DevicePreviewProps) {
  const [device, setDevice] = useState<DeviceKind>(initialDevice);
  const [width, setWidth] = useState(1280);
  const [height, setHeight] = useState(800);
  const [mockup, setMockup] = useState(initialMockup);
  const [laptopStyle, setLaptopStyle] = useState<LaptopStyle>(initialLaptopStyle);
  const [bounds, setBounds] = useState({ width: 0, height: 0 });
  const [dragging, setDragging] = useState(false);
  const areaRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef({ startX: 0, startWidth: 0 });

  useEffect(() => {
    const node = areaRef.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) => {
      setBounds({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const frameInsets = useMemo(() => {
    if (!mockup) return { horizontal: 0, vertical: 0, bottom: 0 };
    switch (device) {
      case "phone": return { horizontal: 30, vertical: 30, bottom: 0 };
      case "tablet": return { horizontal: 34, vertical: 34, bottom: 0 };
      case "laptop": return { horizontal: 32, vertical: 32, bottom: 38 };
      case "desktop": return { horizontal: 30, vertical: 30, bottom: 62 };
      default: return { horizontal: 0, vertical: 0, bottom: 0 };
    }
  }, [device, mockup]);

  const scale = useMemo(() => {
    if (!bounds.width || !bounds.height) return 1;
    const availableWidth = Math.max(100, bounds.width - 64);
    const availableHeight = Math.max(100, bounds.height - 64);
    const fullWidth = width + frameInsets.horizontal;
    const fullHeight = height + frameInsets.vertical + frameInsets.bottom;
    return Math.max(0.08, Math.min(1, availableWidth / fullWidth, availableHeight / fullHeight));
  }, [bounds, width, height, frameInsets]);

  const selectDevice = useCallback((next: DeviceKind) => {
    setDevice(next);
    if (next === "responsive") {
      setMockup(false);
      setWidth(Math.max(240, Math.min(3840, Math.floor(bounds.width || 1280))));
      setHeight(Math.max(320, Math.min(3000, Math.floor((bounds.height || 840) - 72))));
      return;
    }
    if (next === "custom") return;
    const preset = DEVICE_PRESETS[next];
    setWidth(preset.width);
    setHeight(preset.height);
    setMockup(true);
  }, [bounds]);

  const updateWidth = (value: number) => {
    setWidth(Math.max(VIEWPORT_LIMITS.minWidth, Math.min(VIEWPORT_LIMITS.maxWidth, value || VIEWPORT_LIMITS.minWidth)));
    setDevice("custom");
  };

  const updateHeight = (value: number) => {
    setHeight(Math.max(VIEWPORT_LIMITS.minHeight, Math.min(VIEWPORT_LIMITS.maxHeight, value || VIEWPORT_LIMITS.minHeight)));
    setDevice("custom");
  };

  const rotate = () => {
    setWidth(height);
    setHeight(width);
    setDevice("custom");
  };

  useEffect(() => {
    if (!dragging) return;
    const onMove = (event: PointerEvent) => {
      const delta = (event.clientX - dragRef.current.startX) / Math.max(scale, 0.08);
      updateWidth(Math.round(dragRef.current.startWidth + delta));
    };
    const onUp = () => setDragging(false);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [dragging, scale]);

  const outerWidth = (width + frameInsets.horizontal) * scale;
  const outerHeight = (height + frameInsets.vertical + frameInsets.bottom) * scale;

  return (
    <section className={`pdm-root ${className}`}>
      <DeviceToolbar
        device={device}
        width={width}
        height={height}
        mockup={mockup}
        laptopStyle={laptopStyle}
        scale={scale}
        onDeviceChange={selectDevice}
        onWidthChange={updateWidth}
        onHeightChange={updateHeight}
        onMockupChange={setMockup}
        onLaptopStyleChange={setLaptopStyle}
        onRotate={rotate}
      />

      <div ref={areaRef} className="pdm-stage">
        <div className="pdm-stage-center">
          <div className="pdm-scaled-box" style={{ width: outerWidth, height: outerHeight }}>
            <div
              className="pdm-scaled-content"
              style={{
                width: width + frameInsets.horizontal,
                height: height + frameInsets.vertical + frameInsets.bottom,
                transform: `scale(${scale})`,
              }}
            >
              <DeviceFrame
                device={device}
                mockup={mockup}
                laptopStyle={laptopStyle}
                viewportWidth={width}
                viewportHeight={height}
              >
                {children}
              </DeviceFrame>
            </div>
            <button
              type="button"
              className="pdm-resize-handle"
              aria-label="Drag to resize viewport width"
              title="Drag to change viewport width"
              onPointerDown={(event) => {
                event.preventDefault();
                dragRef.current = { startX: event.clientX, startWidth: width };
                setDragging(true);
              }}
            />
          </div>
        </div>
        <div className="pdm-stage-hint">
          Drag the handle to resize · Actual CSS viewport: {width} × {height}px
        </div>
      </div>
    </section>
  );
}

export default DevicePreview;
