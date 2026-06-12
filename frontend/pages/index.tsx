import Map from "../components/Map";
import AgentLog from "../components/AgentLog";

export default function Home() {
  return (
    <div style={{ display: "flex", height: "100vh" }}>
      <div style={{ flex: 1 }}>
        <Map />
      </div>
      <div style={{ width: 400 }}>
        <AgentLog />
      </div>
    </div>
  );
}
