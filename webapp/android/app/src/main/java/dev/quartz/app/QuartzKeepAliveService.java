package dev.quartz.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

import androidx.core.app.NotificationCompat;

/**
 * A foreground service that keeps the WebView's process alive while a turn runs.
 *
 * Android freezes a cached (backgrounded) process. The WebSocket then goes quiet and
 * a turn that takes minutes looks like it died. A foreground service with a visible
 * notification is the only sanctioned way to say "this process is doing work", so the
 * web layer starts this on turn_start and stops it on turn_end.
 *
 * It deliberately does no work of its own: the work is the kernel on the server, and
 * all this process has to do is stay schedulable long enough to receive the frames.
 */
public class QuartzKeepAliveService extends Service {

    public static final String CHANNEL_ID = "quartz_keepalive";
    private static final int NOTIFICATION_ID = 4711;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        createChannel(this);
        startForeground(NOTIFICATION_ID, buildNotification(this));
        // START_NOT_STICKY: a process the OS killed should not resurrect a service
        // that claims a turn is running. The app decides afresh on next launch.
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
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
