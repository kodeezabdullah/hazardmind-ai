import { NextRequest, NextResponse } from "next/server";

// Server-side proxy for Cloudflare R2 artifacts. The public R2 bucket does not
// send CORS headers, so the browser blocks direct fetches of the PNG maps /
// zones GeoJSON. This route fetches them on the server (no CORS) and re-serves
// them with `Access-Control-Allow-Origin: *` so the map can load them.

const ALLOWED_HOST = "pub-720f47eaad2f4997a76a02f8bf14f58a.r2.dev";

export async function GET(req: NextRequest) {
  const target = req.nextUrl.searchParams.get("url");
  if (!target) {
    return NextResponse.json({ error: "missing url" }, { status: 400 });
  }

  let parsed: URL;
  try {
    parsed = new URL(target);
  } catch {
    return NextResponse.json({ error: "invalid url" }, { status: 400 });
  }
  // Only proxy our own R2 bucket — never an arbitrary URL.
  if (parsed.hostname !== ALLOWED_HOST) {
    return NextResponse.json({ error: "host not allowed" }, { status: 403 });
  }

  try {
    const upstream = await fetch(parsed.toString(), { cache: "no-store" });
    if (!upstream.ok) {
      return NextResponse.json({ error: `upstream ${upstream.status}` }, { status: upstream.status });
    }
    const body = await upstream.arrayBuffer();
    const contentType = upstream.headers.get("content-type") ?? "application/octet-stream";
    return new NextResponse(body, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
      },
    });
  } catch (error) {
    return NextResponse.json({ error: String(error) }, { status: 502 });
  }
}
