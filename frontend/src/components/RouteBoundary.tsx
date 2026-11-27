import { Component, type ReactNode } from "react";
import { Alert, Button, Skeleton } from "./ui";

export class RouteBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <section className="m-6 max-w-xl" aria-label="Page recovery">
      <Alert tone="danger" title="This page couldn't open">Your session is still available. Try loading the page again, or use navigation to open another area.</Alert>
      <Button className="mt-4" onClick={() => window.location.reload()}>Reload page</Button>
    </section>;
    return this.props.children;
  }
}

export function RouteLoading() {
  return <div className="grid gap-4 p-6" role="status" aria-label="Loading page">
    <span className="sr-only">Loading page…</span>
    <Skeleton className="h-8 w-48" /><Skeleton className="h-4 w-2/3" />
    <div className="grid gap-4 sm:grid-cols-3">{[0, 1, 2].map(i => <Skeleton key={i} className="h-28 w-full" />)}</div>
    <Skeleton className="h-60 w-full" />
  </div>;
}
