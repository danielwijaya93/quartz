package dev.quartz.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import androidx.core.app.NotificationCompat;

/**
 * A foreground service that keeps the WebView's process, CPU, and Wi-Fi awake while a turn runs.
 *
 * Android freezes a cached (backgrounded) process and suspends CPU/Wi-Fi when the screen locks.
 * Holding a foreground notification + PARTIAL_WAKE_LOCK + WifiLock keeps the WebSocket alive
 * so turn_end / permission_request frames arrive and fire local notifications with the screen off.
 */
public class QuartzKeepAliveService extends Service {

    public static final String CHANNEL_ID = "quartz_keepalive";
    private static final int NOTIFICATION_ID = 4711;
    private static final long WAKE_LOCK_TIMEOUT_MS = 30L * 60L * 1000L;

    private static volatile boolean running = false;

    private PowerManager.WakeLock wakeLock = null;
    private WifiManager.WifiLock wifiLock = null;

    public static boolean isRunning() {
        return running;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        running = true;
        createChannel(this);
        startForeground(NOTIFICATION_ID, buildNotification(this));
        acquireLocks();
        // START_NOT_STICKY: a process the OS killed should not resurrect a service
        // that claims a turn is running. The app decides afresh on next launch.
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        releaseLocks();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void acquireLocks() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Quartz::KeepAliveWakeLock");
                wakeLock.setReferenceCounted(false);
            }
        }
        if (wakeLock != null && !wakeLock.isHeld()) {
            wakeLock.acquire(WAKE_LOCK_TIMEOUT_MS);
        }

        if (wifiLock == null) {
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
            if (wm != null) {
                int mode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                    ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                    : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                wifiLock = wm.createWifiLock(mode, "Quartz::KeepAliveWifiLock");
                wifiLock.setReferenceCounted(false);
            }
        }
        if (wifiLock != null && !wifiLock.isHeld()) {
            wifiLock.acquire();
        }
    }

    private void releaseLocks() {
        if (wakeLock != null && wakeLock.isHeld()) {
            try {
                wakeLock.release();
            } catch (Exception ignored) {
                // Already released.
            }
        }
        if (wifiLock != null && wifiLock.isHeld()) {
            try {
                wifiLock.release();
            } catch (Exception ignored) {
                // Already released.
            }
        }
    }

    static void createChannel(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return;
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "Quartz running",
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("Shown while Quartz is working on a turn");
        channel.setShowBadge(false);
        manager.createNotificationChannel(channel);
    }

    private static Notification buildNotification(Context context) {
        Intent open = new Intent(context, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pending = PendingIntent.getActivity(
            context,
            0,
            open,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        return new NotificationCompat.Builder(context, CHANNEL_ID)
            .setContentTitle("Quartz is working")
            .setContentText("Keeping the session alive so the turn can finish.")
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(pending)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build();
    }
}
