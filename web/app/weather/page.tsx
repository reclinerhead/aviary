import { permanentRedirect } from "next/navigation";

// The station view lives under the Aviary masthead since #652
// (/aviary/weather). This segment keeps the old URL honest for bookmarks
// and the weather.lan door (a 308, per the Next 16 docs), and it still owns
// /weather/history, the archive route the deep charts read -- the
// page-and-API-child arrangement #142 set up, now a redirect-and-API-child.
export default function WeatherPage() {
  permanentRedirect("/aviary/weather");
}
