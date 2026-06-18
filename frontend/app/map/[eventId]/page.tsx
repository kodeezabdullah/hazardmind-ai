import { MapSnapshotView } from "../../../components/MapSnapshotView";

type EventMapPageProps = {
  params: {
    eventId: string;
  };
};

export default function EventMapPage({ params }: EventMapPageProps) {
  return <MapSnapshotView eventId={params.eventId} />;
}
