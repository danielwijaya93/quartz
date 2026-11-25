package dev.quartz.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.service.notification.StatusBarNotification;

import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

import org.json.JSONObject;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

/**
 * Foreground service that keeps Quartz connected to `/ws/notifications` in native Java,
 * completely immune to Android WebView JS pausing and screen-off Doze throttling.
 */
public class QuartzKeepAliveService extends Service {

    public static final String CHANNEL_ID = "quartz_keepalive";
    public static final String ALERT_CHANNEL_ID = "quartz_alerts";
    private static final int NOTIFICATION_ID = 4711;
    private static final long WAKE_LOCK_TIMEOUT_MS = 60L * 60L * 1000L;
    private static final String PREFS_NAME = "quartz_native";
    private static final String KEY_SERVER_URL = "server_url";
    private static final String KEY_TOKEN = "auth_token";
    private static final String KEY_ENABLED = "notifications_enabled";

    private static volatile boolean running = false;

    private PowerManager.WakeLock wakeLock = null;
    private WifiManager.WifiLock wifiLock = null;
    private OkHttpClient httpClient = null;
    private WebSocket webSocket = null;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private volatile boolean destroyed = false;
    private volatile boolean notificationsEnabled = true;
    private volatile boolean notifyTurn = true;
    private volatile boolean notifyApproval = true;
    private volatile boolean notifyBackground = true;
    private volatile boolean notifyError = true;
    private String currentServerUrl = "";
    private String currentToken = "";
    private int reconnectAttempt = 0;

    public static boolean isRunning() {
        return running;
    }

    public static void saveConfig(Context context, String serverUrl, String token, boolean enabled) {
        SharedPreferences prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        SharedPreferences.Editor ed = prefs.edit();
        if (serverUrl != null) ed.putString(KEY_SERVER_URL, serverUrl);
        if (token != null) ed.putString(KEY_TOKEN, token);
        ed.putBoolean(KEY_ENABLED, enabled);
        ed.apply();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        httpClient = new OkHttpClient.Builder()
            .pingInterval(25, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        running = true;
        destroyed = false;
        createChannel(this);
        createAlertChannel(this);
        startForeground(NOTIFICATION_ID, buildNotification(this));
        acquireLocks();

        SharedPreferences prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        String nextUrl = intent != null && intent.hasExtra(KEY_SERVER_URL)
            ? intent.getStringExtra(KEY_SERVER_URL)
            : prefs.getString(KEY_SERVER_URL, "");
        String nextToken = intent != null && intent.hasExtra(KEY_TOKEN)
            ? intent.getStringExtra(KEY_TOKEN)
            : prefs.getString(KEY_TOKEN, "");
        notificationsEnabled = intent != null && intent.hasExtra(KEY_ENABLED)
            ? intent.getBooleanExtra(KEY_ENABLED, true)
            : prefs.getBoolean(KEY_ENABLED, true);
        notifyTurn = prefs.getBoolean("notifyTurn", true);
        notifyApproval = prefs.getBoolean("notifyApproval", true);
        notifyBackground = prefs.getBoolean("notifyBackground", true);
        notifyError = prefs.getBoolean("notifyError", true);

        if (nextUrl == null) nextUrl = "";
        if (nextToken == null) nextToken = "";

        if (!nextUrl.equals(currentServerUrl) || !nextToken.equals(currentToken) || webSocket == null) {
            currentServerUrl = nextUrl;
            currentToken = nextToken;
            reconnectAttempt = 0;
            connectWebSocket();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        destroyed = true;
        handler.removeCallbacksAndMessages(null);
        closeWebSocket();
        if (httpClient != null) {
            httpClient.dispatcher().executorService().shutdown();
        }
        releaseLocks();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private synchronized void closeWebSocket() {
        if (webSocket != null) {
            try {
                webSocket.close(1000, "Service stopped");
            } catch (Exception ignored) {
                // Ignore close errors.
            }
            webSocket = null;
        }
    }

    private synchronized void connectWebSocket() {
        if (destroyed || currentServerUrl.isEmpty()) return;
        closeWebSocket();
        String wsUrl = buildNotificationsWsUrl(currentServerUrl, currentToken);
        if (wsUrl == null) return;

        Request request = new Request.Builder().url(wsUrl).build();
        webSocket = httpClient.newWebSocket(request, new WebSocketListener() {
            @Override
            public void onOpen(WebSocket ws, Response response) {
                reconnectAttempt = 0;
                refreshWakeLock();
            }

            @Override
            public void onMessage(WebSocket ws, String text) {
                refreshWakeLock();
                handleFrame(text);
            }

            @Override
            public void onClosing(WebSocket ws, int code, String reason) {
                ws.close(1000, null);
            }

            @Override
            public void onClosed(WebSocket ws, int code, String reason) {
                if (code != 4401) {
                    scheduleReconnect();
                }
            }

            @Override
            public void onFailure(WebSocket ws, Throwable t, Response response) {
                scheduleReconnect();
            }
        });
    }

    private void scheduleReconnect() {
        if (destroyed || currentServerUrl.isEmpty()) return;
        handler.removeCallbacks(reconnectRunnable);
        long delayMs = Math.min(30_000L, 2_000L * (1L << Math.min(4, reconnectAttempt)));
        reconnectAttempt += 1;
        handler.postDelayed(reconnectRunnable, delayMs);
    }

    private final Runnable reconnectRunnable = this::connectWebSocket;

    private static String buildNotificationsWsUrl(String serverUrl, String token) {
        String trimmed = serverUrl.trim();
        if (trimmed.isEmpty()) return null;
        while (trimmed.endsWith("/")) {
            trimmed = trimmed.substring(0, trimmed.length() - 1);
        }
        String wsBase;
        if (trimmed.startsWith("https://")) {
            wsBase = "wss://" + trimmed.substring("https://".length());
        } else if (trimmed.startsWith("http://")) {
            wsBase = "ws://" + trimmed.substring("http://".length());
        } else if (trimmed.startsWith("wss://") || trimmed.startsWith("ws://")) {
            wsBase = trimmed;
        } else {
            wsBase = "https://" + trimmed;
            wsBase = "wss://" + trimmed;
        }
        String url = wsBase + "/ws/notifications";
        if (token != null && !token.isEmpty()) {
            url += "?token=" + URLEncoder.encode(token, StandardCharsets.UTF_8);
        }
        return url;
    }

    private void handleFrame(String raw) {
        try {
            JSONObject obj = new JSONObject(raw);
            String type = obj.optString("type", "");
            if ("settings_state".equals(type)) {
                notificationsEnabled = obj.optBoolean("notifications", true);
                notifyTurn = obj.optBoolean("notifyTurn", true);
                notifyApproval = obj.optBoolean("notifyApproval", true);
                notifyBackground = obj.optBoolean("notifyBackground", true);
                notifyError = obj.optBoolean("notifyError", true);
                saveConfig(this, null, null, notificationsEnabled);
                getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
                    .edit()
                    .putBoolean("notifyTurn", notifyTurn)
                    .putBoolean("notifyApproval", notifyApproval)
                    .putBoolean("notifyBackground", notifyBackground)
                    .putBoolean("notifyError", notifyError)
                    .apply();
                return;
            }
            if ("notifications_cleared".equals(type)) {
                clearAlertNotifications(this);
                return;
            }
            if (!notificationsEnabled || MainActivity.isForeground) {
                return;
            }
            String title = null;
            String body = null;
            String tag = null;
            if ("turn_end".equals(type)) {
                if (!notifyTurn) return;
                String status = obj.optString("status", "");
                String sessionTitle = obj.optString("sessionTitle", "");
                String prefix = sessionTitle.isEmpty() ? "Quartz" : sessionTitle;
                if ("ok".equals(status)) {
                    double secs = obj.optDouble("elapsedMs", 0) / 1000.0;
                    title = prefix + ": Turn finished";
                    body = String.format(Locale.US, "Completed in %.1fs.", secs);
                    tag = "quartz-turn-" + obj.optString("sessionId", "default");
                } else if ("error".equals(status)) {
                    String err = compactBody(obj.optString("error", ""));
                    title = prefix + ": Turn failed";
                    body = err.isEmpty() ? "An error occurred during the turn." : err;
                    tag = "quartz-turn-" + obj.optString("sessionId", "default");
                }
            } else if ("permission_request".equals(type)) {
                if (!notifyApproval) return;
                String cmd = obj.optString("command", "");
                if (cmd.isEmpty()) cmd = obj.optString("tool", "action");
                title = "Quartz: Approval needed";
                body = compactBody("Allow " + cmd + "?");
                tag = "quartz-permission-" + obj.optString("requestId", "0");
            } else if ("bash_notice".equals(type)) {
                if (!notifyBackground) return;
                title = "Quartz: Background job finished";
                String text = compactBody(obj.optString("text", ""));
                body = text.isEmpty() ? "A background job finished." : text;
                tag = "quartz-bash";
            } else if ("error".equals(type)) {
                if (!notifyError) return;
                title = "Quartz: Error";
                String msg = compactBody(obj.optString("message", ""));
                body = msg.isEmpty() ? "An error occurred." : msg;
                tag = "quartz-error";
            }
            if (title != null && body != null && tag != null) {
                postAlertNotification(title, body, tag);
            }
        } catch (Exception ignored) {
            // Ignore malformed JSON frames.
        }
    }

    private static String compactBody(String text) {
        if (text == null) return "";
        String collapsed = text.replaceAll("\\s+", " ").trim();
        if (collapsed.length() <= 120) return collapsed;
        return collapsed.substring(0, 117).trim() + "...";
    }

    private void postAlertNotification(String title, String body, String tag) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, android.Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                return;
            }
        }
        createAlertChannel(this);
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager == null) return;

        Intent open = new Intent(this, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent pending = PendingIntent.getActivity(
            this,
            1,
            open,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        Notification alert = new NotificationCompat.Builder(this, ALERT_CHANNEL_ID)
            .setContentTitle(title)
            .setContentText(body)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setAutoCancel(true)
            .setContentIntent(pending)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setDefaults(NotificationCompat.DEFAULT_ALL)
            .setGroup("quartz")
            .build();

        int id = 10_000 + (Math.abs(tag.hashCode()) % 1_000_000);
        manager.notify(id, alert);
    }

    public static void clearAlertNotifications(Context context) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            for (StatusBarNotification sbn : manager.getActiveNotifications()) {
                if (sbn.getId() != NOTIFICATION_ID) {
                    manager.cancel(sbn.getId());
                }
            }
        }
    }

    private void refreshWakeLock() {
        if (wakeLock != null) {
            try {
                wakeLock.acquire(WAKE_LOCK_TIMEOUT_MS);
            } catch (Exception ignored) {
                // Ignore.
            }
        }
    }

    private void acquireLocks() {
        if (wakeLock == null) {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "Quartz::KeepAliveWakeLock");
                wakeLock.setReferenceCounted(false);
            }
        }
        refreshWakeLock();

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
        channel.setDescription("Keeps Quartz connected for lock-screen notifications");
        channel.setShowBadge(false);
        manager.createNotificationChannel(channel);
    }

    static void createAlertChannel(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager == null) return;
        if (manager.getNotificationChannel(ALERT_CHANNEL_ID) != null) return;
        NotificationChannel channel = new NotificationChannel(
            ALERT_CHANNEL_ID,
            "Quartz alerts",
            NotificationManager.IMPORTANCE_HIGH
        );
        channel.setDescription("Notifications when a turn finishes or approval is needed");
        channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        channel.enableVibration(true);
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
            .setContentTitle("Quartz is connected")
            .setContentText("Listening for turn & approval notifications.")
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(pending)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build();
    }
}
