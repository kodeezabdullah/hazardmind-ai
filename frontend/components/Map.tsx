import { useEffect, useRef } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

export default function Map() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const map = new maplibregl.Map({
      container: ref.current,
      style: process.env.NEXT_PUBLIC_MAP_STYLE_URL || "https://demotiles.maplibre.org/style.json",
      center: [0, 20],
      zoom: 2,
    });
    return () => map.remove();
  }, []);

  return <div ref={ref} style={{ width: "100%", height: "100%" }} />;
}
