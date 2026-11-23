import * as React from "react";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { BiometricGate } from "./components/BiometricGate";
import { ServerConnectScreen } from "./components/ServerConnectScreen";
import { getServerUrl, isNativeCapacitor } from "./lib/serverUrl";
import "./index.css";

function Root(): React.ReactElement {
  const [ready, setReady] = React.useState(() => !isNativeCapacitor() || Boolean(getServerUrl()));

  React.useEffect(() => {
    const onChangeServer = (): void => setReady(false);
    window.addEventListener("quartz:change-server", onChangeServer);
    return () => window.removeEventListener("quartz:change-server", onChangeServer);
  }, []);

  if (!ready) {
    return <ServerConnectScreen onConnected={() => setReady(true)} />;
  }
  // The gate is a no-op in a browser and when the lock is off, so this wrapper costs
  // nothing for the web build.
  return (
    <BiometricGate>
      <App />
    </BiometricGate>
  );
}

const container = document.getElementById("root");
if (!container) throw new Error("Root element #root not found");

createRoot(container).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
