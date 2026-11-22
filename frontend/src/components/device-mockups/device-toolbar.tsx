import React from "react";
import {
  Maximize2,
  Smartphone,
  Tablet,
  Laptop,
  Monitor,
  RotateCcw,
  Frame,
} from "lucide-react";
import type { DeviceKind, LaptopStyle } from "./device-types";

interface DeviceToolbarProps {
  device: DeviceKind;
  width: number;
  height: number;
  mockup: boolean;
  laptopStyle: LaptopStyle;
  scale: number;
  onDeviceChange: (device: DeviceKind) => void;
  onWidthChange: (width: number) => void;
  onHeightChange: (height: number) => void;
  onMockupChange: (enabled: boolean) => void;
  onLaptopStyleChange: (style: LaptopStyle) => void;
  onRotate: () => void;
}

const buttonClass = (active: boolean) =>
  `pdm-toolbar-button${active ? " is-active" : ""}`;

export function DeviceToolbar({
  device,
  width,
  height,
  mockup,
  laptopStyle,
  scale,
  onDeviceChange,
  onWidthChange,
  onHeightChange,
  onMockupChange,
  onLaptopStyleChange,
  onRotate,
}: DeviceToolbarProps) {
  const presets: Array<{ id: DeviceKind; label: string; Icon: React.ElementType }> = [
    { id: "responsive", label: "Responsive", Icon: Maximize2 },
    { id: "phone", label: "Phone", Icon: Smartphone },
    { id: "tablet", label: "Tablet", Icon: Tablet },
    { id: "laptop", label: "Laptop", Icon: Laptop },
    { id: "desktop", label: "Desktop", Icon: Monitor },
  ];

  return (
    <div className="pdm-toolbar">
      <div className="pdm-toolbar-presets" role="group" aria-label="Device preset">
        {presets.map(({ id, label, Icon }) => (
          <button
            key={id}
            type="button"
            className={buttonClass(device === id)}
            onClick={() => onDeviceChange(id)}
            aria-pressed={device === id}
            title={`${label} viewport`}
          >
            <Icon size={15} aria-hidden="true" />
            <span>{label}</span>
          </button>
        ))}
      </div>

      <div className="pdm-toolbar-divider" />

      <label className="pdm-dimension">
        <span>W</span>
        <input
          type="number"
          min={240}
          max={3840}
          value={width}
          onChange={(event) => onWidthChange(Number(event.target.value))}
          aria-label="Viewport width in pixels"
        />
      </label>
      <span className="pdm-dimension-x">×</span>
      <label className="pdm-dimension">
        <span className="sr-only">Height</span>
        <input
          type="number"
          min={320}
          max={3000}
          value={height}
          onChange={(event) => onHeightChange(Number(event.target.value))}
          aria-label="Viewport height in pixels"
        />
      </label>
      <span className="pdm-px-label">px</span>

      <button type="button" className={buttonClass(false)} onClick={onRotate} title="Rotate viewport">
        <RotateCcw size={15} aria-hidden="true" />
      </button>

      <button
        type="button"
        className={buttonClass(mockup)}
        onClick={() => onMockupChange(!mockup)}
        aria-pressed={mockup}
        title={mockup ? "Turn device frame off" : "Turn device frame on"}
      >
        <Frame size={15} aria-hidden="true" />
        <span>Mockup {mockup ? "On" : "Off"}</span>
      </button>

      {device === "laptop" && mockup && (
        <select
          className="pdm-style-select"
          value={laptopStyle}
          onChange={(event) => onLaptopStyleChange(event.target.value as LaptopStyle)}
          aria-label="Laptop appearance"
        >
          <option value="macbook">MacBook</option>
          <option value="windows">Windows</option>
        </select>
      )}

      <div className="pdm-toolbar-status">
        <span>{Math.round(scale * 100)}%</span>
        <span>{width} × {height}</span>
      </div>
    </div>
  );
}
