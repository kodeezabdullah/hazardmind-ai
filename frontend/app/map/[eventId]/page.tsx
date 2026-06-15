import { DashboardShell } from "../../../components/DashboardShell";

type EventMapPageProps = {
  params: {
    eventId: string;
  };
};

export default function EventMapPage({ params }: EventMapPageProps) {
  return <DashboardShell eventId={params.eventId} routeMode="map" />;
}
