package dev.quartz.app;

import android.Manifest;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.hardware.biometrics.BiometricManager;
import android.net.Uri;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;

import androidx.biometric.BiometricPrompt;
import androidx.core.content.ContextCompat;
import androidx.fragment.app.FragmentActivity;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.util.concurrent.Executor;

/**
 * The few things a WebView cannot do by itself.
 *
 * Kept deliberately small: everything that is plain web API stays in the web layer, so
 * this file does not become a second application to maintain.
 */
@CapacitorPlugin(
    name = "QuartzNative",
    permissions = {
        @Permission(alias = "notifications", strings = { Manifest.permission.POST_NOTIFICATIONS })
    }
)
public class QuartzNativePlugin extends Plugin {

    /** Text handed over by an ACTION_SEND share, consumed once by the web layer. */
    private static volatile String pendingSharedText = null;

    static void setSharedText(String text) {
        pendingSharedText = text;
    }

    @Override
    public void load() {
        QuartzKeepAliveService.createChannel(getContext());
    }

    @PluginMethod
    public void setKeepAlive(PluginCall call) {
        boolean active = Boolean.TRUE.equals(call.getBoolean("active", false));
        String serverUrl = call.getString("serverUrl", null);
        String token = call.getString("token", null);
        boolean enabled = Boolean.TRUE.equals(call.getBoolean("notificationsEnabled", active));
        Context context = getContext();
        QuartzKeepAliveService.saveConfig(context, serverUrl, token, enabled);
        Intent intent = new Intent(context, QuartzKeepAliveService.class);
        if (serverUrl != null) intent.putExtra("server_url", serverUrl);
        if (token != null) intent.putExtra("auth_token", token);
        intent.putExtra("notifications_enabled", enabled);
        if (active) {
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    context.startForegroundService(intent);
                } else {
                    context.startService(intent);
                }
            } catch (Exception ignored) {
                // A background start can be refused; the turn still runs, only less safely.
            }
        } else {
            context.stopService(intent);
        }
        JSObject result = new JSObject();
        result.put("active", active);
        call.resolve(result);
    }

    @PluginMethod
    public void batteryOptimizationStatus(PluginCall call) {
        JSObject result = new JSObject();
        boolean ignored = true;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Context context = getContext();
            PowerManager pm = (PowerManager) context.getSystemService(Context.POWER_SERVICE);
            ignored = pm != null && pm.isIgnoringBatteryOptimizations(context.getPackageName());
        }
        result.put("ignored", ignored);
        call.resolve(result);
    }

    @PluginMethod
    public void requestIgnoreBatteryOptimizations(PluginCall call) {
        Context context = getContext();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            try {
                Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
                intent.setData(Uri.parse("package:" + context.getPackageName()));
                intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                context.startActivity(intent);
            } catch (Exception ignored) {
                try {
                    Intent fallback = new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
                    fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    context.startActivity(fallback);
                } catch (Exception ignoredToo) {
                    // Device has no battery optimization screen.
                }
            }
        }
        call.resolve();
    }

    @PluginMethod
    public void biometricStatus(PluginCall call) {
        JSObject result = new JSObject();
        FragmentActivity activity = (FragmentActivity) getActivity();
        boolean hardware = false;
        boolean enrolled = false;
        String kind = "none";
        if (activity != null) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                BiometricManager manager = activity.getSystemService(BiometricManager.class);
                if (manager != null) {
                    int strong = manager.canAuthenticate(
                        android.hardware.biometrics.BiometricManager.Authenticators.BIOMETRIC_STRONG
                    );
                    int weak = manager.canAuthenticate(
                        android.hardware.biometrics.BiometricManager.Authenticators.BIOMETRIC_WEAK
                    );
                    hardware = strong != android.hardware.biometrics.BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE
                        && weak != android.hardware.biometrics.BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE;
                    enrolled = strong == android.hardware.biometrics.BiometricManager.BIOMETRIC_SUCCESS
                        || weak == android.hardware.biometrics.BiometricManager.BIOMETRIC_SUCCESS;
                    if (enrolled) kind = "biometric";
                }
            } else {
                androidx.biometric.BiometricManager legacy =
                    androidx.biometric.BiometricManager.from(activity);
                hardware = legacy.canAuthenticate() != androidx.biometric.BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE;
                enrolled = legacy.canAuthenticate() == androidx.biometric.BiometricManager.BIOMETRIC_SUCCESS;
                if (enrolled) kind = "biometric";
            }
        }
        result.put("available", hardware);
        result.put("enrolled", enrolled);
        result.put("kind", kind);
        call.resolve(result);
    }

    @PluginMethod
    public void authenticate(PluginCall call) {
        final PluginCall pending = call;
        final FragmentActivity activity = (FragmentActivity) getActivity();
        if (activity == null) {
            JSObject result = new JSObject();
            result.put("ok", false);
            pending.resolve(result);
            return;
        }
        final String reason = call.getString("reason", "Unlock Quartz");
        activity.runOnUiThread(() -> {
            Executor executor = ContextCompat.getMainExecutor(activity);
            BiometricPrompt prompt = new BiometricPrompt(
                activity,
                executor,
                new BiometricPrompt.AuthenticationCallback() {
                    @Override
                    public void onAuthenticationSucceeded(BiometricPrompt.AuthenticationResult result) {
                        JSObject ok = new JSObject();
                        ok.put("ok", true);
                        pending.resolve(ok);
                    }

                    @Override
                    public void onAuthenticationError(int code, CharSequence message) {
                        // A cancel is an answer, not a failure: resolve so the caller
                        // does not have to distinguish exceptions from "no".
                        JSObject no = new JSObject();
                        no.put("ok", false);
                        pending.resolve(no);
                    }
                }
            );
            BiometricPrompt.PromptInfo info = new BiometricPrompt.PromptInfo.Builder()
                .setTitle("Unlock Quartz")
                .setSubtitle(reason)
                .setNegativeButtonText("Cancel")
                .setAllowedAuthenticators(
                    androidx.biometric.BiometricManager.Authenticators.BIOMETRIC_WEAK
                )
                .build();
            prompt.authenticate(info);
        });
    }

    @PluginMethod
    public void consumeSharedText(PluginCall call) {
        JSObject result = new JSObject();
        String text = pendingSharedText;
        pendingSharedText = null;
        result.put("text", text == null ? "" : text);
        result.put("kind", text == null ? "" : "text");
        call.resolve(result);
    }
}
