import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'dev.quartz.app',
  appName: 'Quartz',
  webDir: 'dist',
  server: {
    hostname: 'app.quartz.local',
    androidScheme: 'http',
    cleartext: true,
  },
};

export default config;
