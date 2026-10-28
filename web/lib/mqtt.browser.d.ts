// mqtt.js ships two builds: the default `mqtt` entry is the Node build (uses
// Buffer + node streams), which Turbopack bundles for the browser but which
// then can't serialize packets there -- the CONNECT never sends and the client
// dies with "connack timeout". `mqtt/dist/mqtt.esm` is the self-contained
// browser build. It has no bundled types, so borrow the package's own.
declare module "mqtt/dist/mqtt.esm" {
  import mqtt from "mqtt";
  export * from "mqtt";
  export default mqtt;
}
